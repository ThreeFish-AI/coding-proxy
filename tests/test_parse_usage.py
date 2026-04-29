"""SSE chunk 用量解析单元测试 — 覆盖 Anthropic / OpenAI(Zhipu) / 混合格式."""

from coding.proxy.routing.usage_parser import _set_if_nonzero, parse_usage_from_chunk


def _sse(data_str: str) -> bytes:
    """构造 SSE data 行的 bytes."""
    return f"data: {data_str}\n\n".encode()


# --- _set_if_nonzero 测试 ---


def test_set_if_nonzero_positive():
    d: dict = {}
    _set_if_nonzero(d, "key", 42)
    assert d["key"] == 42


def test_set_if_nonzero_zero_skips():
    d: dict = {"key": 100}
    _set_if_nonzero(d, "key", 0)
    assert d["key"] == 100


def test_set_if_nonzero_negative():
    d: dict = {}
    _set_if_nonzero(d, "key", -1)
    assert d["key"] == -1


# --- Anthropic 原生格式 ---


def test_anthropic_message_start_and_delta():
    """Anthropic 标准: message_start 含 input_tokens, message_delta 含 output_tokens."""
    usage: dict = {}

    # message_start
    parse_usage_from_chunk(
        _sse(
            '{"type":"message_start","message":{"id":"msg_123","model":"claude-sonnet-4-20250514",'
            '"usage":{"input_tokens":100,"cache_creation_input_tokens":10,"cache_read_input_tokens":5}}}'
        ),
        usage,
    )
    assert usage["input_tokens"] == 100
    assert usage["cache_creation_tokens"] == 10
    assert usage["cache_read_tokens"] == 5
    assert usage["request_id"] == "msg_123"
    assert usage["model_served"] == "claude-sonnet-4-20250514"

    # message_delta
    parse_usage_from_chunk(
        _sse(
            '{"type":"message_delta","delta":{"stop_reason":"end_turn"},'
            '"usage":{"output_tokens":50}}'
        ),
        usage,
    )
    assert usage["output_tokens"] == 50
    # input_tokens 不被覆盖
    assert usage["input_tokens"] == 100


def test_anthropic_empty_usage():
    """message_start 中 usage 为空对象，后续有输出."""
    usage: dict = {}
    parse_usage_from_chunk(
        _sse('{"type":"message_start","message":{"id":"msg_abc","usage":{}}}'), usage
    )
    assert usage.get("input_tokens", 0) == 0

    parse_usage_from_chunk(
        _sse('{"type":"message_delta","delta":{},"usage":{"output_tokens":30}}'), usage
    )
    assert usage["output_tokens"] == 30


def test_anthropic_cache_only_input_signal():
    """Anthropic prompt caching 场景下，cache tokens 本身就是有效输入信号."""
    usage: dict = {}
    parse_usage_from_chunk(
        _sse(
            '{"type":"message_start","message":{"id":"msg_cache_only","usage":'
            '{"input_tokens":0,"cache_creation_input_tokens":720,"cache_read_input_tokens":82408}}}'
        ),
        usage,
    )
    parse_usage_from_chunk(
        _sse(
            '{"type":"message_delta","delta":{"stop_reason":"end_turn"},"usage":{"output_tokens":8220}}'
        ),
        usage,
    )

    assert usage.get("input_tokens", 0) == 0
    assert usage["cache_creation_tokens"] == 720
    assert usage["cache_read_tokens"] == 82408
    assert usage["output_tokens"] == 8220


# --- OpenAI / Zhipu 格式 ---


def test_openai_zhipu_final_chunk():
    """Zhipu 最后一个 chunk: 顶层 usage 含 prompt_tokens / completion_tokens."""
    usage: dict = {}
    parse_usage_from_chunk(
        _sse(
            '{"id":"chatcmpl-1","model":"glm-5.1",'
            '"choices":[{"index":0,"finish_reason":"stop","delta":{"role":"assistant","content":""}}],'
            '"usage":{"prompt_tokens":200,"completion_tokens":80,"total_tokens":280}}'
        ),
        usage,
    )
    assert usage["input_tokens"] == 200
    assert usage["output_tokens"] == 80
    assert usage["request_id"] == "chatcmpl-1"
    assert usage["model_served"] == "glm-5.1"


