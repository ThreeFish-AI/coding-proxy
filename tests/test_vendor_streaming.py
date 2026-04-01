"""供应商流式响应的 Anthropic 兼容整形测试."""

from __future__ import annotations

import json

import pytest

from coding.proxy.streaming.anthropic_compat import normalize_anthropic_compatible_stream


async def _raw_chunks(lines: list[str]):
    for line in lines:
        yield line.encode()


def _parse_events(raw_bytes_list: list[bytes]) -> list[dict]:
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


@pytest.mark.asyncio
async def test_filters_vendor_tool_events():
    """过滤 server_tool_use / stream_event 等供应商私有工具事件."""
    chunks = [
        'event: content_block_start\ndata: {"type":"content_block_start","index":0,"content_block":{"type":"server_tool_use","id":"tool_1"}}\n\n',
        'event: content_block_delta\ndata: {"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"hello"}}\n\n',
        'event: message_stop\ndata: {"type":"message_stop"}\n\n',
    ]

    collected = []
    async for chunk in normalize_anthropic_compatible_stream(
        _raw_chunks(chunks), model="glm-5.1",
    ):
        collected.append(chunk)

    events = _parse_events(collected)
    assert [event["event"] for event in events] == ["content_block_delta", "message_stop"]


@pytest.mark.asyncio
async def test_openai_style_stream_is_converted():
    """OpenAI/Zhipu 风格流被转为 Anthropic SSE."""
    chunks = [
        'data: {"id":"chatcmpl-1","model":"glm-5.1","choices":[{"delta":{"content":"Hello"},"finish_reason":null}]}\n\n',
        'data: {"choices":[{"delta":{"content":" world"},"finish_reason":"stop"}],"usage":{"prompt_tokens":5,"completion_tokens":2}}\n\n',
        "data: [DONE]\n\n",
    ]

    collected = []
    async for chunk in normalize_anthropic_compatible_stream(
        _raw_chunks(chunks), model="glm-5.1",
    ):
        collected.append(chunk)

    events = _parse_events(collected)
    event_types = [event["event"] for event in events]
    assert "message_start" in event_types
    assert event_types.count("content_block_delta") == 2
    assert "message_stop" in event_types
    message_delta = next(event for event in events if event["event"] == "message_delta")
    assert message_delta["data"]["usage"]["input_tokens"] == 5
    assert message_delta["data"]["usage"]["output_tokens"] == 2


@pytest.mark.asyncio
async def test_openai_style_stream_preserves_cache_read_tokens():
    """Copilot/OpenAI 风格流式 usage 中的 cache_read_input_tokens 会被保留."""
    chunks = [
        'data: {"id":"chatcmpl-1","model":"claude-sonnet-4","choices":[{"delta":{"content":"Hello"},"finish_reason":null}]}\n\n',
        'data: {"choices":[{"delta":{"content":" world"},"finish_reason":"stop"}],"usage":{"prompt_tokens":25,"completion_tokens":3,"prompt_tokens_details":{"cached_tokens":12}}}\n\n',
        "data: [DONE]\n\n",
    ]

    collected = []
    async for chunk in normalize_anthropic_compatible_stream(
        _raw_chunks(chunks), model="claude-sonnet-4",
    ):
        collected.append(chunk)

    events = _parse_events(collected)
    message_delta = next(event for event in events if event["event"] == "message_delta")
    assert message_delta["data"]["usage"]["input_tokens"] == 25
    assert message_delta["data"]["usage"]["output_tokens"] == 3
    assert message_delta["data"]["usage"]["cache_read_input_tokens"] == 12


