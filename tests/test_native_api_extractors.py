"""三家 provider 原生 API usage 抽取器 + 兜底扫描单测.

覆盖：

- OpenAI: chat (含 reasoning/cached/audio) / responses / embedding / audio STT /
  image / moderation；
- Gemini: generateContent (含 thoughts/cached) / countTokens / cachedContents；
- Anthropic: messages (含 5m/1h cache / server_tool_use) / count_tokens / batches；
- 兜底扫描：未注册 operation 自动走 ``_scan_usage_like``，规范字段 + 非规范 int 入
  ``extra_usage``。
"""

from __future__ import annotations

from coding.proxy.native_api import (
    ExtractionResult,
    extract_usage,
)

# ── OpenAI chat completions ───────────────────────────────────────────


def test_openai_chat_full_usage() -> None:
    body = {
        "id": "chatcmpl-xxx",
        "model": "gpt-4o-2024-08-06",
        "usage": {
            "prompt_tokens": 100,
            "completion_tokens": 200,
            "total_tokens": 300,
            "prompt_tokens_details": {
                "cached_tokens": 40,
                "audio_tokens": 10,
            },
            "completion_tokens_details": {
                "reasoning_tokens": 50,
                "audio_tokens": 20,
                "accepted_prediction_tokens": 5,
                "rejected_prediction_tokens": 3,
            },
        },
    }
    r = extract_usage("openai", "chat", body, 200)
    assert r.input_tokens == 100
    assert r.output_tokens == 200
    assert r.cache_read_tokens == 40
    assert r.extra_usage["audio_input_tokens"] == 10
    assert r.extra_usage["reasoning_tokens"] == 50
    assert r.extra_usage["audio_output_tokens"] == 20
    assert r.extra_usage["accepted_prediction_tokens"] == 5
    assert r.extra_usage["rejected_prediction_tokens"] == 3
    assert r.extra_usage["total_tokens"] == 300
    assert r.request_id == "chatcmpl-xxx"
    assert r.model_served == "gpt-4o-2024-08-06"
    assert r.evidence_kind == "native_openai_usage"
    assert r.raw_usage == body["usage"]


def test_openai_chat_minimal_usage() -> None:
    body = {
        "id": "chatcmpl-abc",
        "model": "gpt-4o-mini",
        "usage": {"prompt_tokens": 5, "completion_tokens": 10},
    }
    r = extract_usage("openai", "chat", body, 200)
    assert r.input_tokens == 5
    assert r.output_tokens == 10
    assert r.cache_read_tokens == 0
    assert "reasoning_tokens" not in r.extra_usage


def test_openai_chat_no_usage_block() -> None:
    body = {"id": "x", "model": "gpt-4o"}
    r = extract_usage("openai", "chat", body, 200)
    assert r.input_tokens == 0
    assert r.output_tokens == 0
    assert r.request_id == "x"
    assert r.model_served == "gpt-4o"


# ── OpenAI responses API ──────────────────────────────────────────────


def test_openai_responses_reasoning() -> None:
    body = {
        "id": "resp_abc",
        "model": "o1-preview",
        "usage": {
            "input_tokens": 50,
            "output_tokens": 200,
            "total_tokens": 250,
            "input_tokens_details": {"cached_tokens": 20},
            "output_tokens_details": {"reasoning_tokens": 150},
        },
    }
    r = extract_usage("openai", "responses", body, 200)
    assert r.input_tokens == 50
    assert r.output_tokens == 200
    assert r.cache_read_tokens == 20
    assert r.extra_usage["reasoning_tokens"] == 150
    assert r.extra_usage["total_tokens"] == 250
    assert r.evidence_kind == "native_openai_responses_usage"


# ── OpenAI embeddings ─────────────────────────────────────────────────


def test_openai_embedding() -> None:
    body = {
        "model": "text-embedding-3-small",
        "usage": {"prompt_tokens": 42, "total_tokens": 42},
    }
    r = extract_usage("openai", "embedding", body, 200)
    assert r.input_tokens == 42
    assert r.output_tokens == 0
    assert r.extra_usage["total_tokens"] == 42


