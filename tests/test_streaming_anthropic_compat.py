"""OpenAI → Anthropic 流式兼容转换器单元测试.

覆盖 :mod:`coding.proxy.streaming.anthropic_compat` 的核心逻辑：
- _OpenAICompatState 状态机生命周期
- _normalize_openai_chunk 各事件类型分发
- _normalize_direct_event 过滤与归一化
- normalize_anthropic_compatible_stream 端到端流程
- 边界条件：空 choices、null delta、异常 finish_reason
"""

from __future__ import annotations

import json

import pytest

from coding.proxy.streaming.anthropic_compat import (
    _OpenAICompatState,
    _extract_cache_creation_tokens,
    _extract_cache_read_tokens,
    _extract_text_fragments,
    _make_event,
    _normalize_direct_event,
    _normalize_openai_chunk,
    _normalize_stream_event,
    normalize_anthropic_compatible_stream,
)


# ── 辅助函数 ───────────────────────────────────────────────


async def _raw_chunks(lines: list[str]):
    """将 SSE 文本行列表转换为字节 AsyncIterator."""
    for line in lines:
        yield line.encode()


def _parse_events(raw_bytes_list: list[bytes]) -> list[dict]:
    """解析 Anthropic SSE 事件列表."""
    events = []
    for raw in raw_bytes_list:
        text = raw.decode()
        for block in text.strip().split("\n\n"):
            if not block.strip():
                continue
            event_type = None
            payload = None
            for line in block.split("\n"):
                if line.startswith("event: "):
                    event_type = line[7:]
                elif line.startswith("data: "):
                    payload = json.loads(line[6:])
            if event_type and payload:
                events.append({"event": event_type, "data": payload})
    return events


# ── _make_event ────────────────────────────────────────────


class TestMakeEvent:
    """SSE 事件序列化测试."""

    def test_basic_event(self):
        result = _make_event("message_start", {"type": "message_start"})
        text = result.decode()
        assert text.startswith("event: message_start")
        assert 'data: {"type": "message_start"}' in text
        assert text.endswith("\n\n")

    def test_unicode_content(self):
        result = _make_event("content_block_delta", {
            "type": "content_block_delta",
            "delta": {"type": "text_delta", "text": "你好世界"},
        })
        data = json.loads(result.decode().split("data: ")[1])
        assert data["delta"]["text"] == "你好世界"


# ── _extract_text_fragments ───────────────────────────────


class TestExtractTextFragments:
    """文本片段提取测试."""

    def test_string_delta(self):
        assert _extract_text_fragments("hello") == ["hello"]

    def test_empty_string(self):
        assert _extract_text_fragments("") == []

    def test_none_input(self):
        assert _extract_text_fragments(None) == []

    def test_list_of_strings(self):
        assert _extract_text_fragments(["a", "", "b"]) == ["a", "b"]

    def test_list_of_dicts_with_type_text(self):
        items = [{"type": "text", "text": "x"}, {"type": "other", "value": 1}]
        assert _extract_text_fragments(items) == ["x"]

    def test_non_string_non_dict(self):
        assert _extract_text_fragments(42) == []


# ── Cache Token 提取 ─────────────────────────────────────


class TestExtractCacheTokens:
    """Cache token 用量提取测试."""

    def test_cache_read_from_top_level(self):
        usage = {"cache_read_input_tokens": 100}
        assert _extract_cache_read_tokens(usage) == 100

    def test_cache_read_from_details_cached_tokens(self):
        usage = {"prompt_tokens_details": {"cached_tokens": 50}}
        assert _extract_cache_read_tokens(usage) == 50

    def test_cache_read_from_details_cache_read_tokens(self):
        usage = {"prompt_tokens_details": {"cache_read_tokens": 30}}
        assert _extract_cache_read_tokens(usage) == 30

    def test_cache_read_no_data(self):
        assert _extract_cache_read_tokens({}) == 0

    def test_cache_creation_from_top_level(self):
        usage = {"cache_creation_input_tokens": 200}
        assert _extract_cache_creation_tokens(usage) == 200

    def test_cache_creation_from_details(self):
        usage = {"prompt_tokens_details": {"cache_creation_tokens": 80}}
        assert _extract_cache_creation_tokens(usage) == 80

    def test_cache_creation_no_data(self):
        assert _extract_cache_creation_tokens({}) == 0