@pytest.mark.asyncio
async def test_anthropic_format_tool_use_block_passes_through():
    """Anthropic 原生格式的 tool_use 内容块应被保留，不被过滤."""
    chunks = [
        'event: message_start\ndata: {"type":"message_start","message":{"id":"msg_1","type":"message","role":"assistant","content":[],"model":"glm-5.1","usage":{"input_tokens":10,"output_tokens":0}}}\n\n',
        'event: content_block_start\ndata: {"type":"content_block_start","index":0,"content_block":{"type":"tool_use","id":"toolu_01","name":"bash","input":{}}}\n\n',
        'event: content_block_delta\ndata: {"type":"content_block_delta","index":0,"delta":{"type":"input_json_delta","partial_json":"{\\"cmd\\":\\""}}\n\n',
        'event: content_block_stop\ndata: {"type":"content_block_stop","index":0}\n\n',
        'event: message_delta\ndata: {"type":"message_delta","delta":{"stop_reason":"tool_use","stop_sequence":null},"usage":{"output_tokens":5}}\n\n',
        'event: message_stop\ndata: {"type":"message_stop"}\n\n',
    ]

    collected = []
    async for chunk in normalize_anthropic_compatible_stream(
        _raw_chunks(chunks), model="glm-5.1",
    ):
        collected.append(chunk)

    events = _parse_events(collected)
    event_types = [event["event"] for event in events]
    # tool_use 块应被保留
    tool_start = next(
        (event for event in events if event["event"] == "content_block_start"),
        None,
    )
    assert tool_start is not None, "tool_use content_block_start 应被保留"
    assert tool_start["data"]["content_block"]["type"] == "tool_use"
    assert tool_start["data"]["content_block"]["name"] == "bash"
    # input_json_delta 应被保留
    tool_delta = next(
        (event for event in events if event["event"] == "content_block_delta"),
        None,
    )
    assert tool_delta is not None, "input_json_delta content_block_delta 应被保留"
    assert tool_delta["data"]["delta"]["type"] == "input_json_delta"
    assert "message_stop" in event_types


@pytest.mark.asyncio
async def test_anthropic_format_vendor_block_still_filtered():
    """供应商私有块类型（如 server_tool_use）仍被过滤，标准块类型被保留."""
    chunks = [
        # 私有类型 server_tool_use → 应被过滤
        'event: content_block_start\ndata: {"type":"content_block_start","index":0,"content_block":{"type":"server_tool_use","id":"t1"}}\n\n',
        # 私有 delta 类型 → 应被过滤
        'event: content_block_delta\ndata: {"type":"content_block_delta","index":0,"delta":{"type":"server_tool_use_delta","partial":"x"}}\n\n',
        # 标准 text 块 → 应被保留
        'event: content_block_start\ndata: {"type":"content_block_start","index":1,"content_block":{"type":"text","text":""}}\n\n',
        'event: content_block_delta\ndata: {"type":"content_block_delta","index":1,"delta":{"type":"text_delta","text":"ok"}}\n\n',
        'event: message_stop\ndata: {"type":"message_stop"}\n\n',
    ]

    collected = []
    async for chunk in normalize_anthropic_compatible_stream(
        _raw_chunks(chunks), model="glm-5.1",
    ):
        collected.append(chunk)

    events = _parse_events(collected)
    block_starts = [e for e in events if e["event"] == "content_block_start"]
    block_deltas = [e for e in events if e["event"] == "content_block_delta"]
    # server_tool_use 块被过滤，只剩 text 块
    assert len(block_starts) == 1
    assert block_starts[0]["data"]["content_block"]["type"] == "text"
    # server_tool_use_delta 被过滤，只剩 text_delta
    assert len(block_deltas) == 1
    assert block_deltas[0]["data"]["delta"]["type"] == "text_delta"


@pytest.mark.asyncio
async def test_anthropic_format_thinking_block_passes_through():
    """Anthropic 原生格式的 thinking 内容块应被保留，不被过滤."""
    chunks = [
        'event: message_start\ndata: {"type":"message_start","message":{"id":"msg_1","type":"message","role":"assistant","content":[],"model":"glm-5.1","usage":{"input_tokens":10,"output_tokens":0}}}\n\n',
        'event: content_block_start\ndata: {"type":"content_block_start","index":0,"content_block":{"type":"thinking","thinking":""}}\n\n',
        'event: content_block_delta\ndata: {"type":"content_block_delta","index":0,"delta":{"type":"thinking_delta","thinking":"Let me think..."}}\n\n',
        'event: content_block_stop\ndata: {"type":"content_block_stop","index":0}\n\n',
        'event: content_block_start\ndata: {"type":"content_block_start","index":1,"content_block":{"type":"text","text":""}}\n\n',
        'event: content_block_delta\ndata: {"type":"content_block_delta","index":1,"delta":{"type":"text_delta","text":"The answer is 42."}}\n\n',
        'event: content_block_stop\ndata: {"type":"content_block_stop","index":1}\n\n',
        'event: message_delta\ndata: {"type":"message_delta","delta":{"stop_reason":"end_turn","stop_sequence":null},"usage":{"output_tokens":15}}\n\n',
        'event: message_stop\ndata: {"type":"message_stop"}\n\n',
    ]

    collected = []
    async for chunk in normalize_anthropic_compatible_stream(
        _raw_chunks(chunks), model="glm-5.1",
    ):
        collected.append(chunk)

    events = _parse_events(collected)
    block_starts = [e for e in events if e["event"] == "content_block_start"]
    block_deltas = [e for e in events if e["event"] == "content_block_delta"]
    # thinking 和 text 两个块都应被保留
    assert len(block_starts) == 2
    assert block_starts[0]["data"]["content_block"]["type"] == "thinking"
    assert block_starts[1]["data"]["content_block"]["type"] == "text"
    # thinking_delta 和 text_delta 都应被保留
    assert len(block_deltas) == 2
    assert block_deltas[0]["data"]["delta"]["type"] == "thinking_delta"
    assert block_deltas[0]["data"]["delta"]["thinking"] == "Let me think..."
    assert block_deltas[1]["data"]["delta"]["type"] == "text_delta"
    assert block_deltas[1]["data"]["delta"]["text"] == "The answer is 42."