# ── OpenAI audio STT ──────────────────────────────────────────────────


def test_openai_audio_transcription_verbose_json() -> None:
    body = {
        "text": "...",
        "duration": 12.5,
        "usage": {"input_tokens": 30, "output_tokens": 50},
    }
    r = extract_usage("openai", "audio.transcription", body, 200)
    assert r.input_tokens == 30
    assert r.output_tokens == 50
    assert r.extra_usage["audio_duration_seconds"] == 12.5


def test_openai_audio_transcription_no_usage() -> None:
    body = {"text": "hello world"}
    r = extract_usage("openai", "audio.transcription", body, 200)
    assert r.input_tokens == 0
    assert r.output_tokens == 0


# ── OpenAI image ──────────────────────────────────────────────────────


def test_openai_image_generation() -> None:
    body = {
        "created": 1,
        "data": [{"b64_json": "..."}, {"b64_json": "..."}],
        "usage": {"input_tokens": 10, "output_tokens": 0},
    }
    r = extract_usage("openai", "image.generation", body, 200)
    assert r.input_tokens == 10
    assert r.extra_usage["generated_images"] == 2


# ── OpenAI moderation ─────────────────────────────────────────────────


def test_openai_moderation() -> None:
    body = {"id": "modr-x", "model": "omni-moderation-latest", "results": [{}, {}]}
    r = extract_usage("openai", "moderation", body, 200)
    assert r.input_tokens == 0
    assert r.extra_usage["moderation_results_count"] == 2


# ── Gemini generateContent ────────────────────────────────────────────


def test_gemini_generate_content_full() -> None:
    body = {
        "responseId": "resp-xyz",
        "modelVersion": "gemini-2.0-flash-001",
        "candidates": [{"content": {"parts": [{"text": "hi"}]}}],
        "usageMetadata": {
            "promptTokenCount": 10,
            "candidatesTokenCount": 20,
            "totalTokenCount": 30,
            "cachedContentTokenCount": 5,
            "thoughtsTokenCount": 100,
            "toolUsePromptTokenCount": 8,
        },
    }
    r = extract_usage("gemini", "generate_content", body, 200)
    assert r.input_tokens == 10
    assert r.output_tokens == 20
    assert r.cache_read_tokens == 5
    assert r.extra_usage["thoughts_tokens"] == 100
    assert r.extra_usage["tool_use_prompt_tokens"] == 8
    assert r.extra_usage["total_token_count"] == 30
    assert r.request_id == "resp-xyz"
    assert r.model_served == "gemini-2.0-flash-001"
    assert r.evidence_kind == "native_gemini_usage_metadata"


def test_gemini_count_tokens_only_total() -> None:
    body = {"totalTokens": 77}
    r = extract_usage("gemini", "count_tokens", body, 200)
    assert r.input_tokens == 77
    assert r.raw_usage == {"totalTokens": 77}


def test_gemini_count_tokens_new_api_with_usage_metadata() -> None:
    """新版 Gemini count_tokens 同时带 usageMetadata — 应覆盖 totalTokens 骨架."""
    body = {
        "totalTokens": 50,
        "usageMetadata": {"promptTokenCount": 50, "totalTokenCount": 50},
    }
    r = extract_usage("gemini", "count_tokens", body, 200)
    assert r.input_tokens == 50
    assert r.extra_usage.get("total_token_count") == 50


def test_gemini_cache_crud() -> None:
    body = {
        "name": "cachedContents/abc",
        "displayName": "my-cache",
        "expireTime": "2025-01-01T00:00:00Z",
        "ttl": "3600s",
    }
    r = extract_usage("gemini", "cache", body, 200)
    assert r.input_tokens == 0
    assert r.extra_usage["expireTime"] == "2025-01-01T00:00:00Z"
    assert r.extra_usage["displayName"] == "my-cache"


