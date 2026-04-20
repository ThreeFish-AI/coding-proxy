"""Gemini usageMetadata 流式用量解析单元测试.

验证 parse_usage_from_chunk 对 Gemini SSE 结构的识别与归一化，与既有 Anthropic/OpenAI
分支完全正交，不互相影响。
"""

from __future__ import annotations

from coding.proxy.routing.usage_parser import (
    build_usage_evidence_records,
    parse_usage_from_chunk,
)


def _sse(data_str: str) -> bytes:
    return f"data: {data_str}\n\n".encode()


# ── 基础字段归一化 ─────────────────────────────────────────


def test_gemini_usage_metadata_basic_fields():
    usage: dict = {}
    parse_usage_from_chunk(
        _sse(
            '{"candidates":[{"content":{"parts":[{"text":"hi"}]}}],'
            '"usageMetadata":{"promptTokenCount":120,"candidatesTokenCount":42,'
            '"totalTokenCount":162},'
            '"responseId":"resp_abc","modelVersion":"gemini-2.0-flash"}'
        ),
        usage,
        vendor_label="Gemini",
    )
    assert usage["input_tokens"] == 120
    assert usage["output_tokens"] == 42
    assert usage.get("cache_read_tokens", 0) == 0
    assert usage["request_id"] == "resp_abc"


def test_gemini_usage_metadata_with_cached_content():
    """含 cachedContentTokenCount → cache_read_tokens."""
    usage: dict = {}
    parse_usage_from_chunk(
        _sse(
            '{"usageMetadata":{"promptTokenCount":200,"candidatesTokenCount":50,'
            '"cachedContentTokenCount":150,"totalTokenCount":250}}'
        ),
        usage,
    )
    assert usage["input_tokens"] == 200
    assert usage["output_tokens"] == 50
    assert usage["cache_read_tokens"] == 150


def test_gemini_usage_metadata_extra_fields_go_to_extra_usage():
    """thoughtsTokenCount / toolUsePromptTokenCount 归入 extra_usage 字典."""
    usage: dict = {}
    parse_usage_from_chunk(
        _sse(
            '{"usageMetadata":{"promptTokenCount":100,"candidatesTokenCount":40,'
            '"thoughtsTokenCount":256,"toolUsePromptTokenCount":12}}'
        ),
        usage,
    )
    assert usage["input_tokens"] == 100
    assert usage["output_tokens"] == 40
    extra = usage.get("extra_usage", {})
    assert extra.get("thoughts_tokens") == 256
    assert extra.get("tool_use_prompt_tokens") == 12


# ── 非零保护 ───────────────────────────────────────────────


def test_gemini_zero_values_do_not_override_previous():
    """后续帧携带的 0 值不应覆盖已提取的非零值（与 Anthropic 分支一致的语义）."""
    usage: dict = {}
    # 第一帧: 含非零
    parse_usage_from_chunk(
        _sse('{"usageMetadata":{"promptTokenCount":100,"candidatesTokenCount":40}}'),
        usage,
    )
    # 第二帧: 0 值应被忽略
    parse_usage_from_chunk(
        _sse('{"usageMetadata":{"promptTokenCount":0,"candidatesTokenCount":0}}'),
        usage,
    )
    assert usage["input_tokens"] == 100
    assert usage["output_tokens"] == 40


# ── Evidence 记录 ──────────────────────────────────────────


def test_gemini_usage_evidence_records_built():
    """parse → build_usage_evidence_records 串联应产出 gemini_usage_metadata 证据行."""
    usage: dict = {}
    parse_usage_from_chunk(
        _sse(
            '{"usageMetadata":{"promptTokenCount":100,"candidatesTokenCount":30,'
            '"cachedContentTokenCount":50},"responseId":"resp_xyz",'
            '"modelVersion":"gemini-2.0-flash"}'
        ),
        usage,
    )

    records = build_usage_evidence_records(
        usage,
        vendor="gemini",
        model_served="gemini-2.0-flash",
        request_id="resp_xyz",
    )
    assert len(records) >= 1
    gemini_record = next(
        (r for r in records if r["evidence_kind"] == "gemini_usage_metadata"),
        None,
    )
    assert gemini_record is not None
    assert gemini_record["vendor"] == "gemini"
    assert gemini_record["model_served"] == "gemini-2.0-flash"
    assert gemini_record["parsed_input_tokens"] == 100
    assert gemini_record["parsed_output_tokens"] == 30
    assert gemini_record["parsed_cache_read_tokens"] == 50
    assert gemini_record["cache_signal_present"] is True
    # source_field_map 指向 Gemini 原始字段名
    import json

    field_map = json.loads(gemini_record["source_field_map_json"])
    assert field_map["input_tokens"] == "promptTokenCount"
    assert field_map["output_tokens"] == "candidatesTokenCount"
    assert field_map["cache_read_tokens"] == "cachedContentTokenCount"


# ── 与既有分支正交性 ───────────────────────────────────────


def test_gemini_branch_does_not_break_anthropic_parsing():
    """同一 usage dict 先喂 Anthropic 再喂 Gemini, 两边都不应互相污染."""
    usage: dict = {}
    parse_usage_from_chunk(
        _sse(
            '{"type":"message_start","message":{"id":"msg_1","model":"claude-sonnet-4",'
            '"usage":{"input_tokens":50}}}'
        ),
        usage,
    )
    parse_usage_from_chunk(
        _sse('{"usageMetadata":{"promptTokenCount":200,"candidatesTokenCount":80}}'),
        usage,
    )
    # Gemini 的 prompt/cand 会覆盖前面的 input_tokens / output_tokens
    # （因为两者分别对应相同的归一化键；这是正常的，
    # 实际使用场景中单条 SSE 流不会同时包含两种协议）
    assert usage["input_tokens"] == 200
    assert usage["output_tokens"] == 80
    # request_id 应采用最先写入的（Anthropic message.id），不被 Gemini 覆盖
    assert usage["request_id"] == "msg_1"


def test_non_gemini_chunk_does_not_emit_gemini_evidence():
    """纯 OpenAI / Anthropic chunk 不应产生 gemini_usage_metadata 证据."""
    usage: dict = {}
    parse_usage_from_chunk(
        _sse(
            '{"id":"chatcmpl-1","usage":{"prompt_tokens":100,"completion_tokens":40}}'
        ),
        usage,
    )
    records = build_usage_evidence_records(
        usage, vendor="openai", model_served="gpt-4o", request_id="req"
    )
    assert all(r["evidence_kind"] != "gemini_usage_metadata" for r in records)


# ── 降级与健壮性 ───────────────────────────────────────────


def test_gemini_non_dict_usage_metadata_ignored():
    """usageMetadata 非 dict 时应静默忽略，不抛异常."""
    usage: dict = {}
    parse_usage_from_chunk(
        _sse('{"usageMetadata":"malformed"}'),
        usage,
    )
    assert usage == {}


def test_gemini_partial_fields_ok():
    """只有 promptTokenCount 一个字段也应正常提取."""
    usage: dict = {}
    parse_usage_from_chunk(
        _sse('{"usageMetadata":{"promptTokenCount":77}}'),
        usage,
    )
    assert usage["input_tokens"] == 77
    assert "output_tokens" not in usage
