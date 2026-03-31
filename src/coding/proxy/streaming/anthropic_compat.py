"""将供应商流式响应收敛为 Anthropic 兼容 SSE."""

from __future__ import annotations

import json
import uuid
from typing import Any, AsyncIterator

_DIRECT_EVENTS = {
    "message_start",
    "content_block_start",
    "content_block_delta",
    "content_block_stop",
    "message_delta",
    "message_stop",
    "ping",
    "error",
}


class _OpenAICompatState:
    def __init__(self, model: str) -> None:
        self.model = model
        self.message_id = f"msg_{uuid.uuid4().hex[:24]}"
        self.started = False
        self.stopped = False
        self.input_tokens = 0
        self.output_tokens = 0
        self.block_index = 0

    def ensure_started(self) -> list[bytes]:
        if self.started:
            return []
        self.started = True
        return [
            _make_event("message_start", {
                "type": "message_start",
                "message": {
                    "id": self.message_id,
                    "type": "message",
                    "role": "assistant",
                    "content": [],
                    "model": self.model,
                    "usage": {
                        "input_tokens": self.input_tokens,
                        "output_tokens": 0,
                    },
                },
            }),
            _make_event("content_block_start", {
                "type": "content_block_start",
                "index": self.block_index,
                "content_block": {"type": "text", "text": ""},
            }),
        ]

    def close(self, reason: str = "end_turn") -> list[bytes]:
        if self.stopped:
            return []
        self.stopped = True
        chunks: list[bytes] = []
        if self.started:
            chunks.append(_make_event("content_block_stop", {
                "type": "content_block_stop",
                "index": self.block_index,
            }))
        chunks.append(_make_event("message_delta", {
            "type": "message_delta",
            "delta": {"stop_reason": reason, "stop_sequence": None},
            "usage": {"output_tokens": self.output_tokens},
        }))
        chunks.append(_make_event("message_stop", {"type": "message_stop"}))
        return chunks


def _make_event(event_type: str, data: dict[str, Any]) -> bytes:
    return f"event: {event_type}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n".encode()


def _extract_text_fragments(delta: Any) -> list[str]:
    if isinstance(delta, str):
        return [delta] if delta else []
    if isinstance(delta, list):
        fragments: list[str] = []
        for item in delta:
            if isinstance(item, str):
                if item:
                    fragments.append(item)
            elif isinstance(item, dict) and item.get("type") == "text":
                text = item.get("text", "")
                if text:
                    fragments.append(text)
        return fragments
    return []


def _normalize_direct_event(data: dict[str, Any], event_name: str | None) -> list[bytes]:
    event_type = data.get("type")
    if event_type == "content_block_start":
        block = data.get("content_block", {})
        if block.get("type") != "text":
            return []
    if event_type == "content_block_delta":
        delta = data.get("delta", {})
        if delta.get("type") != "text_delta":
            return []
    if event_type not in _DIRECT_EVENTS:
        return []
    return [_make_event(event_name or event_type, data)]


def _normalize_stream_event(data: dict[str, Any], event_name: str | None) -> list[bytes]:
    nested = data.get("event")
    if not isinstance(nested, dict):
        return []
    nested_name = event_name or nested.get("type")
    return _normalize_direct_event(nested, nested_name)


def _normalize_openai_chunk(data: dict[str, Any], state: _OpenAICompatState) -> list[bytes]:
    chunks: list[bytes] = []
    usage = data.get("usage", {})
    state.input_tokens = usage.get("prompt_tokens", state.input_tokens)
    state.output_tokens = usage.get("completion_tokens", state.output_tokens)

    choices = data.get("choices", [])
    if not choices:
        return chunks

    choice = choices[0]
    delta = choice.get("delta", {})
    finish_reason = choice.get("finish_reason")
    if delta.get("tool_calls"):
        return chunks

    text_fragments = _extract_text_fragments(delta.get("content"))
    if text_fragments:
        chunks.extend(state.ensure_started())
        for text in text_fragments:
            chunks.append(_make_event("content_block_delta", {
                "type": "content_block_delta",
                "index": state.block_index,
                "delta": {"type": "text_delta", "text": text},
            }))

    if finish_reason:
        stop_reason = "max_tokens" if finish_reason == "length" else "end_turn"
        chunks.extend(state.close(stop_reason))

    return chunks


async def normalize_anthropic_compatible_stream(
    upstream: AsyncIterator[bytes],
    *,
    model: str,
) -> AsyncIterator[bytes]:
    """过滤供应商私有事件，并在需要时把 OpenAI 风格流转成 Anthropic SSE."""
    state = _OpenAICompatState(model)

    async for raw_chunk in upstream:
        text = raw_chunk.decode("utf-8", errors="ignore")
        current_event: str | None = None
        emitted_any = False

        for raw_line in text.splitlines():
            line = raw_line.strip()
            if not line:
                current_event = None
                continue
            if line.startswith("event: "):
                current_event = line[7:].strip()
                continue
            if not line.startswith("data: "):
                continue

            payload = line[6:].strip()
            if not payload:
                continue
            if payload == "[DONE]":
                for chunk in state.close():
                    emitted_any = True
                    yield chunk
                continue

            try:
                data = json.loads(payload)
            except json.JSONDecodeError:
                continue

            chunks: list[bytes] = []
            data_type = data.get("type")
            if data_type in _DIRECT_EVENTS:
                if data_type == "message_start":
                    state.started = True
                elif data_type == "message_stop":
                    state.stopped = True
                chunks = _normalize_direct_event(data, current_event)
            elif data_type == "stream_event":
                chunks = _normalize_stream_event(data, current_event)
            elif "choices" in data:
                chunks = _normalize_openai_chunk(data, state)

            for chunk in chunks:
                emitted_any = True
                yield chunk

        if emitted_any:
            continue

    for chunk in state.close():
        yield chunk
