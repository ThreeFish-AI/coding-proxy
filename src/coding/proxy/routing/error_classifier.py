"""HTTP 错误分类与请求能力画像提取."""

from __future__ import annotations

import json
from typing import Any

import httpx

from ..vendors.base import RequestCapabilities

# ── 结构性验证错误标记 ──────────────────────────────────────
# 这些标记指示的是消息结构不合规（如 tool_result 角色错位、消息交替违规），
# 而非模型无法处理的语义内容。结构性错误不应触发级联故障转移，
# 因为将同样的畸形请求转发到下一层供应商只会重复失败。
_STRUCTURAL_ERROR_MARKERS: frozenset[str] = frozenset(
    {
        "tool_result blocks can only be",
        "tool_use blocks can only be",
        "messages must alternate",
        "messages with role",
        "thinking blocks can only be",
        "content blocks can only be",
    }
)


def extract_error_payload_from_http_status(
    exc: httpx.HTTPStatusError,
) -> dict[str, Any] | None:
    response = exc.response
    if response is None or not response.content:
        return None
    try:
        payload = response.json()
    except (json.JSONDecodeError, UnicodeDecodeError, TypeError, ValueError):
        return None
    return payload if isinstance(payload, dict) else None


def is_structural_validation_error(
    *,
    status_code: int,
    error_message: str | None = None,
) -> bool:
    """检测是否为结构性验证错误（不应触发故障转移）.

    结构性错误指示请求的消息格式不合规（如 tool_result 角色错位），
    将同样的畸形请求转发到下一层供应商不会解决问题。
    与语义拒绝（模型无法处理某些内容）不同，结构性错误应直接返回客户端。

    Returns:
        True 如果是结构性验证错误。
    """
    if status_code != 400:
        return False
    normalized_message = (error_message or "").lower()
    return any(marker.lower() in normalized_message for marker in _STRUCTURAL_ERROR_MARKERS)


def is_semantic_rejection(
    *,
    status_code: int,
    error_type: str | None = None,
    error_message: str | None = None,
) -> bool:
    if status_code != 400:
        return False

    # 结构性验证错误不应被视为语义拒绝
    if is_structural_validation_error(
        status_code=status_code, error_message=error_message
    ):
        return False

    normalized_type = (error_type or "").strip().lower()
    if normalized_type == "invalid_request_error":
        return True
    normalized_message = (error_message or "").lower()
    return any(
        marker in normalized_message
        for marker in (
            "invalid_request_error",
            "should match pattern",
            "validation",
            "tool_use_id",
            "server_tool_use",
            "tool_result",
            "can only be in",
            "bad request",  # 覆盖 Copilot 等返回纯文本 "Bad Request" 的场景
        )
    )


def build_request_capabilities(body: dict[str, Any]) -> RequestCapabilities:
    """从请求体提取能力画像."""
    has_images = False
    for msg in body.get("messages", []):
        content = msg.get("content")
        if not isinstance(content, list):
            continue
        if any(
            isinstance(block, dict) and block.get("type") == "image"
            for block in content
        ):
            has_images = True
            break

    return RequestCapabilities(
        has_tools=bool(body.get("tools") or body.get("tool_choice")),
        has_thinking=bool(body.get("thinking") or body.get("extended_thinking")),
        has_images=has_images,
        has_metadata=bool(body.get("metadata")),
    )
