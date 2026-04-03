"""Google Antigravity Claude 后端 — OAuth2 认证 + Gemini 格式转换."""

from __future__ import annotations

import json
import logging
from typing import Any, AsyncIterator

import httpx

from ..compat.canonical import CompatibilityProfile, CompatibilityStatus
from ..config.schema import AntigravityConfig, FailoverConfig
from ..convert.anthropic_to_gemini import convert_request
from ..convert.gemini_to_anthropic import convert_response, extract_usage
from ..convert.gemini_sse_adapter import adapt_sse_stream
from ..routing.model_mapper import ModelMapper
from .base import (
    BaseBackend,
    BackendCapabilities,
    BackendResponse,
    CapabilityLossReason,
    RequestCapabilities,
    UsageInfo,
    _sanitize_headers_for_synthetic_response,
)
from .antigravity_token_manager import GoogleOAuthTokenManager
from .mixins import TokenBackendMixin
from .token_manager import TokenErrorKind

logger = logging.getLogger(__name__)


class AntigravityBackend(TokenBackendMixin, BaseBackend):
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
        token_manager = GoogleOAuthTokenManager(
            config.client_id, config.client_secret, config.refresh_token,
        )
        TokenBackendMixin.__init__(self, token_manager)
        BaseBackend.__init__(self, config.base_url, config.timeout_ms, failover_config)
        self._model_endpoint = config.model_endpoint
        self._model_mapper = model_mapper
        self._default_model = config.model_endpoint.removeprefix("models/")
        self._last_request_adaptations: list[str] = []
        self._safety_settings = config.safety_settings

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
            json_output=CompatibilityStatus.NATIVE,
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
        converted = convert_request(
            request_body,
            model=resolved_model,
            safety_settings=self._safety_settings,
        )
        gemini_body = converted.body
        self._last_request_adaptations = converted.adaptations
        token = await self._token_manager.get_token()
        new_headers = {
            "content-type": "application/json",
            "authorization": f"Bearer {token}",
        }
        logger.debug(
            "_prepare_request: model=%s → %s, endpoint=/models/%s:generateContent, adaptations=%s",
            request_body.get("model", "?"),
            resolved_model,
            resolved_model,
            converted.adaptations,
        )
        return gemini_body, new_headers

    def _mark_scope_error_if_needed(self, error_text: str) -> None:
        lowered = error_text.lower()
        if "access_token_scope_insufficient" not in lowered:
            return
        self._token_manager.mark_error(
            "Google access_token scope 不足，当前凭证不能调用 Generative Language API",
            kind=TokenErrorKind.INSUFFICIENT_SCOPE,
            needs_reauth=True,
        )

    def get_diagnostics(self) -> dict[str, Any]:
        result: dict[str, Any] = BaseBackend.get_diagnostics(self)
        result.update(self._get_token_diagnostics())
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
        resolved_model = self._last_resolved_model
        endpoint = f"/models/{resolved_model}:generateContent"

        logger.debug("send_message: POST %s", endpoint)
        response = await client.post(endpoint, json=body, headers=prepared_headers)
        logger.debug("send_message: status=%d", response.status_code)

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
        resolved_model = self._last_resolved_model
        endpoint = f"/models/{resolved_model}:streamGenerateContent?alt=sse"

        logger.debug("send_message_stream: POST %s", endpoint)

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
        await BaseBackend.close(self)
