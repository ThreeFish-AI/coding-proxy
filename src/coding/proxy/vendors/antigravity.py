"""Google Antigravity Claude 供应商 — OAuth2 认证 + Gemini/v1internal 格式转换."""

from __future__ import annotations

import json
import logging
import uuid
from collections.abc import AsyncIterator
from typing import Any

import httpx

from ..compat.canonical import CompatibilityProfile, CompatibilityStatus
from ..config.schema import AntigravityConfig, FailoverConfig
from ..convert.anthropic_to_gemini import convert_request
from ..convert.gemini_sse_adapter import adapt_sse_stream
from ..convert.gemini_to_anthropic import convert_response, extract_usage
from ..routing.model_mapper import ModelMapper
from .base import (
    BaseVendor,
    CapabilityLossReason,
    RequestCapabilities,
    UsageInfo,
    VendorCapabilities,
    VendorResponse,
    _sanitize_headers_for_synthetic_response,
)

# GoogleOAuthTokenManager 已从 antigravity_token_manager.py 合并至本文件末尾
from .mixins import TokenBackendMixin
from .token_manager import BaseTokenManager, TokenAcquireError, TokenErrorKind

logger = logging.getLogger(__name__)

# v1internal 客户端指纹常量（与 Antigravity-Manager 对齐）
_V1INTERNAL_USER_AGENT = (
    "Antigravity/4.1.31 (Macintosh; Intel Mac OS X 10_15_7) "
    "Chrome/132.0.6834.160 Electron/39.2.3"
)
# Cloud Resource Manager API（用于自动发现 GCP project_id）
_CRM_PROJECTS_URL = "https://cloudresourcemanager.googleapis.com/v1/projects"
_V1INTERNAL_BASE_URL = "https://cloudcode-pa.googleapis.com/v1internal"


# ── Google OAuth2 Token 管理器（原 antigravity_token_manager.py） ──


