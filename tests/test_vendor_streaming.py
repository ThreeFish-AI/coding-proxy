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
