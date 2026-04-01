"""Gemini SSE → Anthropic SSE 流适配器单元测试."""

from __future__ import annotations

import json

import pytest

from coding.proxy.convert.gemini_sse_adapter import adapt_sse_stream


async def _chunks_from(data_list: list[str]):
    """辅助：将 data 字符串列表转换为 SSE 字节 AsyncIterator."""
    for item in data_list:
        yield f"data: {item}\n\n".encode()


def _parse_events(raw_bytes_list: list[bytes]) -> list[dict]:
    """辅助：解析 Anthropic SSE 事件列表."""
    events = []
    for raw in raw_bytes_list:
        text = raw.decode()
        for block in text.strip().split("\n\n"):
            lines = block.strip().split("\n")
            event_type = None
            data = None
            for line in lines:
                if line.startswith("event: "):
                    event_type = line[7:]
                elif line.startswith("data: "):
                    data = json.loads(line[6:])
            if event_type and data:
                events.append({"event": event_type, "data": data})
    return events


# --- 基础流转换 ---


@pytest.mark.asyncio
async def test_single_chunk_with_finish():
    """单个包含文本和 finishReason 的 chunk."""
    gemini_data = json.dumps({
        "candidates": [{
            "content": {"parts": [{"text": "Hello!"}], "role": "model"},
            "finishReason": "STOP",
        }],
        "usageMetadata": {"promptTokenCount": 5, "candidatesTokenCount": 2},
    })

    collected = []
    async for chunk in adapt_sse_stream(
        _chunks_from([gemini_data]),
        model="claude-sonnet-4-20250514",
        request_id="msg_test",
    ):
        collected.append(chunk)

    events = _parse_events(collected)
    event_types = [e["event"] for e in events]

    assert "message_start" in event_types
    assert "content_block_start" in event_types
    assert "content_block_delta" in event_types
    assert "content_block_stop" in event_types
    assert "message_delta" in event_types
    assert "message_stop" in event_types

    # 验证 message_start
    msg_start = next(e for e in events if e["event"] == "message_start")
    assert msg_start["data"]["message"]["id"] == "msg_test"
    assert msg_start["data"]["message"]["model"] == "claude-sonnet-4-20250514"
    assert msg_start["data"]["message"]["usage"]["input_tokens"] == 5

    # 验证 content_block_delta
    delta = next(e for e in events if e["event"] == "content_block_delta")
    assert delta["data"]["delta"]["text"] == "Hello!"

    # 验证 message_delta
    msg_delta = next(e for e in events if e["event"] == "message_delta")
    assert msg_delta["data"]["delta"]["stop_reason"] == "end_turn"
    assert msg_delta["data"]["usage"]["output_tokens"] == 2


@pytest.mark.asyncio
async def test_multi_chunk_stream():
    """多个 chunk 的流式响应."""
    chunk1 = json.dumps({
        "candidates": [{
            "content": {"parts": [{"text": "Hello"}], "role": "model"},
        }],
        "usageMetadata": {"promptTokenCount": 10},
    })
    chunk2 = json.dumps({
        "candidates": [{
            "content": {"parts": [{"text": " World"}], "role": "model"},
        }],
    })
    chunk3 = json.dumps({
        "candidates": [{
            "content": {"parts": [{"text": "!"}], "role": "model"},
            "finishReason": "STOP",
        }],
        "usageMetadata": {"promptTokenCount": 10, "candidatesTokenCount": 8},
    })

    collected = []
    async for chunk in adapt_sse_stream(
        _chunks_from([chunk1, chunk2, chunk3]),
        model="claude-sonnet-4-20250514",
    ):
        collected.append(chunk)

    events = _parse_events(collected)
    event_types = [e["event"] for e in events]

    # message_start 只出现一次
    assert event_types.count("message_start") == 1

    # content_block_delta 出现 3 次
    deltas = [e for e in events if e["event"] == "content_block_delta"]
    assert len(deltas) == 3
    assert deltas[0]["data"]["delta"]["text"] == "Hello"
    assert deltas[1]["data"]["delta"]["text"] == " World"
    assert deltas[2]["data"]["delta"]["text"] == "!"


@pytest.mark.asyncio
async def test_max_tokens_finish_reason():
    """MAX_TOKENS finishReason 映射为 max_tokens."""
    gemini_data = json.dumps({
        "candidates": [{
            "content": {"parts": [{"text": "partial..."}], "role": "model"},
            "finishReason": "MAX_TOKENS",
        }],
        "usageMetadata": {"candidatesTokenCount": 100},
    })

    collected = []
    async for chunk in adapt_sse_stream(
        _chunks_from([gemini_data]),
        model="claude-sonnet-4-20250514",
    ):
        collected.append(chunk)

    events = _parse_events(collected)
    msg_delta = next(e for e in events if e["event"] == "message_delta")
    assert msg_delta["data"]["delta"]["stop_reason"] == "max_tokens"