class GoogleOAuthTokenManager(BaseTokenManager):
    """Google OAuth2 token 自动刷新管理.

    流程: refresh_token -> POST oauth2.googleapis.com/token -> access_token (~1 小时有效期)

    .. note::
        与 ``auth.providers.google.GoogleOAuthProvider.refresh()`` 的关系：
        - ``GoogleOAuthProvider`` = 完整 OAuth 流程（login + refresh + validate），用于 CLI 登录场景
        - ``GoogleOAuthTokenManager`` = 轻量级 token 刷新器，用于后端运行时自动续期
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
        if scope:
            from ..auth.providers.google import GoogleOAuthProvider

            if not GoogleOAuthProvider.has_required_scopes(scope):
                logger.warning(
                    "Google access_token scope 不完整（%s），"
                    "可能影响 Generative Language API 调用",
                    scope,
                )
        expires_in = data.get("expires_in", 3600)
        logger.info("Google OAuth token refreshed, expires_in=%ds", expires_in)
        return data["access_token"], float(expires_in)

    def update_refresh_token(self, new_refresh_token: str) -> None:
        """运行时热更新 refresh_token（重认证后调用）."""
        self._refresh_token = new_refresh_token
        self.invalidate()


class AntigravityVendor(TokenBackendMixin, BaseVendor):
    """Google Antigravity Claude API 供应商.

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
            config.client_id,
            config.client_secret,
            config.refresh_token,
        )
        TokenBackendMixin.__init__(self, token_manager)
        BaseVendor.__init__(self, config.base_url, config.timeout_ms, failover_config)
        self._model_endpoint = config.model_endpoint
        self._model_mapper = model_mapper
        self._default_model = config.model_endpoint.removeprefix("models/")
        self._last_request_adaptations: list[str] = []
        self._safety_settings = config.safety_settings
        # v1internal 协议字段
        self._project_id: str = config.project_id
        self._session_id: str = uuid.uuid4().hex[:16]
        self._message_count: int = 0
        # project_id 自动发现状态
        self._project_id_discovered: str = ""
        self._project_discovery_attempted: bool = False

    def get_name(self) -> str:
        return "antigravity"

    def _is_v1internal_mode(self) -> bool:
        """检测是否启用 v1internal 协议模式（与 Antigravity-Manager 对齐）."""
        return bool(self._effective_project_id) and "v1internal" in self._base_url

    @property
    def _effective_project_id(self) -> str:
        """返回有效的 project_id：显式配置优先，否则使用自动发现的值."""
        return self._project_id or self._project_id_discovered

    async def _discover_project_id(self, access_token: str) -> str:
        """通过 Cloud Resource Manager API 自动发现用户的 GCP project_id.

        利用已有的 cloud-platform OAuth scope 调用 CRM API 列出用户有权限的项目，
        选择合适的 project_id 后自动切换至 v1internal 模式。

        Args:
            access_token: 当前有效的 Google OAuth access_token

        Returns:
            发现到的 project_id；失败返回空字符串
        """
        if self._project_discovery_attempted:
            return self._project_id_discovered

        # 已手动配置则跳过
        if self._project_id:
            self._project_discovery_attempted = True
            return ""

        self._project_discovery_attempted = True
        client = self._get_client()
        try:
            response = await client.get(
                _CRM_PROJECTS_URL,
                headers={"authorization": f"Bearer {access_token}"},
            )
            response.raise_for_status()

            data = response.json()
            projects = data.get("projects", [])

            if not projects:
                logger.warning(
                    "GCP 项目自动发现完成但未找到任何项目，"
                    "回退至标准 GLA 模式。请手动配置 project_id 或确认账号已关联 GCP 项目。"
                )
                return ""

            # 选择策略：优先 ACTIVE 状态的项目
            selected = None
            for p in projects:
                if p.get("lifecycleState", "") == "ACTIVE":
                    selected = p
                    break
            if selected is None:
                for p in projects:
                    if p.get("lifecycleState", "") != "DELETE_REQUESTED":
                        selected = p
                        break
            if selected is None:
                selected = projects[0]

            project_id = selected.get("projectId", "")
            if not project_id:
                logger.warning(
                    "GCP 项目 '%s' 缺少 projectId 字段，跳过。",
                    selected.get("name", "unknown"),
                )
                return ""

            # 发现成功：原子性切换到 v1internal 模式
            old_base_url = self._base_url
            self._base_url = _V1INTERNAL_BASE_URL
            self._project_id_discovered = project_id

            # 重建 HTTP 客户端（base_url 是初始化参数）
            if self._client is not None and not self._client.is_closed:
                await self._client.aclose()
                self._client = None

            logger.info(
                "GCP 项目自动发现成功: project_id=%s, name=%s, "
                "已自动切换至 v1internal 协议模式",
                project_id,
                selected.get("name", "unknown"),
            )
            return project_id

        except httpx.HTTPStatusError as exc:
            logger.error(
                "GCP 项目自动发现 API 错误 (HTTP %d)，回退至标准 GLA 模式。%s",
                exc.response.status_code,
                exc,
            )
            return ""
        except (httpx.TimeoutException, httpx.ConnectError) as exc:
            logger.warning(
                "GCP 项目自动发现网络异常，回退至标准 GLA 模式。%s",
                exc,
            )
            return ""
        except Exception as exc:
            logger.error(
                "GCP 项目自动发现未知异常，回退至标准 GLA 模式。%s",
                exc,
            )
            return ""

    def get_capabilities(self) -> VendorCapabilities:
        return VendorCapabilities(
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
        self,
        request_caps: RequestCapabilities,
    ) -> tuple[bool, list[CapabilityLossReason]]:
        supported, reasons = super().supports_request(request_caps)
        if not supported:
            reasons = [
                reason
                for reason in reasons
                if reason
                not in {
                    CapabilityLossReason.THINKING,
                    CapabilityLossReason.TOOLS,
                    CapabilityLossReason.METADATA,
                }
            ]
        return len(reasons) == 0, reasons

    def map_model(self, model: str) -> str:
        resolved = self._model_mapper.map(
            model,
            vendor="antigravity",
            default=self._default_model,
        )
        self._last_requested_model = model
        self._last_resolved_model = resolved
        self._last_model_resolution_reason = (
            "configured_mapping"
            if resolved != self._default_model or model == self._default_model
            else "config_default_model_endpoint"
        )
        return resolved

    async def _prepare_request(
        self,
        request_body: dict[str, Any],
        headers: dict[str, str],
    ) -> tuple[dict[str, Any], dict[str, str]]:
        """转换 Anthropic 请求并注入 Google OAuth token.

        支持两种协议模式：
        - 标准 Gemini 模式：直接发送 Gemini 请求体到 generativelanguage.googleapis.com
        - v1internal 模式：将请求体包裹在 v1internal 信封中发送到 cloudcode-pa.googleapis.com
        """
        resolved_model = self.map_model(request_body.get("model", "unknown"))
        converted = convert_request(
            request_body,
            model=resolved_model,
            safety_settings=self._safety_settings,
        )
        gemini_body = converted.body
        self._last_request_adaptations = converted.adaptations
        token = await self._token_manager.get_token()

        # 懒加载：未配置 project_id 时自动发现并切换 v1internal 模式
        if not self._project_id and not self._project_discovery_attempted:
            discovered = await self._discover_project_id(token)
            if discovered:
                logger.info("已自动启用 v1internal 协议模式（project_id=%s）", discovered)
            else:
                logger.info(
                    "无法自动发现 GCP project_id，继续使用标准 GLA 模式。"
                    "如需启用 v1internal 协议，请在配置中手动指定 project_id。"
                )

        if self._is_v1internal_mode():
            return self._prepare_v1internal_request(gemini_body, resolved_model, token)

        new_headers = {
            "content-type": "application/json",
            "authorization": f"Bearer {token}",
        }
        logger.debug(
            "_prepare_request: model=%s -> %s, endpoint=/models/%s:generateContent, adaptations=%s",
            request_body.get("model", "?"),
            resolved_model,
            resolved_model,
            converted.adaptations,
        )
        return gemini_body, new_headers

    def _prepare_v1internal_request(
        self,
        gemini_body: dict[str, Any],
        resolved_model: str,
        token: str,
    ) -> tuple[dict[str, Any], dict[str, str]]:
        """将 Gemini 请求体包裹在 v1internal 信封中（与 Antigravity-Manager 对齐）.

        v1internal 是 Google Cloud Code 内部 API 协议，接受与标准 GLA 相同的 OAuth 凭证，
        但需要特定的信封格式和客户端指纹 Headers。
        """
        self._message_count += 1
        envelope = {
            "project": self._effective_project_id,
            "requestId": f"agent/antigravity/{self._session_id}/{self._message_count}",
            "request": gemini_body,
            "model": resolved_model,
            "userAgent": "antigravity",
            "requestType": "agent",
        }
        new_headers = {
            "content-type": "application/json",
            "authorization": f"Bearer {token}",
            "user-agent": _V1INTERNAL_USER_AGENT,
            "x-client-name": "antigravity",
            "x-client-version": "4.1.31",
        }
        logger.debug(
            "_prepare_v1internal_request: model=%s -> %s, project=%s, requestId=%s",
            resolved_model,
            resolved_model,
            self._effective_project_id,
            envelope["requestId"],
        )
        return envelope, new_headers

    def _mark_scope_error_if_needed(self, error_text: str) -> None:
        lowered = error_text.lower()
        if "access_token_scope_insufficient" not in lowered:
            return
        logger.error(
            "Generative Language API 拒绝访问（ACCESS_TOKEN_SCOPE_INSUFFICIENT）。"
            "如使用标准 GLA 端点，请确认凭证已授权正确的 scope；"
            "建议切换至 v1internal 协议（配置 project_id）以获得更好的兼容性。"
        )
        self._token_manager.mark_error(
            "Google access_token scope 不足，当前凭证不能调用 Generative Language API",
            kind=TokenErrorKind.INSUFFICIENT_SCOPE,
            needs_reauth=True,
        )

    def get_diagnostics(self) -> dict[str, Any]:
        result: dict[str, Any] = BaseVendor.get_diagnostics(self)
        result.update(self._get_token_diagnostics())
        if self._last_request_adaptations:
            result["request_adaptations"] = self._last_request_adaptations
        if self._last_requested_model:
            result["requested_model"] = self._last_requested_model
        if self._last_resolved_model:
            result["resolved_model"] = self._last_resolved_model
        if self._last_model_resolution_reason:
            result["model_resolution_reason"] = self._last_model_resolution_reason
        # project_id 发现诊断
        result["project_id_source"] = (
            "configured" if self._project_id
            else ("discovered" if self._project_id_discovered else "none")
        )
        if self._project_id_discovered:
            result["discovered_project_id"] = self._project_id_discovered
        result["is_v1internal_mode"] = self._is_v1internal_mode()
        return result

    async def send_message(
        self,
        request_body: dict[str, Any],
        headers: dict[str, str],
    ) -> VendorResponse:
        """覆写: Gemini / v1internal 端点 + 响应逆转换."""
        body, prepared_headers = await self._prepare_request(request_body, headers)
        client = self._get_client()
        resolved_model = self._last_resolved_model
        endpoint = (
            ":generateContent"
            if self._is_v1internal_mode()
            else f"/models/{resolved_model}:generateContent"
        )

        logger.debug("send_message: POST %s", endpoint)
        response = await client.post(endpoint, json=body, headers=prepared_headers)
        logger.debug("send_message: status=%d", response.status_code)

        if response.status_code >= 400:
            self._on_error_status(response.status_code)
            self._mark_scope_error_if_needed(response.text)
            return VendorResponse(
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

        return VendorResponse(
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
        """覆写: Gemini / v1internal SSE 流 -> Anthropic SSE 流适配."""
        body, prepared_headers = await self._prepare_request(request_body, headers)
        client = self._get_client()
        resolved_model = self._last_resolved_model
        endpoint = (
            ":streamGenerateContent?alt=sse"
            if self._is_v1internal_mode()
            else f"/models/{resolved_model}:streamGenerateContent?alt=sse"
        )

        logger.debug("send_message_stream: POST %s", endpoint)

        async with client.stream(
            "POST",
            endpoint,
            json=body,
            headers=prepared_headers,
        ) as response:
            if response.status_code >= 400:
                self._on_error_status(response.status_code)
                error_body = await response.aread()
                self._mark_scope_error_if_needed(
                    error_body.decode("utf-8", errors="ignore"),
                )
                logger.warning(
                    "%s stream error: status=%d body=%s",
                    self.get_name(),
                    response.status_code,
                    error_body[:500],
                )
                raise httpx.HTTPStatusError(
                    f"{self.get_name()} API error: {response.status_code}",
                    request=response.request,
                    response=httpx.Response(
                        response.status_code,
                        content=error_body,
                        headers=_sanitize_headers_for_synthetic_response(
                            response.headers
                        ),
                        request=response.request,
                    ),
                )

            model = request_body.get("model", "unknown")
            async for chunk in adapt_sse_stream(response.aiter_bytes(), model):
                yield chunk

    async def close(self) -> None:
        await self._token_manager.close()
        await BaseVendor.close(self)


# 向后兼容别名
AntigravityBackend = AntigravityVendor