# ── Anthropic messages ────────────────────────────────────────────────


def test_anthropic_messages_full() -> None:
    body = {
        "id": "msg_01xyz",
        "model": "claude-opus-4-7",
        "type": "message",
        "usage": {
            "input_tokens": 100,
            "output_tokens": 200,
            "cache_creation_input_tokens": 50,
            "cache_read_input_tokens": 30,
            "cache_creation": {
                "ephemeral_5m_input_tokens": 40,
                "ephemeral_1h_input_tokens": 10,
            },
            "server_tool_use": {"web_search_requests": 2},
        },
    }
    r = extract_usage("anthropic", "messages", body, 200)
    assert r.input_tokens == 100
    assert r.output_tokens == 200
    assert r.cache_creation_tokens == 50
    assert r.cache_read_tokens == 30
    assert r.extra_usage["cache_5m_tokens"] == 40
    assert r.extra_usage["cache_1h_tokens"] == 10
    assert r.extra_usage["server_tool_use_web_search_requests"] == 2
    assert r.request_id == "msg_01xyz"
    assert r.model_served == "claude-opus-4-7"
    assert r.evidence_kind == "native_anthropic_usage"


def test_anthropic_count_tokens() -> None:
    body = {"input_tokens": 123}
    r = extract_usage("anthropic", "count_tokens", body, 200)
    assert r.input_tokens == 123
    assert r.output_tokens == 0
    assert r.raw_usage == {"input_tokens": 123}


def test_anthropic_messages_batch_no_token_extraction() -> None:
    body = {
        "id": "msgbatch_01",
        "type": "message_batch",
        "processing_status": "in_progress",
        "request_counts": {"processing": 2, "succeeded": 0, "errored": 0},
    }
    r = extract_usage("anthropic", "messages.batch", body, 200)
    assert r.input_tokens == 0
    assert r.output_tokens == 0
    assert r.extra_usage["batch_id"] == "msgbatch_01"
    assert r.extra_usage["batch_processing_status"] == "in_progress"


# ── 通用兜底扫描 ──────────────────────────────────────────────────────


def test_fallback_scan_openai_unknown_op() -> None:
    """未注册 operation 走兜底扫描 — 匹配 top-level usage."""
    body = {
        "id": "future-thing",
        "model": "new-model",
        "usage": {
            "prompt_tokens": 7,
            "completion_tokens": 9,
            "exotic_tokens": 42,  # 非规范字段 → extra_usage
        },
    }
    r = extract_usage("openai", "unknown_future_op", body, 200)
    assert r.input_tokens == 7
    assert r.output_tokens == 9
    assert r.extra_usage["exotic_tokens"] == 42
    assert r.evidence_kind == "native_generic_scan"


def test_fallback_scan_gemini_usage_metadata_block() -> None:
    body = {
        "responseId": "r-1",
        "usageMetadata": {"promptTokenCount": 3, "candidatesTokenCount": 4},
    }
    r = extract_usage("gemini", "some_future_op", body, 200)
    assert r.input_tokens == 3
    assert r.output_tokens == 4
    assert r.request_id == "r-1"
    assert r.evidence_kind == "native_generic_scan"


def test_fallback_scan_no_usage_key() -> None:
    body = {"hello": "world"}
    r = extract_usage("openai", "unknown_op", body, 200)
    assert r.input_tokens == 0
    assert r.output_tokens == 0
    assert r.raw_usage == {}


def test_extract_usage_on_non_dict_returns_empty() -> None:
    r = extract_usage("openai", "chat", None, 200)  # type: ignore[arg-type]
    assert isinstance(r, ExtractionResult)
    assert r.input_tokens == 0


def test_extract_usage_unknown_provider_fallback_scan() -> None:
    body = {"usage": {"prompt_tokens": 11}}
    r = extract_usage("cohere", "anything", body, 200)
    assert r.input_tokens == 11
    assert r.evidence_kind == "native_generic_scan"
