"""将供应商流式响应收敛为 Anthropic 兼容 SSE."""

from __future__ import annotations

import json
import logging
import uuid
from typing import Any, AsyncIterator

logger = logging.getLogger(__name__)

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
        self.cache_creation_tokens = 0
        self.cache_read_tokens = 0
        self.block_index = 0
        self.content_block_open = False
        self.tool_calls: dict[int, dict[str, Any]] = {}
        self.usage_updated = False  # 标记是否已收到 usage 信息
        self.thinking_block_open = False

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
                        **(
                            {"cache_creation_input_tokens": self.cache_creation_tokens}
                            if self.cache_creation_tokens > 0 else {}
                        ),
                        **(
                            {"cache_read_input_tokens": self.cache_read_tokens}
                            if self.cache_read_tokens > 0 else {}
                        ),
                    },
                },
            }),
        ]

    def close(self, reason: str = "end_turn") -> list[bytes]:
        if self.stopped:
            return []
        self.stopped = True
        chunks: list[bytes] = []
        if self.started and self.content_block_open:
            chunks.append(_make_event("content_block_stop", {
                "type": "content_block_stop",
                "index": self.block_index,
            }))
            self.content_block_open = False
        # 确保在最终的 message_delta 中包含完整的 token 信息
        usage_data = {"output_tokens": self.output_tokens}
        if self.usage_updated and self.input_tokens > 0:
            usage_data["input_tokens"] = self.input_tokens
        if self.cache_creation_tokens > 0:
            usage_data["cache_creation_input_tokens"] = self.cache_creation_tokens
        if self.cache_read_tokens > 0:
            usage_data["cache_read_input_tokens"] = self.cache_read_tokens

        chunks.append(_make_event("message_delta", {
            "type": "message_delta",
            "delta": {"stop_reason": reason, "stop_sequence": None},
            "usage": usage_data,
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


# OpenAI 兼容协议供应商可能使用的非标准内容块类型别名（Copilot / Antigravity 等使用）
_TOOL_USE_BLOCK_TYPES = {"text", "tool_use", "tool_call", "function_call", "thinking"}

# OpenAI 兼容协议供应商可能使用的非标准 delta 类型别名
_INPUT_JSON_DELTA_TYPES = {"input_json_delta", "arguments_delta", "tool_call_delta"}


def _normalize_direct_event(data: dict[str, Any], event_name: str | None) -> list[bytes]:
    event_type = data.get("type")
    if event_type == "content_block_start":
        block = data.get("content_block", {})
        block_type = block.get("type")
        if block_type not in _TOOL_USE_BLOCK_TYPES:
            logger.debug("Filtered non-standard content_block_start type: %s", block_type)
            return []
        # OpenAI 兼容供应商可能在 content_block_start.input 中内联返回完整工具参数
        if block_type == "tool_use":
            result = [_make_event(event_name or event_type, data)]
            inline_input = block.get("input")
            if isinstance(inline_input, dict) and inline_input:
                logger.debug(
                    "Tool_use block with inline input: name=%s args=%s",
                    block.get("name", "?"),
                    json.dumps(inline_input, ensure_ascii=False)[:200],
                )
                result.append(_make_event("content_block_delta", {
                    "type": "content_block_delta",
                    "index": data.get("index", 0),
                    "delta": {
                        "type": "input_json_delta",
                        "partial_json": json.dumps(inline_input, ensure_ascii=False),
                    },
                }))
            return result
        # 非标准类型归一化为 tool_use
        if block_type in ("tool_call", "function_call"):
            logger.debug("Normalizing non-standard block type '%s' to 'tool_use'", block_type)
            normalized_block = {**block, "type": "tool_use"}
            normalized_data = {**data, "content_block": normalized_block}
            return [_make_event(event_name or event_type, normalized_data)]

    if event_type == "content_block_delta":
        delta = data.get("delta", {})
        delta_type = delta.get("type")
        # 放行标准 delta 类型
        if delta_type in {"text_delta", "input_json_delta", "thinking_delta"}:
            return [_make_event(event_name or event_type, data)]
        # 归一化非标准 input_json_delta 别名（OpenAI 兼容供应商可能使用 arguments_delta 等）
        if delta_type in _INPUT_JSON_DELTA_TYPES:
            normalized_delta = {**delta, "type": "input_json_delta"}
            if "partial_json" not in normalized_delta and "arguments" in normalized_delta:
                normalized_delta["partial_json"] = normalized_delta.pop("arguments")
            logger.debug("Normalizing non-standard delta type '%s' to 'input_json_delta'", delta_type)
            return [_make_event(event_name or event_type, {**data, "delta": normalized_delta})]
        # 其他 delta 类型过滤
        logger.debug("Filtered non-standard content_block_delta type: %s", delta_type)
        return []

    if event_type not in _DIRECT_EVENTS:
        logger.debug("Filtered non-standard event type: %s", event_type)
        return []
    return [_make_event(event_name or event_type, data)]


def _normalize_stream_event(data: dict[str, Any], event_name: str | None) -> list[bytes]:
    nested = data.get("event")
    if not isinstance(nested, dict):
        return []
    nested_name = event_name or nested.get("type")
    return _normalize_direct_event(nested, nested_name)


def _extract_prompt_tokens_details(usage: dict[str, Any]) -> dict[str, Any]:
    details = usage.get("prompt_tokens_details")
    return details if isinstance(details, dict) else {}


def _extract_cache_read_tokens(usage: dict[str, Any]) -> int:
    details = _extract_prompt_tokens_details(usage)
    for value in (
        usage.get("cache_read_input_tokens"),
        details.get("cached_tokens"),
        details.get("cache_read_tokens"),
    ):
        if isinstance(value, int):
            return value
    return 0


def _extract_cache_creation_tokens(usage: dict[str, Any]) -> int:
    details = _extract_prompt_tokens_details(usage)
    for value in (
        usage.get("cache_creation_input_tokens"),
        details.get("cache_creation_input_tokens"),
        details.get("cache_creation_tokens"),
    ):
        if isinstance(value, int):
            return value
    return 0


def _normalize_openai_chunk(data: dict[str, Any], state: _OpenAICompatState) -> list[bytes]:
    chunks: list[bytes] = []
    usage = data.get("usage", {})

    # 处理 token 统计更新
    if "prompt_tokens" in usage:
        state.input_tokens = usage.get("prompt_tokens", state.input_tokens)
        state.usage_updated = True

    if "completion_tokens" in usage:
        state.output_tokens = usage.get("completion_tokens", state.output_tokens)
        state.usage_updated = True

    cache_read_tokens = _extract_cache_read_tokens(usage)
    if cache_read_tokens > 0:
        state.cache_read_tokens = cache_read_tokens
        state.usage_updated = True

    cache_creation_tokens = _extract_cache_creation_tokens(usage)
    if cache_creation_tokens > 0:
        state.cache_creation_tokens = cache_creation_tokens
        state.usage_updated = True

    choices = data.get("choices", [])
    if not choices:
        return chunks

    choice = choices[0]
    delta = choice.get("delta", {})
    finish_reason = choice.get("finish_reason")

    # ── 处理 reasoning_content（OpenAI 兼容格式的深度思考内容） ──
    reasoning_content = delta.get("reasoning_content")
    if reasoning_content:
        chunks.extend(state.ensure_started())
        if not state.thinking_block_open:
            chunks.append(_make_event("content_block_start", {
                "type": "content_block_start",
                "index": state.block_index,
                "content_block": {"type": "thinking", "thinking": ""},
            }))
            state.thinking_block_open = True
            state.content_block_open = True
        chunks.append(_make_event("content_block_delta", {
            "type": "content_block_delta",
            "index": state.block_index,
            "delta": {"type": "thinking_delta", "thinking": reasoning_content},
        }))

    text_fragments = _extract_text_fragments(delta.get("content"))
    if text_fragments:
        chunks.extend(state.ensure_started())
        # 如果 thinking 块开着，先关闭它再打开 text 块
        if state.thinking_block_open:
            chunks.append(_make_event("content_block_stop", {
                "type": "content_block_stop",
                "index": state.block_index,
            }))
            state.block_index += 1
            state.thinking_block_open = False
            state.content_block_open = False

        if state.content_block_open and any(
            tool.get("anthropic_block_index") == state.block_index
            for tool in state.tool_calls.values()
        ):
            chunks.append(_make_event("content_block_stop", {
                "type": "content_block_stop",
                "index": state.block_index,
            }))
            state.block_index += 1
            state.content_block_open = False

        if not state.content_block_open:
            chunks.append(_make_event("content_block_start", {
                "type": "content_block_start",
                "index": state.block_index,
                "content_block": {"type": "text", "text": ""},
            }))
            state.content_block_open = True

        for text in text_fragments:
            chunks.append(_make_event("content_block_delta", {
                "type": "content_block_delta",
                "index": state.block_index,
                "delta": {"type": "text_delta", "text": text},
            }))

    tool_calls = delta.get("tool_calls") or []
    for tool_call in tool_calls:
        if not isinstance(tool_call, dict):
            continue
        tool_index = int(tool_call.get("index", 0))
        if tool_call.get("id") and isinstance(tool_call.get("function"), dict) and tool_call["function"].get("name"):
            if state.content_block_open:
                chunks.append(_make_event("content_block_stop", {
                    "type": "content_block_stop",
                    "index": state.block_index,
                }))
                state.block_index += 1
                state.content_block_open = False

            state.tool_calls[tool_index] = {
                "id": tool_call["id"],
                "name": tool_call["function"]["name"],
                "anthropic_block_index": state.block_index,
            }
            chunks.extend(state.ensure_started())
            chunks.append(_make_event("content_block_start", {
                "type": "content_block_start",
                "index": state.block_index,
                "content_block": {
                    "type": "tool_use",
                    "id": tool_call["id"],
                    "name": tool_call["function"]["name"],
                    "input": {},
                },
            }))
            state.content_block_open = True

        function = tool_call.get("function")
        if isinstance(function, dict):
            tool_info = state.tool_calls.get(tool_index)
            arguments = function.get("arguments")
            if tool_info and arguments:
                chunks.append(_make_event("content_block_delta", {
                    "type": "content_block_delta",
                    "index": tool_info["anthropic_block_index"],
                    "delta": {
                        "type": "input_json_delta",
                        "partial_json": arguments,
                    },
                }))
            elif tool_info and arguments is None and finish_reason in ("tool_calls", "stop"):
                # OpenAI 兼容供应商有时在 finish 时仍返回 null arguments
                logger.debug(
                    "Tool call '%s' has null arguments at finish_reason=%s",
                    tool_info.get("name", "?"), finish_reason,
                )

    if finish_reason:
        stop_reason = "max_tokens" if finish_reason == "length" else "end_turn"
        if finish_reason == "tool_calls":
            stop_reason = "tool_use"
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
                # 诊断：记录含工具调用的 OpenAI 格式 chunk
                choices = data.get("choices", [])
                if choices and any(
                    isinstance(c.get("delta", {}).get("tool_calls"), list)
                    for c in choices
                    if isinstance(c, dict)
                ):
                    logger.debug(
                        "OpenAI tool_call chunk: %s",
                        json.dumps(data, ensure_ascii=False)[:500],
                    )
                chunks = _normalize_openai_chunk(data, state)

            for chunk in chunks:
                emitted_any = True
                yield chunk

        if emitted_any:
            continue

    for chunk in state.close():
        yield chunk