@pytest.mark.asyncio
async def test_empty_text_chunk_skipped():
    """空文本 chunk 不产生 delta."""
    chunk1 = json.dumps({
        "candidates": [{
            "content": {"parts": [{"text": ""}], "role": "model"},
        }],
    })
    chunk2 = json.dumps({
        "candidates": [{
            "content": {"parts": [{"text": "Hi"}], "role": "model"},
            "finishReason": "STOP",
        }],
    })

    collected = []
    async for chunk in adapt_sse_stream(
        _chunks_from([chunk1, chunk2]),
        model="test",
    ):
        collected.append(chunk)

    events = _parse_events(collected)
    deltas = [e for e in events if e["event"] == "content_block_delta"]
    # 空文本不产生 delta，只有 "Hi" 产生
    assert len(deltas) == 1
    assert deltas[0]["data"]["delta"]["text"] == "Hi"


@pytest.mark.asyncio
async def test_stream_without_finish_reason():
    """流正常结束但未收到 finishReason → 补发关闭事件."""
    chunk = json.dumps({
        "candidates": [{
            "content": {"parts": [{"text": "Hello"}], "role": "model"},
        }],
    })

    collected = []
    async for c in adapt_sse_stream(_chunks_from([chunk]), model="test"):
        collected.append(c)

    events = _parse_events(collected)
    event_types = [e["event"] for e in events]

    # 必须包含完整的关闭序列
    assert "content_block_stop" in event_types
    assert "message_delta" in event_types
    assert "message_stop" in event_types

    msg_delta = next(e for e in events if e["event"] == "message_delta")
    assert msg_delta["data"]["delta"]["stop_reason"] == "end_turn"


@pytest.mark.asyncio
async def test_no_candidates_chunk_ignored():
    """无 candidates 的 chunk 被忽略."""
    chunk1 = json.dumps({"usageMetadata": {"promptTokenCount": 5}})
    chunk2 = json.dumps({
        "candidates": [{
            "content": {"parts": [{"text": "OK"}], "role": "model"},
            "finishReason": "STOP",
        }],
        "usageMetadata": {"candidatesTokenCount": 1},
    })

    collected = []
    async for c in adapt_sse_stream(_chunks_from([chunk1, chunk2]), model="test"):
        collected.append(c)

    events = _parse_events(collected)
    deltas = [e for e in events if e["event"] == "content_block_delta"]
    assert len(deltas) == 1
    assert deltas[0]["data"]["delta"]["text"] == "OK"


@pytest.mark.asyncio
async def test_auto_generated_request_id():
    """未指定 request_id 时自动生成."""
    chunk = json.dumps({
        "candidates": [{
            "content": {"parts": [{"text": "Hi"}], "role": "model"},
            "finishReason": "STOP",
        }],
        "usageMetadata": {},
    })

    collected = []
    async for c in adapt_sse_stream(_chunks_from([chunk]), model="test"):
        collected.append(c)

    events = _parse_events(collected)
    msg_start = next(e for e in events if e["event"] == "message_start")
    assert msg_start["data"]["message"]["id"].startswith("msg_")


@pytest.mark.asyncio
async def test_stream_function_call_maps_to_tool_use():
    gemini_data = json.dumps({
        "candidates": [{
            "content": {"parts": [{
                "functionCall": {"id": "fc_1", "name": "search_docs", "args": {"query": "gemini"}},
            }], "role": "model"},
            "finishReason": "STOP",
        }],
        "usageMetadata": {"candidatesTokenCount": 3},
    })

    collected = []
    async for chunk in adapt_sse_stream(_chunks_from([gemini_data]), model="claude-sonnet-4"):
        collected.append(chunk)

    events = _parse_events(collected)
    start = next(e for e in events if e["event"] == "content_block_start")
    delta = next(e for e in events if e["event"] == "content_block_delta")
    msg_delta = next(e for e in events if e["event"] == "message_delta")
    assert start["data"]["content_block"]["type"] == "tool_use"
    assert start["data"]["content_block"]["name"] == "search_docs"
    assert delta["data"]["delta"]["type"] == "input_json_delta"
    assert msg_delta["data"]["delta"]["stop_reason"] == "tool_use"


@pytest.mark.asyncio
async def test_stream_thought_part_maps_to_thinking_delta():
    gemini_data = json.dumps({
        "candidates": [{
            "content": {"parts": [{"text": "先分析", "thought": True}], "role": "model"},
            "finishReason": "STOP",
        }],
    })

    collected = []
    async for chunk in adapt_sse_stream(_chunks_from([gemini_data]), model="claude-sonnet-4"):
        collected.append(chunk)

    events = _parse_events(collected)
    start = next(e for e in events if e["event"] == "content_block_start")
    delta = next(e for e in events if e["event"] == "content_block_delta")
    assert start["data"]["content_block"]["type"] == "thinking"
    assert delta["data"]["delta"]["type"] == "thinking_delta"
    assert delta["data"]["delta"]["thinking"] == "先分析"
