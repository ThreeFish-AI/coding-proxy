"""Google Antigravity Claude 后端 — OAuth2 认证 + Gemini 格式转换."""

from __future__ import annotations

import json
import logging
from typing import Any, AsyncIterator

import httpx

from ..auth.providers.google import GoogleOAuthProvider
from ..compat.canonical import CompatibilityProfile, CompatibilityStatus
from ..config.schema import AntigravityConfig, FailoverConfig
from ..convert.anthropic_to_gemini import convert_request
from ..convert.gemini_to_anthropic import convert_response, extract_usage
from ..convert.gemini_sse_adapter import adapt_sse_stream
from ..routing.model_mapper import ModelMapper
from .base import (
    CapabilityLossReason,
    BackendCapabilities,
    BackendResponse,
    BaseBackend,
    RequestCapabilities,
    UsageInfo,
    _sanitize_headers_for_synthetic_response,
)
from .token_manager import BaseTokenManager, TokenAcquireError, TokenErrorKind

logger = logging.getLogger(__name__)


class GoogleOAuthTokenManager(BaseTokenManager):
    """Google OAuth2 token 自动刷新管理.

    流程: refresh_token → POST oauth2.googleapis.com/token → access_token (~1 小时有效期)
    """

    _REFRESH_MARGIN = 120  # 提前刷新余量（秒）
    _TOKEN_URL = "https://oauth2.googleapis.com/token"

    def __init__(self, client_id: str, client_secret: str, refresh_token: str) -> None:
        super().__init__()
        self._client_id = client_id
        self._client_secret = client_secret
        self._refresh_token = refresh_token

    async def _acquire(self) -> tuple[str, float]:
        """通过 refresh_token 获取新的 access_token."""
        client = self._get_client()
        try:
            response = await client.post(
                self._TOKEN_URL,
                data={
                    "client_id": self._client_id,
                    "client_secret": self._client_secret,
                    "refresh_token": self._refresh_token,
                    "grant_type": "refresh_token",
                },
            )
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            # 400 + invalid_grant 表示 refresh_token 已失效
            if exc.response.status_code == 400:
                try:
                    err = exc.response.json()
                    if err.get("error") == "invalid_grant":
                        raise TokenAcquireError.with_kind(
                            "Google refresh_token 已失效",
                            kind=TokenErrorKind.INVALID_CREDENTIALS,
                            needs_reauth=True,
                        ) from exc
                except (ValueError, KeyError):
                    pass
            raise TokenAcquireError.with_kind(
                f"Google token 刷新失败: {exc}",
                kind=TokenErrorKind.TEMPORARY,
            ) from exc
        except (httpx.TimeoutException, httpx.ConnectError) as exc:
            raise TokenAcquireError.with_kind(
                f"Google token 刷新网络异常: {exc}",
                kind=TokenErrorKind.TEMPORARY,
            ) from exc
        data = response.json()
        scope = data.get("scope", "")
        if scope and not GoogleOAuthProvider.has_required_scopes(scope):
            raise TokenAcquireError.with_kind(
                "Google access_token 缺少 Antigravity 所需 scope",
                kind=TokenErrorKind.INSUFFICIENT_SCOPE,
                needs_reauth=True,
            )
        expires_in = data.get("expires_in", 3600)
        logger.info("Google OAuth token refreshed, expires_in=%ds", expires_in)
        return data["access_token"], float(expires_in)

    def update_refresh_token(self, new_refresh_token: str) -> None:
        """运行时热更新 refresh_token（重认证后调用）."""
        self._refresh_token = new_refresh_token
        self.invalidate()


