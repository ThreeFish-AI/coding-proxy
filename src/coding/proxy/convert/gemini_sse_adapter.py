"""Gemini SSE 字节流 → Anthropic SSE 字节流适配器.

将 Gemini 的 SSE 流式响应转换为 Anthropic Messages API 的 SSE 事件格式:
  message_start → content_block_start → content_block_delta* →
  content_block_stop → message_delta → message_stop
"""

from __future__ import annotations

import json
import logging
import uuid
from typing import Any, AsyncIterator

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
    total_output_tokens = 0
    input_tokens = 0

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
                continue

            # 提取 usage
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

            # 首次收到内容 → message_start + content_block_start
            if not started and parts:
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
                yield _make_event("content_block_start", {
                    "type": "content_block_start",
                    "index": block_index,
                    "content_block": {"type": "text", "text": ""},
                })

            # 文本增量
            for part in parts:
                if "text" in part and part["text"]:
                    if not started:
                        # 若尚未 started（无 parts 的首 chunk 后跟有 text 的 chunk）
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
                        yield _make_event("content_block_start", {
                            "type": "content_block_start",
                            "index": block_index,
                            "content_block": {"type": "text", "text": ""},
                        })
                    yield _make_event("content_block_delta", {
                        "type": "content_block_delta",
                        "index": block_index,
                        "delta": {"type": "text_delta", "text": part["text"]},
                    })

            # finishReason → 关闭序列
            if finish_reason and finish_reason != "FINISH_REASON_UNSPECIFIED":
                stop_reason = _map_finish_reason(finish_reason)
                if started:
                    yield _make_event("content_block_stop", {
                        "type": "content_block_stop",
                        "index": block_index,
                    })
                yield _make_event("message_delta", {
                    "type": "message_delta",
                    "delta": {"stop_reason": stop_reason, "stop_sequence": None},
                    "usage": {"output_tokens": total_output_tokens},
                })
                yield _make_event("message_stop", {"type": "message_stop"})
                return

    # 流正常结束但未收到 finishReason → 补发关闭事件
    if started:
        yield _make_event("content_block_stop", {
            "type": "content_block_stop",
            "index": block_index,
        })
    yield _make_event("message_delta", {
        "type": "message_delta",
        "delta": {"stop_reason": "end_turn", "stop_sequence": None},
        "usage": {"output_tokens": total_output_tokens},
    })
    yield _make_event("message_stop", {"type": "message_stop"})


def _make_event(event_type: str, data: dict[str, Any]) -> bytes:
    """构造 Anthropic SSE 事件字节."""
    return f"event: {event_type}\ndata: {json.dumps(data)}\n\n".encode()


def _map_finish_reason(reason: str) -> str:
    """映射 Gemini finishReason → Anthropic stop_reason."""
    mapping = {
        "STOP": "end_turn",
        "MAX_TOKENS": "max_tokens",
        "SAFETY": "end_turn",
        "RECITATION": "end_turn",
        "OTHER": "end_turn",
    }
    return mapping.get(reason, "end_turn")
