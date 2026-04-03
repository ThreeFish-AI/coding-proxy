"""HTTP 错误响应构造工具."""

from __future__ import annotations

import json
from typing import Any

import httpx
from fastapi import Response


def json_error_response(
    status_code: int,
    *,
    error_type: str,
    message: str,
    details: list[str] | None = None,
) -> Response:
    """构造 JSON 格式的错误响应."""
    payload: dict[str, Any] = {
        "error": {
            "type": error_type,
            "message": message,
        }
    }
    if details:
        payload["error"]["details"] = details
    return Response(
        content=json.dumps(payload, ensure_ascii=False).encode(),
        status_code=status_code,
        media_type="application/json",
    )


def stream_error_event(error_type: str, message: str, details: list[str] | None = None) -> bytes:
    """构造 SSE 格式的错误事件."""
    payload: dict[str, Any] = {
        "type": "error",
        "error": {
            "type": error_type,
            "message": message,
        },
    }
    if details:
        payload["error"]["details"] = details
    return f"event: error\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n".encode()


def extract_stream_http_error(exc: httpx.HTTPStatusError) -> tuple[str, str]:
    """从 HTTPStatusError 中提取错误类型和消息."""
    response = exc.response
    if response is None:
        return "api_error", str(exc)

    try:
        payload = response.json() if response.content else None
    except (json.JSONDecodeError, UnicodeDecodeError, TypeError, ValueError):
        payload = None

    if isinstance(payload, dict):
        error = payload.get("error")
        if isinstance(error, dict):
            error_type = error.get("type")
            message = error.get("message")
            if isinstance(error_type, str) and isinstance(message, str) and message:
                return error_type, message
        message = payload.get("message")
        if isinstance(message, str) and message:
            return "api_error", message

    text = response.text.strip() if response.content else ""
    if text:
        return "api_error", text[:500]
    return "api_error", str(exc)