def test_openai_final_chunk_with_model():
    """OpenAI 最终 chunk 有 model 字段时应提取到 model_served."""
    usage: dict = {}
    parse_usage_from_chunk(
        _sse(
            '{"id":"chatcmpl-2","model":"gpt-4o-2024-08-06",'
            '"usage":{"prompt_tokens":50,"completion_tokens":20}}'
        ),
        usage,
    )
    assert usage["input_tokens"] == 50
    assert usage["output_tokens"] == 20
    assert usage["model_served"] == "gpt-4o-2024-08-06"


def test_openai_final_chunk_without_model():
    """OpenAI 最终 chunk 无 model 字段时不应设置 model_served."""
    usage: dict = {}
    parse_usage_from_chunk(
        _sse('{"id":"chatcmpl-3","usage":{"prompt_tokens":30,"completion_tokens":10}}'),
        usage,
    )
    assert usage["input_tokens"] == 30
    assert usage["output_tokens"] == 10
    assert "model_served" not in usage


def test_openai_final_chunk_with_cache_tokens():
    """OpenAI/Copilot 风格最终 chunk 应提取 cache read / creation tokens."""
    usage: dict = {}
    parse_usage_from_chunk(
        _sse(
            '{"id":"chatcmpl-cache","usage":{"prompt_tokens":120,"completion_tokens":30,'
            '"cache_read_input_tokens":40,"cache_creation_input_tokens":10}}'
        ),
        usage,
    )
    assert usage["input_tokens"] == 120
    assert usage["output_tokens"] == 30
    assert usage["cache_read_tokens"] == 40
    assert usage["cache_creation_tokens"] == 10
    assert usage["request_id"] == "chatcmpl-cache"


def test_openai_zhipu_content_chunks_no_usage():
    """Zhipu 中间 chunk 不含 usage，不应干扰."""
    usage: dict = {}
    parse_usage_from_chunk(
        _sse(
            '{"id":"chatcmpl-1","choices":[{"index":0,"delta":{"content":"Hello"},"finish_reason":null}]}'
        ),
        usage,
    )
    assert usage.get("input_tokens", 0) == 0
    assert usage.get("output_tokens", 0) == 0

    # 最后一个 chunk 才有 usage
    parse_usage_from_chunk(
        _sse(
            '{"id":"chatcmpl-1","choices":[{"index":0,"finish_reason":"stop",'
            '"delta":{"role":"assistant","content":""}}],'
            '"usage":{"prompt_tokens":50,"completion_tokens":10}}'
        ),
        usage,
    )
    assert usage["input_tokens"] == 50
    assert usage["output_tokens"] == 10


# --- 混合格式 ---


def test_mixed_anthropic_input_openai_output():
    """Anthropic message_start 提供 input_tokens, Zhipu 最后 chunk 提供 completion_tokens."""
    usage: dict = {}
    parse_usage_from_chunk(
        _sse(
            '{"type":"message_start","message":{"id":"msg_mix","usage":{"input_tokens":150}}}'
        ),
        usage,
    )
    assert usage["input_tokens"] == 150

    parse_usage_from_chunk(
        _sse(
            '{"id":"mix-1","choices":[{"finish_reason":"stop","delta":{}}],'
            '"usage":{"completion_tokens":40}}'
        ),
        usage,
    )
    assert usage["output_tokens"] == 40
    assert usage["input_tokens"] == 150  # 不被后续 chunk 覆盖


# --- 零值保护 ---


