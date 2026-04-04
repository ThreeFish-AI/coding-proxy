"""供应商抽象基类 — 模板方法模式."""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from typing import Any, AsyncIterator

import httpx

# 从 model/ 模块正交导入所有类型、常量与工具函数，并 re-export 以保持向后兼容
from ..model.vendor import (  # noqa: F401
    VendorCapabilities,
    VendorResponse,
    CapabilityLossReason,
    CopilotExchangeDiagnostics,
    CopilotMisdirectedRequest,
    CopilotModelCatalog,
    NoCompatibleVendorError,
    RequestCapabilities,
    UsageInfo,
    decode_json_body,
    extract_error_message,
    sanitize_headers_for_synthetic_response,
)
from ..model.constants import (  # noqa: F401
    PROXY_SKIP_HEADERS,
    RESPONSE_SANITIZE_SKIP_HEADERS,
)

# ── 废弃别名（向后兼容旧名称） ──────────────────────────
_decode_json_body = decode_json_body
_extract_error_message = extract_error_message
_sanitize_headers_for_synthetic_response = sanitize_headers_for_synthetic_response

from ..compat.canonical import (
    CanonicalRequest,
    CompatibilityDecision,
    CompatibilityProfile,
    CompatibilityStatus,
    CompatibilityTrace,
)
from ..compat.session_store import CompatSessionRecord
from ..config.schema import FailoverConfig

logger = logging.getLogger(__name__)


