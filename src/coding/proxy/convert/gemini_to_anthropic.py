"""Google Gemini 响应 → Anthropic Messages API 格式转换."""

from __future__ import annotations

import logging
import uuid
from typing import Any

logger = logging.getLogger(__name__)

# Gemini finishReason → Anthropic stop_reason 映射（SOT）
# 本映射为 Gemini→Anthropic 协议转换层中 finish reason 的唯一定义源，
# gemini_sse_adapter 通过导入本常量实现去重。
GEMINI_FINISH_REASON_MAP: dict[str, str] = {
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
    stop_reason = (
        "tool_use"
        if any(block.get("type") == "tool_use" for block in content_blocks)
        else (GEMINI_FINISH_REASON_MAP.get(finish_reason, "end_turn"))
    )

    usage = extract_usage(gemini_resp)
    msg_id = (
        request_id or gemini_resp.get("responseId") or f"msg_{uuid.uuid4().hex[:24]}"
    )

    result = {
        "id": msg_id,
        "type": "message",
        "role": "assistant",
        "content": content_blocks,
        "model": model,
        "stop_reason": stop_reason,
        "stop_sequence": None,
        "usage": usage,
    }
    logger.debug(
        "convert_response: %d content blocks, stop_reason=%s",
        len(content_blocks),
        stop_reason,
    )
    return result


def extract_usage(gemini_resp: dict[str, Any]) -> dict[str, int]:
    meta = gemini_resp.get("usageMetadata", {})
    return {
        "input_tokens": meta.get("promptTokenCount", 0),
        "output_tokens": meta.get("candidatesTokenCount", 0),
        "cache_creation_input_tokens": 0,
        "cache_read_input_tokens": 0,
    }


def _convert_parts(parts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    blocks: list[dict[str, Any]] = []
    for part in parts:
        signature = part.get("thoughtSignature")
        if part.get("functionCall"):
            fc = part["functionCall"]
            blocks.append(
                {
                    "type": "tool_use",
                    "id": fc.get("id") or f"toolu_{uuid.uuid4().hex[:24]}",
                    "name": fc.get("name", ""),
                    "input": fc.get("args", {}),
                    **({"signature": signature} if signature else {}),
                }
            )
            continue
        if part.get("text") is not None:
            text = part.get("text", "")
            if part.get("thought"):
                blocks.append(
                    {
                        "type": "thinking",
                        "thinking": text,
                        **({"signature": signature} if signature else {}),
                    }
                )
            elif text:
                blocks.append({"type": "text", "text": text})
            elif signature:
                blocks.append(
                    {"type": "thinking", "thinking": "", "signature": signature}
                )
    return blocks
