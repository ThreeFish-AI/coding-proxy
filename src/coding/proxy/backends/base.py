"""后端抽象基类 — 模板方法模式."""

from __future__ import annotations

import json
import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, AsyncIterator

import httpx

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

# 代理转发时应跳过的 hop-by-hop 请求头
PROXY_SKIP_HEADERS = {"host", "content-length", "transfer-encoding", "connection"}

# 构造合成 Response 时需移除的头部（避免 httpx 二次解压已解压内容）
_SYNTHETIC_RESPONSE_SKIP_HEADERS = {"content-encoding", "content-length", "transfer-encoding"}


def _sanitize_headers_for_synthetic_response(headers: httpx.Headers) -> dict[str, str]:
    """移除 content-encoding 等头部，避免合成 httpx.Response 时触发二次解压."""
    return {k: v for k, v in headers.items() if k.lower() not in _SYNTHETIC_RESPONSE_SKIP_HEADERS}


def _decode_json_body(response: httpx.Response) -> dict[str, Any] | list[Any] | None:
    """安全解析 JSON 响应.

    若 content-type 未声明 JSON 或内容非法，返回 None，而不是抛 JSONDecodeError。
    """
    if not response.content:
        return None

    content_type = response.headers.get("content-type", "").lower()
    if "json" not in content_type:
        try:
            return json.loads(response.content)
        except (json.JSONDecodeError, UnicodeDecodeError, TypeError):
            return None

    try:
        return response.json()
    except (json.JSONDecodeError, UnicodeDecodeError, TypeError):
        return None


def _extract_error_message(response: httpx.Response, resp_body: dict[str, Any] | list[Any] | None) -> str | None:
    if isinstance(resp_body, dict):
        error = resp_body.get("error")
        if isinstance(error, dict):
            return error.get("message")
        if isinstance(error, str):
            return error
        message = resp_body.get("message")
        if isinstance(message, str):
            return message

    if not response.content:
        return None
    text = response.text.strip()
    return text[:500] if text else None


@dataclass
class UsageInfo:
    """一次调用的 Token 用量."""

    input_tokens: int = 0
    output_tokens: int = 0
    cache_creation_tokens: int = 0
    cache_read_tokens: int = 0
    request_id: str = ""


class CapabilityLossReason(Enum):
    """请求语义与后端能力不匹配的原因."""

    TOOLS = "tools"
    THINKING = "thinking"
    IMAGES = "images"
    VENDOR_TOOLS = "vendor_tools"
    METADATA = "metadata"


@dataclass(frozen=True)
class RequestCapabilities:
    """一次请求实际使用到的能力画像."""

    has_tools: bool = False
    has_thinking: bool = False
    has_images: bool = False
    has_metadata: bool = False


@dataclass(frozen=True)
class BackendCapabilities:
    """后端能力声明."""

    supports_tools: bool = True
    supports_thinking: bool = True
    supports_images: bool = True
    emits_vendor_tool_events: bool = False
    supports_metadata: bool = True


@dataclass
class BackendResponse:
    """后端响应结果."""

    status_code: int = 200
    usage: UsageInfo = field(default_factory=UsageInfo)
    is_streaming: bool = False
    raw_body: bytes = b"{}"
    error_type: str | None = None
    error_message: str | None = None
    model_served: str | None = None
    response_headers: dict[str, str] = field(default_factory=dict)


class NoCompatibleBackendError(RuntimeError):
    """当前请求没有可安全承接的后端."""

    def __init__(self, message: str, *, reasons: list[str] | None = None) -> None:
        super().__init__(message)
        self.reasons = reasons or []


class BaseBackend(ABC):
    """后端抽象基类，提供 HTTP 客户端管理和请求模板."""

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
        """返回后端名称（用于日志）."""

    def map_model(self, model: str) -> str:
        """将请求模型名映射为后端实际使用的模型名.

        默认实现为恒等映射（无转换）.
        有模型映射需求的后端（如 Zhipu）应覆写此方法.
        """
        return model

    def get_capabilities(self) -> BackendCapabilities:
        """返回后端能力声明.

        默认视为 Anthropic 兼容后端。
        """
        return BackendCapabilities()

    def get_compatibility_profile(self) -> CompatibilityProfile:
        caps = self.get_capabilities()
        native_or_unsafe = lambda supported: CompatibilityStatus.NATIVE if supported else CompatibilityStatus.UNSAFE
        return CompatibilityProfile(
            thinking=native_or_unsafe(caps.supports_thinking),
            tool_calling=native_or_unsafe(caps.supports_tools),
            tool_streaming=CompatibilityStatus.SIMULATED if caps.supports_tools else CompatibilityStatus.UNSAFE,
            mcp_tools=CompatibilityStatus.UNKNOWN,
            images=native_or_unsafe(caps.supports_images),
            metadata=native_or_unsafe(caps.supports_metadata),
            json_output=CompatibilityStatus.UNKNOWN,
            usage_tokens=CompatibilityStatus.SIMULATED,
        )

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
        """判断后端是否能无损承接该请求."""
        backend_caps = self.get_capabilities()
        reasons: list[CapabilityLossReason] = []

        if request_caps.has_tools and not backend_caps.supports_tools:
            reasons.append(CapabilityLossReason.TOOLS)
        if request_caps.has_thinking and not backend_caps.supports_thinking:
            reasons.append(CapabilityLossReason.THINKING)
        if request_caps.has_images and not backend_caps.supports_images:
            reasons.append(CapabilityLossReason.IMAGES)
        if request_caps.has_metadata and not backend_caps.supports_metadata:
            reasons.append(CapabilityLossReason.METADATA)
        if request_caps.has_tools and backend_caps.emits_vendor_tool_events:
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

    def _on_error_status(self, status_code: int) -> None:
        """响应错误状态码时的钩子（如 token 失效标记）."""

    def get_diagnostics(self) -> dict[str, Any]:
        """返回后端运行时诊断信息."""
        diagnostics: dict[str, Any] = {}
        if self._compat_trace is not None:
            diagnostics["compat"] = self._compat_trace.to_dict()
        return diagnostics

    def should_trigger_failover(self, status_code: int, body: dict[str, Any] | None) -> bool:
        """基于 FailoverConfig 的通用故障转移判断.

        无 failover_config 时返回 False（终端后端默认行为）.
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
        """检查后端健康状态（轻量级探测）.

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
    ) -> BackendResponse:
        """发送非流式消息请求."""
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
            return BackendResponse(
                status_code=response.status_code,
                raw_body=raw_content,
                error_type=resp_body.get("error", {}).get("type") if isinstance(resp_body, dict) and isinstance(resp_body.get("error"), dict) else None,
                error_message=_extract_error_message(response, resp_body),
                response_headers=dict(response.headers),
            )

        usage = resp_body.get("usage", {}) if isinstance(resp_body, dict) else {}
        return BackendResponse(
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
