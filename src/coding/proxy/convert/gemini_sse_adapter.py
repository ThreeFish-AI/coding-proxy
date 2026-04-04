"""Gemini SSE 字节流 → Anthropic SSE 字节流适配器."""

from __future__ import annotations

import json
import logging
import uuid
from typing import Any, AsyncIterator

from .gemini_to_anthropic import GEMINI_FINISH_REASON_MAP

logger = logging.getLogger(__name__)


async def adapt_sse_stream(
    gemini_chunks: AsyncIterator[bytes],
    model: str,
    request_id: str | None = None,
) -> AsyncIterator[bytes]:
    """将 Gemini SSE 流转换为 Anthropic Messages SSE 流."""
    msg_id = request_id or f"msg_{uuid.uuid4().hex[:24]}"
    started = False
    block_index = 0
    current_block_type: str | None = None
    total_output_tokens = 0
    input_tokens = 0
    used_tool = False

    async for raw_chunk in gemini_chunks:
        text = raw_chunk.decode("utf-8", errors="ignore")

        for line in text.split("\n"):
            line = line.strip()
            if not line.startswith("data:"):
                continue
            payload = line[5:].strip()
            if not payload or payload == "[DONE]":
                continue

            try:
                data = json.loads(payload)
            except json.JSONDecodeError:
                logger.debug("SSE chunk JSON 解析失败，跳过: %s", payload[:200])
                continue

            meta = data.get("usageMetadata", {})
            if "promptTokenCount" in meta:
                input_tokens = meta["promptTokenCount"]
            if "candidatesTokenCount" in meta:
                total_output_tokens = meta["candidatesTokenCount"]

            candidates = data.get("candidates", [])
            if not candidates:
                continue
            candidate = candidates[0]
            parts = candidate.get("content", {}).get("parts", [])
            finish_reason = candidate.get("finishReason")

            for part in parts:
                block_type, start_block, delta = _part_to_events(part)
                if delta is None:
                    continue
                if not started:
                    started = True
                    yield _make_event("message_start", {
                        "type": "message_start",
                        "message": {
                            "id": msg_id,
                            "type": "message",
                            "role": "assistant",
                            "content": [],
                            "model": model,
                            "usage": {"input_tokens": input_tokens, "output_tokens": 0},
                        },
                    })

                if current_block_type != block_type:
                    if current_block_type is not None:
                        yield _make_event("content_block_stop", {
                            "type": "content_block_stop",
                            "index": block_index,
                        })
                        block_index += 1
                    yield _make_event("content_block_start", {
                        "type": "content_block_start",
                        "index": block_index,
                        "content_block": start_block,
                    })
                    current_block_type = block_type

                yield _make_event("content_block_delta", {
                    "type": "content_block_delta",
                    "index": block_index,
                    "delta": delta,
                })

                if block_type == "tool_use":
                    used_tool = True
                    yield _make_event("content_block_stop", {
                        "type": "content_block_stop",
                        "index": block_index,
                    })
                    block_index += 1
                    current_block_type = None

            if finish_reason and finish_reason != "FINISH_REASON_UNSPECIFIED":
                if current_block_type is not None:
                    yield _make_event("content_block_stop", {
                        "type": "content_block_stop",
                        "index": block_index,
                    })
                    current_block_type = None
                stop_reason = "tool_use" if used_tool else _map_finish_reason(finish_reason)
                yield _make_event("message_delta", {
                    "type": "message_delta",
                    "delta": {"stop_reason": stop_reason, "stop_sequence": None},
                    "usage": {"output_tokens": total_output_tokens},
                })
                yield _make_event("message_stop", {"type": "message_stop"})
                return

    if current_block_type is not None:
        yield _make_event("content_block_stop", {
            "type": "content_block_stop",
            "index": block_index,
        })
    yield _make_event("message_delta", {
        "type": "message_delta",
        "delta": {"stop_reason": "tool_use" if used_tool else "end_turn", "stop_sequence": None},
        "usage": {"output_tokens": total_output_tokens},
    })
    yield _make_event("message_stop", {"type": "message_stop"})


def _part_to_events(part: dict[str, Any]) -> tuple[str, dict[str, Any], dict[str, Any] | None]:
    if part.get("functionCall"):
        fc = part["functionCall"]
        start_block = {
            "type": "tool_use",
            "id": fc.get("id") or f"toolu_{uuid.uuid4().hex[:24]}",
            "name": fc.get("name", ""),
            "input": {},
        }
        return "tool_use", start_block, {
            "type": "input_json_delta",
            "partial_json": json.dumps(fc.get("args", {}), ensure_ascii=False),
        }

    if part.get("text") is not None and part.get("thought"):
        return "thinking", {"type": "thinking", "thinking": ""}, {
            "type": "thinking_delta",
            "thinking": part.get("text", ""),
        }

    if part.get("text"):
        return "text", {"type": "text", "text": ""}, {
            "type": "text_delta",
            "text": part["text"],
        }

    return "text", {"type": "text", "text": ""}, None


def _make_event(event_type: str, data: dict[str, Any]) -> bytes:
    return f"event: {event_type}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n".encode()


def _map_finish_reason(reason: str) -> str:
    return GEMINI_FINISH_REASON_MAP.get(reason, "end_turn")