class BaseVendor(ABC):
    """供应商抽象基类，提供 HTTP 客户端管理和请求模板."""

    def __init__(
        self,
        base_url: str,
        timeout_ms: int,
        failover_config: FailoverConfig | None = None,
    ) -> None:
        self._base_url = base_url
        self._timeout_ms = timeout_ms
        self._failover_config = failover_config
        self._client: httpx.AsyncClient | None = None
        self._compat_trace: CompatibilityTrace | None = None
        self._compat_session_record: CompatSessionRecord | None = None

    def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                base_url=self._base_url,
                timeout=httpx.Timeout(self._timeout_ms / 1000.0),
            )
        return self._client

    @abstractmethod
    def get_name(self) -> str:
        """返回供应商名称（用于日志）."""

    def map_model(self, model: str) -> str:
        """将请求模型名映射为供应商实际使用的模型名.

        默认实现为恒等映射（无转换）.
        有模型映射需求的供应商（如 Zhipu）应覆写此方法.
        """
        return model

    def get_capabilities(self) -> VendorCapabilities:
        """返回供应商能力声明.

        默认视为 Anthropic 兼容供应商。
        """
        return VendorCapabilities()

    def get_compatibility_profile(self) -> CompatibilityProfile:
        caps = self.get_capabilities()
        return CompatibilityProfile(
            thinking=self._compat_status_from_bool(caps.supports_thinking),
            tool_calling=self._compat_status_from_bool(caps.supports_tools),
            tool_streaming=CompatibilityStatus.SIMULATED if caps.supports_tools else CompatibilityStatus.UNSAFE,
            mcp_tools=CompatibilityStatus.UNKNOWN,
            images=self._compat_status_from_bool(caps.supports_images),
            metadata=self._compat_status_from_bool(caps.supports_metadata),
            json_output=CompatibilityStatus.UNKNOWN,
            usage_tokens=CompatibilityStatus.SIMULATED,
        )

    @staticmethod
    def _compat_status_from_bool(supported: bool) -> CompatibilityStatus:
        """将布尔能力映射为兼容性状态."""
        return CompatibilityStatus.NATIVE if supported else CompatibilityStatus.UNSAFE

    def make_compatibility_decision(self, request: CanonicalRequest) -> CompatibilityDecision:
        profile = self.get_compatibility_profile()
        simulation_actions: list[str] = []
        unsupported: list[str] = []

        if request.thinking.enabled and profile.thinking is CompatibilityStatus.SIMULATED:
            simulation_actions.append("thinking_simulation")
        elif request.thinking.enabled and profile.thinking not in {
            CompatibilityStatus.NATIVE, CompatibilityStatus.SIMULATED,
        }:
            unsupported.append("thinking")

        if request.tool_names and profile.tool_calling is CompatibilityStatus.SIMULATED:
            simulation_actions.append("tool_calling_simulation")
        elif request.tool_names and profile.tool_calling not in {
            CompatibilityStatus.NATIVE, CompatibilityStatus.SIMULATED,
        }:
            unsupported.append("tools")

        if request.metadata and profile.metadata is CompatibilityStatus.SIMULATED:
            simulation_actions.append("metadata_projection")
        elif request.metadata and profile.metadata not in {
            CompatibilityStatus.NATIVE, CompatibilityStatus.SIMULATED,
        }:
            unsupported.append("metadata")

        if request.supports_json_output and profile.json_output is CompatibilityStatus.SIMULATED:
            simulation_actions.append("json_output_projection")
        elif request.supports_json_output and profile.json_output not in {
            CompatibilityStatus.NATIVE, CompatibilityStatus.SIMULATED,
        }:
            unsupported.append("response_format")

        if unsupported:
            return CompatibilityDecision(
                status=CompatibilityStatus.UNSAFE,
                simulation_actions=simulation_actions,
                unsupported_semantics=unsupported,
            )
        if simulation_actions:
            return CompatibilityDecision(
                status=CompatibilityStatus.SIMULATED,
                simulation_actions=simulation_actions,
            )
        return CompatibilityDecision(status=CompatibilityStatus.NATIVE)

    def set_compat_context(
        self,
        *,
        trace: CompatibilityTrace,
        session_record: CompatSessionRecord | None,
    ) -> None:
        self._compat_trace = trace
        self._compat_session_record = session_record

    def get_compat_trace(self) -> CompatibilityTrace | None:
        return self._compat_trace

    def supports_request(
        self, request_caps: RequestCapabilities,
    ) -> tuple[bool, list[CapabilityLossReason]]:
        """判断供应商是否能无损承接该请求."""
        vendor_caps = self.get_capabilities()
        reasons: list[CapabilityLossReason] = []

        if request_caps.has_tools and not vendor_caps.supports_tools:
            reasons.append(CapabilityLossReason.TOOLS)
        if request_caps.has_thinking and not vendor_caps.supports_thinking:
            reasons.append(CapabilityLossReason.THINKING)
        if request_caps.has_images and not vendor_caps.supports_images:
            reasons.append(CapabilityLossReason.IMAGES)
        if request_caps.has_metadata and not vendor_caps.supports_metadata:
            reasons.append(CapabilityLossReason.METADATA)
        if request_caps.has_tools and vendor_caps.emits_vendor_tool_events:
            reasons.append(CapabilityLossReason.VENDOR_TOOLS)

        return len(reasons) == 0, reasons

    @abstractmethod
    async def _prepare_request(
        self,
        request_body: dict[str, Any],
        headers: dict[str, str],
    ) -> tuple[dict[str, Any], dict[str, str]]:
        """准备请求体和请求头，由子类实现差异化逻辑（支持异步操作）."""

    def _get_endpoint(self) -> str:
        """返回 API 端点路径（默认 /v1/messages）."""
        return "/v1/messages"

    # ── 响应处理钩子 ──────────────────────────────────────

    def _pre_send_check(self, request_body: dict[str, Any], headers: dict[str, str]) -> None:
        """发送前检查钩子. 子类可覆写以实现快速失败（如缺少 API key）.

        默认实现为空操作（no-op）.
        """

    def _normalize_error_response(
        self,
        status_code: int,
        response: httpx.Response,
        vendor_resp: VendorResponse,
    ) -> VendorResponse:
        """错误响应归一化钩子. 子类可覆写以定制错误格式.

        默认实现直接返回原始 vendor_resp（无修改，透传行为）.
        """
        return vendor_resp

    # ── 生命周期钩子 ─────────────────────────────────────

    def _on_error_status(self, status_code: int) -> None:
        """响应错误状态码时的钩子（如 token 失效标记）."""

    def get_diagnostics(self) -> dict[str, Any]:
        """返回供应商运行时诊断信息."""
        diagnostics: dict[str, Any] = {}
        if self._compat_trace is not None:
            diagnostics["compat"] = self._compat_trace.to_dict()
        return diagnostics

    def should_trigger_failover(self, status_code: int, body: dict[str, Any] | None) -> bool:
        """基于 FailoverConfig 的通用故障转移判断.

        无 failover_config 时返回 False（终端供应商默认行为）.
        """
        if self._failover_config is None:
            return False
        if status_code not in self._failover_config.status_codes:
            return False
        if body and "error" in body:
            error = body["error"]
            error_type = error.get("type", "")
            error_message = error.get("message", "").lower()
            if error_type in self._failover_config.error_types:
                return True
            for pattern in self._failover_config.error_message_patterns:
                if pattern.lower() in error_message:
                    return True
        # 429/503 即使无法解析 body 也触发故障转移
        return status_code in (429, 503)

    async def check_health(self) -> bool:
        """检查供应商健康状态（轻量级探测）.

        默认实现返回 True（假定健康）。
        子类可覆写以实现低成本的认证层检查。
        """
        return True

    async def send_message_stream(
        self,
        request_body: dict[str, Any],
        headers: dict[str, str],
    ) -> AsyncIterator[bytes]:
        """发送消息并返回 SSE 字节流."""
        self._pre_send_check(request_body, headers)
        body, prepared_headers = await self._prepare_request(request_body, headers)
        client = self._get_client()
        endpoint = self._get_endpoint()

        async with client.stream(
            "POST",
            endpoint,
            json=body,
            headers=prepared_headers,
        ) as response:
            if response.status_code >= 400:
                self._on_error_status(response.status_code)
                error_body = await response.aread()
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
            async for chunk in response.aiter_bytes():
                yield chunk

    async def send_message(
        self,
        request_body: dict[str, Any],
        headers: dict[str, str],
    ) -> VendorResponse:
        """发送非流式消息请求."""
        self._pre_send_check(request_body, headers)
        body, prepared_headers = await self._prepare_request(request_body, headers)
        client = self._get_client()
        endpoint = self._get_endpoint()

        response = await client.post(
            endpoint,
            json=body,
            headers=prepared_headers,
        )

        raw_content = response.content
        resp_body = _decode_json_body(response)

        if response.status_code >= 400:
            self._on_error_status(response.status_code)
            vendor_resp = VendorResponse(
                status_code=response.status_code,
                raw_body=raw_content,
                error_type=resp_body.get("error", {}).get("type") if isinstance(resp_body, dict) and isinstance(resp_body.get("error"), dict) else None,
                error_message=_extract_error_message(response, resp_body),
                response_headers=dict(response.headers),
            )
            return self._normalize_error_response(response.status_code, response, vendor_resp)

        usage = resp_body.get("usage", {}) if isinstance(resp_body, dict) else {}
        return VendorResponse(
            status_code=response.status_code,
            raw_body=raw_content,
            usage=UsageInfo(
                input_tokens=usage.get("input_tokens", 0) or usage.get("prompt_tokens", 0),
                output_tokens=usage.get("output_tokens", 0) or usage.get("completion_tokens", 0),
                cache_creation_tokens=usage.get("cache_creation_input_tokens", 0),
                cache_read_tokens=usage.get("cache_read_input_tokens", 0),
                request_id=resp_body.get("id", "") if isinstance(resp_body, dict) else "",
            ),
            model_served=resp_body.get("model") if isinstance(resp_body, dict) else None,
        )

    async def close(self) -> None:
        if self._client and not self._client.is_closed:
            await self._client.aclose()


# ═══════════════════════════════════════════════════════════════
# 向后兼容别名（v2 移除）
# ═══════════════════════════════════════════════════════════════

BaseBackend = BaseVendor
NoCompatibleBackendError = NoCompatibleVendorError