def test_zero_does_not_overwrite_nonzero():
    """后续 chunk 的 0 值不应覆盖已提取的非零值."""
    usage: dict = {}
    parse_usage_from_chunk(
        _sse(
            '{"type":"message_start","message":{"id":"msg_z","usage":{"input_tokens":100}}}'
        ),
        usage,
    )
    assert usage["input_tokens"] == 100

    # 另一个 message_start 带 input_tokens=0（如 Anthropic 某些事件）
    parse_usage_from_chunk(
        _sse('{"type":"message_start","message":{"usage":{"input_tokens":0}}}'), usage
    )
    assert usage["input_tokens"] == 100


# --- request_id fallback ---


def test_request_id_from_top_level():
    """OpenAI 格式: id 在顶层而非 message 内."""
    usage: dict = {}
    parse_usage_from_chunk(
        _sse(
            '{"id":"top-level-id","usage":{"prompt_tokens":10,"completion_tokens":5}}'
        ),
        usage,
    )
    assert usage["request_id"] == "top-level-id"


def test_request_id_message_priority():
    """message.id 应优先于顶层 data.id."""
    usage: dict = {}
    parse_usage_from_chunk(
        _sse('{"id":"top-id","message":{"id":"msg-id","usage":{"input_tokens":10}}}'),
        usage,
    )
    assert usage["request_id"] == "msg-id"

    # 后续顶层 id 不应覆盖已设置的 message.id
    parse_usage_from_chunk(
        _sse('{"id":"another-top-id","usage":{"output_tokens":5}}'), usage
    )
    assert usage["request_id"] == "msg-id"


# --- 边界情况 ---


def test_done_marker():
    """data: [DONE] 不应导致解析错误."""
    usage: dict = {}
    parse_usage_from_chunk(b"data: [DONE]\n\n", usage)
    assert usage == {}


def test_invalid_json_skipped():
    """无效 JSON 应被静默跳过."""
    usage: dict = {}
    parse_usage_from_chunk(b"data: {invalid json}\n\n", usage)
    assert usage == {}


def test_multiple_sse_lines_in_single_chunk():
    """一个 TCP chunk 包含多个 SSE 行."""
    usage: dict = {}
    chunk = (
        b'data: {"type":"message_start","message":{"id":"msg_multi","usage":{"input_tokens":80}}}\n\n'
        b'data: {"type":"message_delta","delta":{},"usage":{"output_tokens":20}}\n\n'
    )
    parse_usage_from_chunk(chunk, usage)
    assert usage["input_tokens"] == 80
    assert usage["output_tokens"] == 20


# --- null usage 安全保护（防御上游 SSE 字段为 null 的极端格式） ---


def test_null_usage_at_top_level_does_not_raise():
    """data.usage 显式为 null 时应被静默忽略，不抛异常、不产生 WARNING."""
    usage: dict = {}
    parse_usage_from_chunk(
        _sse('{"id":"chatcmpl-null","choices":[{"delta":{}}],"usage":null}'),
        usage,
    )
    # 不应写入任何 token 字段
    assert usage.get("input_tokens", 0) == 0
    assert usage.get("output_tokens", 0) == 0


def test_null_usage_in_message_does_not_raise():
    """message.usage 显式为 null 时应被静默忽略."""
    usage: dict = {}
    parse_usage_from_chunk(
        _sse('{"type":"message_start","message":{"id":"msg_null","usage":null}}'),
        usage,
    )
    assert usage.get("input_tokens", 0) == 0


def test_null_usage_does_not_break_subsequent_valid_chunks():
    """null usage 帧之后到来的有效帧仍能正确解析."""
    usage: dict = {}
    # 1. null usage 帧
    parse_usage_from_chunk(
        _sse('{"id":"chatcmpl-1","choices":[{"delta":{"content":"hi"}}],"usage":null}'),
        usage,
    )
    # 2. 有效的最终帧
    parse_usage_from_chunk(
        _sse(
            '{"id":"chatcmpl-1","choices":[{"finish_reason":"stop","delta":{}}],'
            '"usage":{"prompt_tokens":12,"completion_tokens":3}}'
        ),
        usage,
    )
    assert usage["input_tokens"] == 12
    assert usage["output_tokens"] == 3
