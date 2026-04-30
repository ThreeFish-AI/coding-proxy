"""供应商核心数据模型 — 类型定义、常量引用与工具函数.

从本模块正交提取，遵循单一职责原则：
- 数据类型：UsageInfo / CapabilityLossReason / RequestCapabilities /
           VendorCapabilities / VendorResponse / NoCompatibleVendorError
- Copilot 诊断数据类：CopilotMisdirectedRequest / CopilotExchangeDiagnostics /
                      CopilotModelCatalog
- 工具函数：JSON 解析、错误消息提取、响应头清洗
- 常量引用：自 :mod:`coding.proxy.model.constants` 重导出
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

import httpx

from .constants import RESPONSE_SANITIZE_SKIP_HEADERS

# ═══════════════════════════════════════════════════════════════
# 工具函数（公开 API，去除原 _ 前缀）
# ═══════════════════════════════════════════════════════════════


def sanitize_headers_for_synthetic_response(headers: httpx.Headers) -> dict[str, str]:
    """移除 content-encoding 等头部，避免合成 httpx.Response 时触发二次解压."""
    return {
        k: v
        for k, v in headers.items()
        if k.lower() not in RESPONSE_SANITIZE_SKIP_HEADERS
    }


def decode_json_body(response: httpx.Response) -> dict[str, Any] | list[Any] | None:
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


def extract_error_message(
    response: httpx.Response, resp_body: dict[str, Any] | list[Any] | None
) -> str | None:
    """从 HTTP 响应中提取可读错误消息."""
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


# ═══════════════════════════════════════════════════════════════
# 供应商核心数据类型
# ═══════════════════════════════════════════════════════════════


@dataclass
class UsageInfo:
    """一次调用的 Token 用量."""

    input_tokens: int = 0
    output_tokens: int = 0
    cache_creation_tokens: int = 0
    cache_read_tokens: int = 0
    request_id: str = ""


class CapabilityLossReason(Enum):
    """请求语义与供应商能力不匹配的原因."""

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
class VendorCapabilities:
    """供应商能力声明."""

    supports_tools: bool = True
    supports_thinking: bool = True
    supports_images: bool = True
    emits_vendor_tool_events: bool = False
    supports_metadata: bool = True


@dataclass
class VendorResponse:
    """供应商响应结果."""

    status_code: int = 200
    usage: UsageInfo = field(default_factory=UsageInfo)
    is_streaming: bool = False
    raw_body: bytes = b"{}"
    error_type: str | None = None
    error_message: str | None = None
    model_served: str | None = None
    response_headers: dict[str, str] = field(default_factory=dict)


class NoCompatibleVendorError(RuntimeError):
    """当前请求没有可安全承接的供应商."""

    def __init__(self, message: str, *, reasons: list[str] | None = None) -> None:
        super().__init__(message)
        self.reasons = reasons or []


# ═══════════════════════════════════════════════════════════════
# Copilot 诊断数据类
# ═══════════════════════════════════════════════════════════════


@dataclass
class CopilotMisdirectedRequest:
    """Copilot 421 Misdirected 请求诊断载体."""

    base_url: str
    status_code: int
    request: Any  # httpx.Request (avoid circular import at module level)
    headers: Any  # httpx.Headers
    body: bytes


@dataclass
class CopilotExchangeDiagnostics:
    """最近一次 Copilot token 交换的运行时诊断."""

    raw_shape: str = ""
    token_field: str = ""
    expires_in_seconds: int = 0
    expires_at_unix: int = 0
    capabilities: dict[str, Any] = field(default_factory=dict)
    updated_at_unix: int = 0

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {}
        if self.raw_shape:
            data["raw_shape"] = self.raw_shape
        if self.token_field:
            data["token_field"] = self.token_field
        if self.expires_in_seconds:
            data["expires_in_seconds"] = self.expires_in_seconds
        if self.expires_at_unix:
            data["ttl_seconds"] = max(
                self.expires_at_unix - int(__import__("time").time()), 0
            )
        if self.capabilities:
            data["capabilities"] = self.capabilities
        if self.updated_at_unix:
            data["updated_at"] = self.updated_at_unix
        return data


@dataclass
class CopilotModelCatalog:
    """Copilot 模型目录缓存."""

    available_models: list[str] = field(default_factory=list)
    fetched_at_unix: int = 0

    def age_seconds(self) -> int | None:
        if not self.fetched_at_unix:
            return None
        return max(int(__import__("time").time()) - self.fetched_at_unix, 0)


# ═══════════════════════════════════════════════════════════════
# 向后兼容别名（v2 移除）
# ═══════════════════════════════════════════════════════════════

BackendCapabilities = VendorCapabilities
BackendResponse = VendorResponse
NoCompatibleBackendError = NoCompatibleVendorError

__all__ = [
    # 新命名
    "VendorCapabilities",
    "VendorResponse",
    "NoCompatibleVendorError",
    # 向后兼容别名
    "BackendCapabilities",
    "BackendResponse",
    "NoCompatibleBackendError",
    # 通用类型（不变）
    "UsageInfo",
    "CapabilityLossReason",
    "RequestCapabilities",
    # Copilot 诊断类
    "CopilotExchangeDiagnostics",
    "CopilotMisdirectedRequest",
    "CopilotModelCatalog",
    # 工具函数
    "decode_json_body",
    "extract_error_message",
    "sanitize_headers_for_synthetic_response",
]
