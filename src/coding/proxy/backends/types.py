"""后端类型定义 — 数据模型、常量与工具函数.

从 base.py 正交提取，遵循单一职责原则：
- 常量：代理转发头过滤规则、合成响应头清洗规则
- 工具函数：JSON 解析、错误消息提取、响应头清洗
- 数据类型：UsageInfo / CapabilityLossReason / RequestCapabilities /
           BackendCapabilities / BackendResponse / NoCompatibleBackendError
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

import httpx

# 代理转发时应跳过的 hop-by-hop 请求头
PROXY_SKIP_HEADERS = {"host", "content-length", "transfer-encoding", "connection"}

# 构造合成 Response 时需移除的头部（避免 httpx 二次解压已解压内容）
RESPONSE_SANITIZE_SKIP_HEADERS = {"content-encoding", "content-length", "transfer-encoding"}

# 向后兼容别名
_SYNTHETIC_RESPONSE_SKIP_HEADERS = RESPONSE_SANITIZE_SKIP_HEADERS


def _sanitize_headers_for_synthetic_response(headers: httpx.Headers) -> dict[str, str]:
    """移除 content-encoding 等头部，避免合成 httpx.Response 时触发二次解压."""
    return {k: v for k, v in headers.items() if k.lower() not in RESPONSE_SANITIZE_SKIP_HEADERS}


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
