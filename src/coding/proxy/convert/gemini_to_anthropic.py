"""Google Gemini/Vertex AI 响应 → Anthropic Messages API 格式转换."""

from __future__ import annotations

import uuid
from typing import Any

# Gemini finishReason → Anthropic stop_reason
_FINISH_REASON_MAP = {
    "STOP": "end_turn",
    "MAX_TOKENS": "max_tokens",
    "SAFETY": "end_turn",
    "RECITATION": "end_turn",
    "OTHER": "end_turn",
}


def convert_response(
    gemini_resp: dict[str, Any],
    *,
    model: str = "unknown",
    request_id: str | None = None,
) -> dict[str, Any]:
    """将 Gemini 非流式响应转换为 Anthropic Messages API 格式."""
    candidates = gemini_resp.get("candidates", [])
    candidate = candidates[0] if candidates else {}

    content_parts = candidate.get("content", {}).get("parts", [])
    content_blocks = _convert_parts(content_parts)

    finish_reason = candidate.get("finishReason", "STOP")
    stop_reason = _FINISH_REASON_MAP.get(finish_reason, "end_turn")

    usage = extract_usage(gemini_resp)
    msg_id = request_id or f"msg_{uuid.uuid4().hex[:24]}"

    return {
        "id": msg_id,
        "type": "message",
        "role": "assistant",
        "content": content_blocks,
        "model": model,
        "stop_reason": stop_reason,
        "stop_sequence": None,
        "usage": usage,
    }


def extract_usage(gemini_resp: dict[str, Any]) -> dict[str, int]:
    """提取 Gemini usageMetadata → Anthropic usage 格式."""
    meta = gemini_resp.get("usageMetadata", {})
    return {
        "input_tokens": meta.get("promptTokenCount", 0),
        "output_tokens": meta.get("candidatesTokenCount", 0),
        "cache_creation_input_tokens": 0,
        "cache_read_input_tokens": 0,
    }


def _convert_parts(parts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """转换 Gemini parts → Anthropic content blocks."""
    blocks: list[dict[str, Any]] = []
    for part in parts:
        if "text" in part:
            blocks.append({"type": "text", "text": part["text"]})
        elif "functionCall" in part:
            fc = part["functionCall"]
            blocks.append({
                "type": "tool_use",
                "id": f"toolu_{uuid.uuid4().hex[:24]}",
                "name": fc.get("name", ""),
                "input": fc.get("args", {}),
            })
    return blocks
