"""HTTP 错误分类与请求能力画像提取."""

from __future__ import annotations

import json
from typing import Any

import httpx

from ..vendors.base import RequestCapabilities


def extract_error_payload_from_http_status(exc: httpx.HTTPStatusError) -> dict[str, Any] | None:
    response = exc.response
    if response is None or not response.content:
        return None
    try:
        payload = response.json()
    except (json.JSONDecodeError, UnicodeDecodeError, TypeError, ValueError):
        return None
    return payload if isinstance(payload, dict) else None


def is_semantic_rejection(
    *,
    status_code: int,
    error_type: str | None = None,
    error_message: str | None = None,
) -> bool:
    if status_code != 400:
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