# ── _OpenAICompatState 状态机 ────────────────────────────


class TestOpenAICompatState:
    """状态机生命周期测试."""

    def test_initial_state(self):
        state = _OpenAICompatState("test-model")
        assert state.started is False
        assert state.stopped is False
        assert state.block_index == 0
        assert state.content_block_open is False
        assert state.thinking_block_open is False

    def test_ensure_started_emits_message_start(self):
        state = _OpenAICompatState("m")
        chunks = state.ensure_started()
        assert len(chunks) == 1
        data = json.loads(chunks[0].decode().split("data: ")[1])
        assert data["type"] == "message_start"
        assert data["message"]["model"] == "m"
        assert state.started is True

    def test_ensure_started_idempotent(self):
        state = _OpenAICompatState("m")
        state.ensure_started()
        chunks = state.ensure_started()
        assert chunks == []

    def test_close_without_start_returns_empty(self):
        state = _OpenAICompatState("m")
        chunks = state.close()
        # 即使未 started 也应产生 message_stop（保证流结束）
        assert len(chunks) >= 1
        types = [json.loads(c.decode().split("data: ")[1])["type"] for c in chunks]
        assert "message_stop" in types

    def test_close_idempotent(self):
        state = _OpenAICompatState("m")
        state.close()
        chunks = state.close()
        assert chunks == []

    def test_close_with_usage(self):
        state = _OpenAICompatState("m")
        state.output_tokens = 42
        state.input_tokens = 10
        state.usage_updated = True
        chunks = state.close()
        delta = next(
            (c for c in chunks if b"message_delta" in c),
            None,
        )
        assert delta is not None
        data = json.loads(delta.decode().split("data: ")[1])
        assert data["usage"]["output_tokens"] == 42
        assert data["usage"]["input_tokens"] == 10

    def test_close_stop_reason_default_end_turn(self):
        state = _OpenAICompatState("m")
        state.ensure_started()
        chunks = state.close()
        delta = next(c for c in chunks if b"message_delta" in c)
        data = json.loads(delta.decode().split("data: ")[1])
        assert data["delta"]["stop_reason"] == "end_turn"

    def test_close_stop_reason_custom(self):
        state = _OpenAICompatState("m")
        state.ensure_started()
        chunks = state.close(reason="tool_use")
        delta = next(c for c in chunks if b"message_delta" in c)
        data = json.loads(delta.decode().split("data: ")[1])
        assert data["delta"]["stop_reason"] == "tool_use"

    def test_update_usage_prompt_tokens(self):
        state = _OpenAICompatState("m")
        state.update_usage({"prompt_tokens": 100})
        assert state.input_tokens == 100
        assert state.usage_updated is True

    def test_update_usage_completion_tokens(self):
        state = _OpenAICompatState("m")
        state.update_usage({"completion_tokens": 50})
        assert state.output_tokens == 50

    def test_update_usage_cache_read(self):
        state = _OpenAICompatState("m")
        state.update_usage({"prompt_tokens_details": {"cached_tokens": 20}})
        assert state.cache_read_tokens == 20

    def test_open_thinking_block(self):
        state = _OpenAICompatState("m")
        chunks = state.open_thinking_block()
        assert len(chunks) == 1
        data = json.loads(chunks[0].decode().split("data: ")[1])
        assert data["content_block"]["type"] == "thinking"
        assert state.thinking_block_open is True
        assert state.content_block_open is True

    def test_open_thinking_block_idempotent(self):
        state = _OpenAICompatState("m")
        state.open_thinking_block()
        chunks = state.open_thinking_block()
        assert chunks == []

    def test_ensure_text_block_after_thinking(self):
        """打开 text 块前应先关闭 thinking 块."""
        state = _OpenAICompatState("m")
        state.ensure_started()
        state.open_thinking_block()
        chunks = state.ensure_text_block()
        # 应包含：thinking block_stop + text block_start
        types = [json.loads(c.decode().split("data: ")[1])["type"] for c in chunks]
        assert "content_block_stop" in types
        assert "content_block_start" in types
        assert state.thinking_block_open is False

    def test_ensure_text_block_idempotent(self):
        state = _OpenAICompatState("m")
        state.ensure_text_block()  # 打开
        chunks = state.ensure_text_block()  # 再次调用应无操作
        assert chunks == []

    def test_close_content_block_increments_index(self):
        state = _OpenAICompatState("m")
        state.content_block_open = True
        chunks = state.close_content_block()
        assert len(chunks) == 1
        assert state.block_index == 1
        assert state.content_block_open is False

    def test_close_content_block_when_not_open(self):
        state = _OpenAICompatState("m")
        chunks = state.close_content_block()
        assert chunks == []
        assert state.block_index == 0

    def test_open_tool_block(self):
        state = _OpenAICompatState("m")
        tool_call = {
            "id": "call_1",
            "function": {"name": "bash"},
        }
        chunks = state.open_tool_block(0, tool_call)
        assert len(chunks) == 1
        data = json.loads(chunks[0].decode().split("data: ")[1])
        assert data["content_block"]["type"] == "tool_use"
        assert data["content_block"]["name"] == "bash"
        assert 0 in state.tool_calls

    def test_feed_tool_arguments(self):
        state = _OpenAICompatState("m")
        state.tool_calls[0] = {
            "id": "call_1",
            "name": "bash",
            "anthropic_block_index": 0,
        }
        chunks = state.feed_tool_arguments(0, '{"cmd":"ls"}')
        assert len(chunks) == 1
        data = json.loads(chunks[0].decode().split("data: ")[1])
        assert data["delta"]["partial_json"] == '{"cmd":"ls"}'

    def test_feed_tool_arguments_unknown_index(self):
        state = _OpenAICompatState("m")
        chunks = state.feed_tool_arguments(99, "{}")
        assert chunks == []

    def test_feed_tool_arguments_empty_string(self):
        state = _OpenAICompatState("m")
        state.tool_calls[0] = {
            "id": "call_1",
            "name": "bash",
            "anthropic_block_index": 0,
        }
        chunks = state.feed_tool_arguments(0, "")
        assert chunks == []