@pytest.mark.asyncio
async def test_openai_format_reasoning_content_converted_to_thinking():
    """OpenAI/智谱格式的 reasoning_content 应被转换为 Anthropic thinking 内容块."""
    chunks = [
        'data: {"id":"chatcmpl-1","model":"glm-5.1","choices":[{"delta":{"reasoning_content":"Let me think step by step..."},"finish_reason":null}]}\n\n',
        'data: {"id":"chatcmpl-1","model":"glm-5.1","choices":[{"delta":{"reasoning_content":" First, I need to consider..."},"finish_reason":null}]}\n\n',
        'data: {"id":"chatcmpl-1","model":"glm-5.1","choices":[{"delta":{"content":"The answer is 42."},"finish_reason":null}]}\n\n',
        'data: {"choices":[{"delta":{"content":""},"finish_reason":"stop"}],"usage":{"prompt_tokens":5,"completion_tokens":10}}\n\n',
        "data: [DONE]\n\n",
    ]

    collected = []
    async for chunk in normalize_anthropic_compatible_stream(
        _raw_chunks(chunks), model="glm-5.1",
    ):
        collected.append(chunk)

    events = _parse_events(collected)
    block_starts = [e for e in events if e["event"] == "content_block_start"]
    block_deltas = [e for e in events if e["event"] == "content_block_delta"]
    # 应有 thinking 块 + text 块
    assert len(block_starts) == 2
    assert block_starts[0]["data"]["content_block"]["type"] == "thinking"
    assert block_starts[1]["data"]["content_block"]["type"] == "text"
    # thinking_delta 片段
    thinking_deltas = [d for d in block_deltas if d["data"]["delta"]["type"] == "thinking_delta"]
    assert len(thinking_deltas) == 2
    assert thinking_deltas[0]["data"]["delta"]["thinking"] == "Let me think step by step..."
    assert thinking_deltas[1]["data"]["delta"]["thinking"] == " First, I need to consider..."
    # text_delta 片段
    text_deltas = [d for d in block_deltas if d["data"]["delta"]["type"] == "text_delta"]
    assert len(text_deltas) == 1
    assert text_deltas[0]["data"]["delta"]["text"] == "The answer is 42."


@pytest.mark.asyncio
async def test_openai_tool_call_stream_is_converted():
    """OpenAI tool_calls 增量流被转为 Anthropic tool_use 事件."""
    chunks = [
        'data: {"id":"chatcmpl-1","model":"claude-opus-4","choices":[{"delta":{"tool_calls":[{"index":0,"id":"call_1","type":"function","function":{"name":"get_weather"}}]},"finish_reason":null}]}\n\n',
        'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"function":{"arguments":"{\\"city\\":\\"Tokyo\\"}"}}],"content":null},"finish_reason":"tool_calls"}],"usage":{"prompt_tokens":9,"completion_tokens":4}}\n\n',
        "data: [DONE]\n\n",
    ]

    collected = []
    async for chunk in normalize_anthropic_compatible_stream(
        _raw_chunks(chunks), model="claude-opus-4",
    ):
        collected.append(chunk)

    events = _parse_events(collected)
    tool_start = next(event for event in events if event["event"] == "content_block_start")
    tool_delta = next(event for event in events if event["event"] == "content_block_delta")
    message_delta = next(event for event in events if event["event"] == "message_delta")
    assert tool_start["data"]["content_block"]["type"] == "tool_use"
    assert tool_start["data"]["content_block"]["name"] == "get_weather"
    assert tool_delta["data"]["delta"]["type"] == "input_json_delta"
    assert message_delta["data"]["delta"]["stop_reason"] == "tool_use"