class AntigravityBackend(BaseBackend):
    """Google Antigravity Claude API 后端.

    通过 Google OAuth2 认证，将 Anthropic 格式请求转换为 Gemini 格式
    发往 Generative AI 端点，并将响应转回 Anthropic 格式.
    """

    def __init__(
        self,
        config: AntigravityConfig,
        failover_config: FailoverConfig,
        model_mapper: ModelMapper,
    ) -> None:
        super().__init__(config.base_url, config.timeout_ms, failover_config)
        self._token_manager = GoogleOAuthTokenManager(
            config.client_id, config.client_secret, config.refresh_token,
        )
        self._model_endpoint = config.model_endpoint
        self._model_mapper = model_mapper
        self._default_model = config.model_endpoint.removeprefix("models/")
        self._last_request_adaptations: list[str] = []
        self._last_resolved_model = ""
        self._last_requested_model = ""
        self._last_model_resolution_reason = ""

    def get_name(self) -> str:
        return "antigravity"

    def get_capabilities(self) -> BackendCapabilities:
        return BackendCapabilities(
            supports_tools=True,
            supports_thinking=True,
            supports_images=True,
            supports_metadata=True,
        )

    def get_compatibility_profile(self) -> CompatibilityProfile:
        return CompatibilityProfile(
            thinking=CompatibilityStatus.NATIVE,
            tool_calling=CompatibilityStatus.NATIVE,
            tool_streaming=CompatibilityStatus.SIMULATED,
            mcp_tools=CompatibilityStatus.UNKNOWN,
            images=CompatibilityStatus.NATIVE,
            metadata=CompatibilityStatus.SIMULATED,
            json_output=CompatibilityStatus.UNKNOWN,
            usage_tokens=CompatibilityStatus.SIMULATED,
        )

    def supports_request(
        self, request_caps: RequestCapabilities,
    ) -> tuple[bool, list[CapabilityLossReason]]:
        supported, reasons = super().supports_request(request_caps)
        if not supported:
            reasons = [
                reason for reason in reasons
                if reason not in {
                    CapabilityLossReason.THINKING,
                    CapabilityLossReason.TOOLS,
                    CapabilityLossReason.METADATA,
                }
            ]
        return len(reasons) == 0, reasons

    def map_model(self, model: str) -> str:
        resolved = self._model_mapper.map(
            model,
            backend="antigravity",
            default=self._default_model,
        )
        self._last_requested_model = model
        self._last_resolved_model = resolved
        self._last_model_resolution_reason = (
            "configured_mapping" if resolved != self._default_model or model == self._default_model
            else "config_default_model_endpoint"
        )
        return resolved

    async def _prepare_request(
        self,
        request_body: dict[str, Any],
        headers: dict[str, str],
    ) -> tuple[dict[str, Any], dict[str, str]]:
        """转换 Anthropic 请求为 Gemini 格式，注入 Google OAuth token."""
        resolved_model = self.map_model(request_body.get("model", "unknown"))
        converted = convert_request(request_body, model=resolved_model)
        gemini_body = converted.body
        self._last_request_adaptations = converted.adaptations
        token = await self._token_manager.get_token()
        new_headers = {
            "content-type": "application/json",
            "authorization": f"Bearer {token}",
            "anthropic-beta": "claude-code-20250219",
        }
        return gemini_body, new_headers

    def _on_error_status(self, status_code: int) -> None:
        """401/403 时标记 token 失效以触发被动刷新."""
        if status_code in (401, 403):
            self._token_manager.invalidate()

    def _mark_scope_error_if_needed(self, error_text: str) -> None:
        lowered = error_text.lower()
        if "access_token_scope_insufficient" not in lowered:
            return
        self._token_manager.mark_error(
            "Google access_token scope 不足，当前凭证不能调用 Generative Language API",
            kind=TokenErrorKind.INSUFFICIENT_SCOPE,
            needs_reauth=True,
        )

    async def check_health(self) -> bool:
        """检查 Google OAuth token 是否可刷新（免费操作）."""
        try:
            token = await self._token_manager.get_token()
            return bool(token)
        except Exception:
            logger.warning("Antigravity health check failed: token refresh error")
            return False

    def get_diagnostics(self) -> dict[str, Any]:
        diagnostics = self._token_manager.get_diagnostics()
        result: dict[str, Any] = super().get_diagnostics()
        if diagnostics:
            result["token_manager"] = diagnostics
        if self._last_request_adaptations:
            result["request_adaptations"] = self._last_request_adaptations
        if self._last_requested_model:
            result["requested_model"] = self._last_requested_model
        if self._last_resolved_model:
            result["resolved_model"] = self._last_resolved_model
        if self._last_model_resolution_reason:
            result["model_resolution_reason"] = self._last_model_resolution_reason
        return result

    async def send_message(
        self,
        request_body: dict[str, Any],
        headers: dict[str, str],
    ) -> BackendResponse:
        """覆写: Gemini 端点 + 响应逆转换."""
        body, prepared_headers = await self._prepare_request(request_body, headers)
        client = self._get_client()
        resolved_model = self.map_model(request_body.get('model', 'unknown'))
        endpoint = f"/models/{resolved_model}:generateContent"

        response = await client.post(endpoint, json=body, headers=prepared_headers)

        if response.status_code >= 400:
            self._on_error_status(response.status_code)
            self._mark_scope_error_if_needed(response.text)
            return BackendResponse(
                status_code=response.status_code,
                raw_body=response.content,
                error_type="api_error",
                error_message=response.text[:500],
                response_headers=dict(response.headers),
            )

        gemini_resp = response.json()
        model = request_body.get("model", "unknown")
        anthropic_resp = convert_response(gemini_resp, model=model)
        usage_data = extract_usage(gemini_resp)
        raw_body = json.dumps(anthropic_resp).encode()

        return BackendResponse(
            status_code=200,
            raw_body=raw_body,
            usage=UsageInfo(
                input_tokens=usage_data.get("input_tokens", 0),
                output_tokens=usage_data.get("output_tokens", 0),
            ),
            model_served=resolved_model,
        )

    async def send_message_stream(
        self,
        request_body: dict[str, Any],
        headers: dict[str, str],
    ) -> AsyncIterator[bytes]:
        """覆写: Gemini SSE 流 → Anthropic SSE 流适配."""
        body, prepared_headers = await self._prepare_request(request_body, headers)
        client = self._get_client()
        resolved_model = self.map_model(request_body.get("model", "unknown"))
        endpoint = f"/models/{resolved_model}:streamGenerateContent?alt=sse"

        async with client.stream(
            "POST", endpoint, json=body, headers=prepared_headers,
        ) as response:
            if response.status_code >= 400:
                self._on_error_status(response.status_code)
                error_body = await response.aread()
                self._mark_scope_error_if_needed(
                    error_body.decode("utf-8", errors="ignore"),
                )
                logger.warning(
                    "%s stream error: status=%d body=%s",
                    self.get_name(), response.status_code, error_body[:500],
                )
                raise httpx.HTTPStatusError(
                    f"{self.get_name()} API error: {response.status_code}",
                    request=response.request,
                    response=httpx.Response(
                        response.status_code,
                        content=error_body,
                        headers=_sanitize_headers_for_synthetic_response(response.headers),
                        request=response.request,
                    ),
                )

            model = request_body.get("model", "unknown")
            async for chunk in adapt_sse_stream(response.aiter_bytes(), model):
                yield chunk

    async def close(self) -> None:
        await self._token_manager.close()
        await super().close()