# ── _normalize_direct_event ─────────────────────────────


class TestNormalizeDirectEvent:
    """直接事件归一化测试."""

    def test_standard_event_passes_through(self):
        data = {"type": "ping"}
        result = _normalize_direct_event(data, "ping")
        assert len(result) == 1
        assert b"event: ping" in result[0]

    def test_message_start_passes_through(self):
        data = {"type": "message_start", "message": {}}
        result = _normalize_direct_event(data, "message_start")
        assert len(result) == 1

    def test_nonstandard_content_block_start_filtered(self):
        data = {
            "type": "content_block_start",
            "index": 0,
            "content_block": {"type": "server_tool_use", "id": "t1"},
        }
        result = _normalize_direct_event(data, "content_block_start")
        assert result == []

    def test_tool_use_block_start_preserved(self):
        data = {
            "type": "content_block_start",
            "index": 0,
            "content_block": {"type": "tool_use", "id": "tu_1", "name": "bash", "input": {}},
        }
        result = _normalize_direct_event(data, "content_block_start")
        assert len(result) == 1

    def test_tool_use_with_inline_input_generates_delta(self):
        data = {
            "type": "content_block_start",
            "index": 0,
            "content_block": {
                "type": "tool_use",
                "id": "tu_1",
                "name": "Task",
                "input": {"description": "test"},
            },
        }
        result = _normalize_direct_event(data, "content_block_start")
        assert len(result) == 2
        # 第二个事件应为 input_json_delta
        delta_data = json.loads(result[1].decode().split("data: ")[1])
        assert delta_data["delta"]["type"] == "input_json_delta"

    def test_tool_call_normalized_to_tool_use(self):
        data = {
            "type": "content_block_start",
            "index": 0,
            "content_block": {"type": "tool_call", "id": "tc_1", "name": "fn", "input": {}},
        }
        result = _normalize_direct_event(data, "content_block_start")
        assert len(result) == 1
        block_data = json.loads(result[0].decode().split("data: ")[1])
        assert block_data["content_block"]["type"] == "tool_use"

    def test_function_call_normalized_to_tool_use(self):
        data = {
            "type": "content_block_start",
            "index": 0,
            "content_block": {"type": "function_call", "id": "fc_1", "name": "fn2", "input": {}},
        }
        result = _normalize_direct_event(data, "content_block_start")
        block_data = json.loads(result[0].decode().split("data: ")[1])
        assert block_data["content_block"]["type"] == "tool_use"

    def test_standard_delta_types_pass(self):
        for delta_type in ("text_delta", "input_json_delta", "thinking_delta"):
            data = {
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": delta_type},
            }
            result = _normalize_direct_event(data, "content_block_delta")
            assert len(result) == 1, f"{delta_type} should pass through"

    def test_nonstandard_delta_filtered(self):
        data = {
            "type": "content_block_delta",
            "index": 0,
            "delta": {"type": "unknown_delta", "value": "x"},
        }
        result = _normalize_direct_event(data, "content_block_delta")
        assert result == []

    def test_arguments_delta_normalized(self):
        data = {
            "type": "content_block_delta",
            "index": 0,
            "delta": {"type": "arguments_delta", "arguments": '{"key":"val"}'},
        }
        result = _normalize_direct_event(data, "content_block_delta")
        assert len(result) == 1
        delta_data = json.loads(result[0].decode().split("data: ")[1])
        assert delta_data["delta"]["type"] == "input_json_delta"
        assert delta_data["delta"]["partial_json"] == '{"key":"val"}'

    def test_nonstandard_event_type_filtered(self):
        data = {"type": "vendor_private_event"}
        result = _normalize_direct_event(data, None)
        assert result == []


