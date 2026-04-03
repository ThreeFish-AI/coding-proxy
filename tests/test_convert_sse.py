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


# ── 新增测试用例 ──────────────────────────────────────


@pytest.mark.asyncio
async def test_multi_block_text_then_thinking():
    """文本块 → 思考块的 block 切换."""
    c1 = _make_candidate([{"text": "First"}])
    c2 = _make_candidate([{"text": "Hmm...", "thought": True}])
    c3 = _make_candidate([{"text": "done thinking", "thought": True}], finish="STOP")

    collected = []
    async for c in adapt_sse_stream(_chunks_from([
        _dump(c1), _dump(c2), _dump(c3),
    ]), model="test"):
        collected.append(c)

    events = _parse_events(collected)
    starts = [e for e in events if e["event"] == "content_block_start"]
    assert len(starts) >= 2
    assert starts[0]["data"]["content_block"]["type"] == "text"
    assert starts[1]["data"]["content_block"]["type"] == "thinking"


@pytest.mark.asyncio
async def test_complex_three_block_sequence():
    """思考 → 文本 → 工具调用的三块序列."""
    c1 = _make_candidate([{"text": "thinking...", "thought": True}])
    c2 = _make_candidate([{"text": "I'll call the tool."}])
    c3 = _make_candidate([{
        "functionCall": {"id": "fc_1", "name": "read_file", "args": {"path": "/tmp"}},
    }], finish="STOP")

    collected = []
    async for c in adapt_sse_stream(_chunks_from([
        _dump(c1), _dump(c2), _dump(c3),
    ]), model="test"):
        collected.append(c)

    events = _parse_events(collected)
    starts = [e for e in events if e["event"] == "content_block_start"]
    assert len(starts) == 3
    types = [s["data"]["content_block"]["type"] for s in starts]
    assert types == ["thinking", "text", "tool_use"]
    msg_delta = next(e for e in events if e["event"] == "message_delta")
    assert msg_delta["data"]["delta"]["stop_reason"] == "tool_use"


@pytest.mark.asyncio
async def test_usage_across_multiple_chunks():
    """usage 从多个 chunk 累积."""
    c1 = _make_candidate([{"text": "A"}], prompt_tokens=10)
    c2 = _make_candidate([{"text": "B"}], prompt_tokens=10, cand_tokens=3)
    c3 = _make_candidate([{"text": "C"}], finish="STOP", cand_tokens=5)

    collected = []
    async for c in adapt_sse_stream(_chunks_from([
        _dump(c1), _dump(c2), _dump(c3),
    ]), model="test"):
        collected.append(c)

    events = _parse_events(collected)
    msg_start = next(e for e in events if e["event"] == "message_start")
    assert msg_start["data"]["message"]["usage"]["input_tokens"] == 10
    msg_delta = next(e for e in events if e["event"] == "message_delta")
    assert msg_delta["data"]["usage"]["output_tokens"] == 5


@pytest.mark.asyncio
async def test_stream_safety_finish_reason():
    """SAFETY finishReason → end_turn."""
    data = _make_candidate([{"text": "blocked?"}], finish="SAFETY")

    collected = []
    async for c in adapt_sse_stream(_chunks_from([_dump(data)]), model="test"):
        collected.append(c)

    events = _parse_events(collected)
    msg_delta = next(e for e in events if e["event"] == "message_delta")
    assert msg_delta["data"]["delta"]["stop_reason"] == "end_turn"


@pytest.mark.asyncio
async def test_stream_recitation_finish_reason():
    """RECITATION finishReason → end_turn."""
    data = _make_candidate([{"text": "quoted text"}], finish="RECITATION")

    collected = []
    async for c in adapt_sse_stream(_chunks_from([_dump(data)]), model="test"):
        collected.append(c)

    events = _parse_events(collected)
    msg_delta = next(e for e in events if e["event"] == "message_delta")
    assert msg_delta["data"]["delta"]["stop_reason"] == "end_turn"


@pytest.mark.asyncio
async def test_consecutive_text_chunks_merge():
    """连续同类型文本 chunk 合并为同一 content_block."""
    c1 = _make_candidate([{"text": "Hello"}])
    c2 = _make_candidate([{"text": " World"}])
    c3 = _make_candidate([{"text": "!"}], finish="STOP")

    collected = []
    async for c in adapt_sse_stream(_chunks_from([
        _dump(c1), _dump(c2), _dump(c3),
    ]), model="test"):
        collected.append(c)

    events = _parse_events(collected)
    starts = [e for e in events if e["event"] == "content_block_start"]
    assert len(starts) == 1
    assert starts[0]["data"]["content_block"]["type"] == "text"

    deltas = [e for e in events if e["event"] == "content_block_delta"]
    assert len(deltas) == 3


@pytest.mark.asyncio
async def test_malformed_json_chunk_skipped():
    """非法 JSON data 行被跳过不报错."""
    good = _make_candidate([{"text": "OK"}], finish="STOP")

    collected = []
    async for c in adapt_sse_stream(
        _chunks_from(["{broken json}", _dump(good)]), model="test",
    ):
        collected.append(c)

    events = _parse_events(collected)
    deltas = [e for e in events if e["event"] == "content_block_delta"]
    assert len(deltas) == 1
    assert deltas[0]["data"]["delta"]["text"] == "OK"


@pytest.mark.asyncio
async def test_empty_stream_emits_close_events():
    """零 chunk 流仍发送完整关闭事件序列."""
    collected = []
    async for c in adapt_sse_stream(_chunks_from([]), model="test"):
        collected.append(c)

    events = _parse_events(collected)
    event_types = [e["event"] for e in events]
    assert "message_delta" in event_types
    assert "message_stop" in event_types


@pytest.mark.asyncio
async def test_function_call_with_preexisting_id():
    """functionCall 带 id 时保留该 id 作为 tool_use id."""
    data = _make_candidate([{
        "functionCall": {
            "id": "my_custom_id_42",
            "name": "exec_cmd",
            "args": {"cmd": "ls"},
        }
    }], finish="STOP")

    collected = []
    async for c in adapt_sse_stream(_chunks_from([_dump(data)]), model="test"):
        collected.append(c)

    events = _parse_events(collected)
    start = next(e for e in events if e["event"] == "content_block_start")
    assert start["data"]["content_block"]["id"] == "my_custom_id_42"


# ── 辅助函数 ──────────────────────────────────────────


def _make_candidate(parts, *, finish=None, prompt_tokens=None, cand_tokens=None):
    """构造单个 candidate 的 Gemini 响应字典."""
    cand = {
        "content": {"parts": parts, "role": "model"},
    }
    if finish:
        cand["finishReason"] = finish
    resp = {"candidates": [cand]}
    meta = {}
    if prompt_tokens is not None:
        meta["promptTokenCount"] = prompt_tokens
    if cand_tokens is not None:
        meta["candidatesTokenCount"] = cand_tokens
    if meta:
        resp["usageMetadata"] = meta
    return resp


def _dump(obj):
    return json.dumps(obj)