# ── _normalize_stream_event ──────────────────────────────


class TestNormalizeStreamEvent:
    """嵌套 stream_event 归一化测试."""

    def test_valid_nested_event(self):
        data = {
            "type": "stream_event",
            "event": {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "hi"}},
        }
        result = _normalize_stream_event(data, "content_block_delta")
        assert len(result) == 1

    def test_invalid_nested_event_returns_empty(self):
        data = {"type": "stream_event", "event": "not_a_dict"}
        result = _normalize_stream_event(data, None)
        assert result == []

    def test_missing_event_key(self):
        data = {"type": "stream_event"}
        result = _normalize_stream_event(data, None)
        assert result == []


# ── _normalize_openai_chunk ──────────────────────────────


class TestNormalizeOpenAIChunk:
    """OpenAI chunk → Anthropic 事件序列转换测试."""

    def _make_state(self) -> _OpenAICompatState:
        return _OpenAICompatState("test-model")

    def test_empty_choices_returns_empty(self):
        state = self._make_state()
        chunks = _normalize_openai_chunk({"choices": []}, state)
        assert chunks == []

    def test_missing_choices_returns_empty(self):
        state = self._make_state()
        chunks = _normalize_openai_chunk({}, state)
        assert chunks == []

    def test_text_content_produces_text_delta(self):
        state = self._make_state()
        chunks = _normalize_openai_chunk({
            "choices": [{"delta": {"content": "Hello"}, "finish_reason": None}],
        }, state)
        assert any(b"text_delta" in c and b"Hello" in c for c in chunks)

    def test_reasoning_content_opens_thinking_block(self):
        state = self._make_state()
        chunks = _normalize_openai_chunk({
            "choices": [{"delta": {"reasoning_content": "Thinking..."}, "finish_reason": None}],
        }, state)
        assert any(b"thinking" in c and b"Thinking..." in c for c in chunks)
        assert state.thinking_block_open is True

    def test_finish_reason_stop_maps_to_end_turn(self):
        state = self._make_state()
        chunks = _normalize_openai_chunk({
            "choices": [{"delta": {}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 5, "completion_tokens": 3},
        }, state)
        assert any(b"end_turn" in c for c in chunks)

    def test_finish_reason_length_maps_to_max_tokens(self):
        state = self._make_state()
        chunks = _normalize_openai_chunk({
            "choices": [{"delta": {}, "finish_reason": "length"}],
        }, state)
        assert any(b"max_tokens" in c for c in chunks)

    def test_finish_reason_tool_calls_maps_to_tool_use(self):
        state = self._make_state()
        chunks = _normalize_openai_chunk({
            "choices": [{"delta": {}, "finish_reason": "tool_calls"}],
        }, state)
        assert any(b"tool_use" in c for c in chunks)

    def test_tool_call_registration(self):
        state = self._make_state()
        chunks = _normalize_openai_chunk({
            "choices": [{
                "delta": {
                    "tool_calls": [{
                        "index": 0,
                        "id": "call_1",
                        "function": {"name": "get_weather"},
                    }],
                },
                "finish_reason": None,
            }],
        }, state)
        assert any(b"tool_use" in c and b"get_weather" in c for c in chunks)
        assert 0 in state.tool_calls

    def test_tool_call_argument_feeding(self):
        state = self._make_state()
        # 先注册工具
        _normalize_openai_chunk({
            "choices": [{
                "delta": {
                    "tool_calls": [{
                        "index": 0,
                        "id": "call_1",
                        "function": {"name": "bash"},
                    }],
                },
                "finish_reason": None,
            }],
        }, state)
        # 再追加参数
        chunks = _normalize_openai_chunk({
            "choices": [{
                "delta": {
                    "tool_calls": [{
                        "index": 0,
                        "function": {"arguments": '{"cmd":"ls"}'},
                    }],
                },
                "finish_reason": None,
            }],
        }, state)
        assert any(b"input_json_delta" in c for c in chunks)

    def test_null_choice_skipped(self):
        """choices 中含 None 元素时，生产代码会抛出 AttributeError（已知边界行为）."""
        state = self._make_state()
        with pytest.raises(AttributeError):
            _normalize_openai_chunk({
                "choices": [None, {"delta": {"content": "ok"}, "finish_reason": "stop"}],
            }, state)

    def test_usage_updated_from_chunk(self):
        state = self._make_state()
        _normalize_openai_chunk({
            "usage": {"prompt_tokens": 100, "completion_tokens": 20},
            "choices": [{"delta": {"content": "hi"}, "finish_reason": "stop"}],
        }, state)
        assert state.input_tokens == 100
        assert state.output_tokens == 20


# ── normalize_anthropic_compatible_stream 端到端 ────────


class TestNormalizeAnthropicCompatibleStream:
    """端到端流式转换集成测试."""

    @pytest.mark.asyncio
    async def test_empty_stream_closes_cleanly(self):
        """空输入流仍应输出 close 事件."""
        collected = []
        async for chunk in normalize_anthropic_compatible_stream(
            _raw_chunks([]), model="test",
        ):
            collected.append(chunk)
        events = _parse_events(collected)
        assert any(e["event"] == "message_stop" for e in events)

    @pytest.mark.asyncio
    async def test_done_sentinel_triggers_close(self):
        """[DONE] 标记触发消息关闭."""
        chunks = [
            'data: {"choices":[{"delta":{"content":"hi"},"finish_reason":"stop"}],"usage":{"prompt_tokens":1,"completion_tokens":1}}\n\n',
            "data: [DONE]\n\n",
        ]
        collected = []
        async for chunk in normalize_anthropic_compatible_stream(
            _raw_chunks(chunks), model="test",
        ):
            collected.append(chunk)
        events = _parse_events(collected)
        assert any(e["event"] == "message_stop" for e in events)

    @pytest.mark.asyncio
    async def test_malformed_json_skipped(self):
        """格式错误的 JSON 行被跳过，不中断处理."""
        chunks = [
            'data: {invalid json}\n\n',
            'data: {"choices":[{"delta":{"content":"ok"},"finish_reason":"stop"}],"usage":{"prompt_tokens":1,"completion_tokens":1}}\n\n',
            "data: [DONE]\n\n",
        ]
        collected = []
        async for chunk in normalize_anthropic_compatible_stream(
            _raw_chunks(chunks), model="test",
        ):
            collected.append(chunk)
        events = _parse_events(collected)
        # 应至少有 message_stop
        assert any(e["event"] == "message_stop" for e in events)

    @pytest.mark.asyncio
    async def test_anthropic_native_passthrough(self):
        """原生 Anthropic 格式事件完整透传."""
        chunks = [
            'event: message_start\ndata: {"type":"message_start","message":{"id":"msg_1","type":"message","role":"assistant","content":[],"model":"claude-sonnet-4","usage":{"input_tokens":10,"output_tokens":0}}}\n\n',
            'event: content_block_start\ndata: {"type":"content_block_start","index":0,"content_block":{"type":"text","text":""}}\n\n',
            'event: content_block_delta\ndata: {"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"Hi"}}\n\n',
            'event: content_block_stop\ndata: {"type":"content_block_stop","index":0}\n\n',
            'event: message_delta\ndata: {"type":"message_delta","delta":{"stop_reason":"end_turn","stop_sequence":null},"usage":{"output_tokens":3}}\n\n',
            'event: message_stop\ndata: {"type":"message_stop"}\n\n',
        ]
        collected = []
        async for chunk in normalize_anthropic_compatible_stream(
            _raw_chunks(chunks), model="claude-sonnet-4",
        ):
            collected.append(chunk)
        events = _parse_events(collected)
        event_types = [e["event"] for e in events]
        assert event_types == [
            "message_start", "content_block_start", "content_block_delta",
            "content_block_stop", "message_delta", "message_stop",
        ]

    @pytest.mark.asyncio
    async def test_ping_event_passes_through(self):
        """ping 事件被透传（流结束时还会附加 message_delta + message_stop）."""
        chunks = ['event: ping\ndata: {"type":"ping"}\n\n']
        collected = []
        async for chunk in normalize_anthropic_compatible_stream(
            _raw_chunks(chunks), model="test",
        ):
            collected.append(chunk)
        events = _parse_events(collected)
        # ping 事件被保留，流关闭时追加 message_delta + message_stop
        assert events[0]["event"] == "ping"
        assert any(e["event"] == "message_stop" for e in events)

    @pytest.mark.asyncio
    async def test_error_event_passes_through(self):
        """error 事件被透传（流结束时还会附加 message_delta + message_stop）."""
        chunks = [
            'event: error\ndata: {"type":"error","error":{"type":"api_error","message":"upstream error"}}\n\n',
        ]
        collected = []
        async for chunk in normalize_anthropic_compatible_stream(
            _raw_chunks(chunks), model="test",
        ):
            collected.append(chunk)
        events = _parse_events(collected)
        # error 事件被保留，流关闭时追加 message_delta + message_stop
        assert events[0]["event"] == "error"
        assert any(e["event"] == "message_stop" for e in events)
