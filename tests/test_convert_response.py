"""Gemini → Anthropic 响应格式转换单元测试."""

from __future__ import annotations

from coding.proxy.convert.gemini_to_anthropic import (
    convert_response,
    extract_usage,
)

# --- convert_response ---


def test_simple_text_response():
    """简单文本响应转换."""
    gemini = {
        "candidates": [
            {
                "content": {
                    "parts": [{"text": "Hello, world!"}],
                    "role": "model",
                },
                "finishReason": "STOP",
            }
        ],
        "usageMetadata": {
            "promptTokenCount": 10,
            "candidatesTokenCount": 5,
        },
    }
    result = convert_response(gemini, model="claude-sonnet-4-20250514")
    assert result["type"] == "message"
    assert result["role"] == "assistant"
    assert result["model"] == "claude-sonnet-4-20250514"
    assert result["stop_reason"] == "end_turn"
    assert len(result["content"]) == 1
    assert result["content"][0] == {"type": "text", "text": "Hello, world!"}
    assert result["usage"]["input_tokens"] == 10
    assert result["usage"]["output_tokens"] == 5


def test_max_tokens_finish_reason():
    """MAX_TOKENS finishReason 映射."""
    gemini = {
        "candidates": [
            {
                "content": {"parts": [{"text": "partial"}], "role": "model"},
                "finishReason": "MAX_TOKENS",
            }
        ],
        "usageMetadata": {"promptTokenCount": 5, "candidatesTokenCount": 100},
    }
    result = convert_response(gemini)
    assert result["stop_reason"] == "max_tokens"


def test_safety_finish_reason():
    """SAFETY finishReason → end_turn."""
    gemini = {
        "candidates": [
            {
                "content": {"parts": [{"text": ""}], "role": "model"},
                "finishReason": "SAFETY",
            }
        ],
        "usageMetadata": {},
    }
    result = convert_response(gemini)
    assert result["stop_reason"] == "end_turn"


def test_unknown_finish_reason():
    """未知 finishReason 默认 end_turn."""
    gemini = {
        "candidates": [
            {
                "content": {"parts": [{"text": "ok"}], "role": "model"},
                "finishReason": "UNKNOWN_NEW_VALUE",
            }
        ],
        "usageMetadata": {},
    }
    result = convert_response(gemini)
    assert result["stop_reason"] == "end_turn"


def test_function_call_response():
    """functionCall 响应 → tool_use 内容块."""
    gemini = {
        "candidates": [
            {
                "content": {
                    "parts": [
                        {
                            "functionCall": {
                                "name": "get_weather",
                                "args": {"city": "Paris"},
                            }
                        }
                    ],
                    "role": "model",
                },
                "finishReason": "STOP",
            }
        ],
        "usageMetadata": {"promptTokenCount": 20, "candidatesTokenCount": 10},
    }
    result = convert_response(gemini)
    assert len(result["content"]) == 1
    block = result["content"][0]
    assert block["type"] == "tool_use"
    assert block["name"] == "get_weather"
    assert block["input"] == {"city": "Paris"}
    assert block["id"].startswith("toolu_")


def test_multi_part_response():
    """多 parts 响应."""
    gemini = {
        "candidates": [
            {
                "content": {
                    "parts": [
                        {"text": "Here's the result: "},
                        {"text": "42"},
                    ],
                    "role": "model",
                },
                "finishReason": "STOP",
            }
        ],
        "usageMetadata": {},
    }
    result = convert_response(gemini)
    assert len(result["content"]) == 2
    assert result["content"][0]["text"] == "Here's the result: "
    assert result["content"][1]["text"] == "42"


def test_empty_candidates():
    """空 candidates → 空 content."""
    gemini = {"candidates": [], "usageMetadata": {}}
    result = convert_response(gemini)
    assert result["content"] == []
    assert result["stop_reason"] == "end_turn"


def test_custom_request_id():
    """自定义 request_id."""
    gemini = {
        "candidates": [
            {
                "content": {"parts": [{"text": "hi"}], "role": "model"},
                "finishReason": "STOP",
            }
        ],
        "usageMetadata": {},
    }
    result = convert_response(gemini, request_id="msg_custom_123")
    assert result["id"] == "msg_custom_123"


def test_auto_generated_id():
    """自动生成 msg_id."""
    gemini = {
        "candidates": [
            {
                "content": {"parts": [{"text": "hi"}], "role": "model"},
                "finishReason": "STOP",
            }
        ],
        "usageMetadata": {},
    }
    result = convert_response(gemini)
    assert result["id"].startswith("msg_")


# --- extract_usage ---


def test_extract_usage_full():
    """完整 usageMetadata 提取."""
    gemini = {
        "usageMetadata": {
            "promptTokenCount": 100,
            "candidatesTokenCount": 50,
        },
    }
    usage = extract_usage(gemini)
    assert usage == {
        "input_tokens": 100,
        "output_tokens": 50,
        "cache_creation_input_tokens": 0,
        "cache_read_input_tokens": 0,
    }


def test_extract_usage_empty():
    """空 usageMetadata."""
    usage = extract_usage({})
    assert usage["input_tokens"] == 0
    assert usage["output_tokens"] == 0


def test_extract_usage_partial():
    """部分 usageMetadata."""
    gemini = {"usageMetadata": {"promptTokenCount": 42}}
    usage = extract_usage(gemini)
    assert usage["input_tokens"] == 42
    assert usage["output_tokens"] == 0


# ── 新增测试用例 ──────────────────────────────────────


def test_thinking_response_with_signature():
    """thoughtSignature 正确映射为 signature 字段."""
    gemini = {
        "candidates": [
            {
                "content": {
                    "parts": [
                        {
                            "text": "Let me think",
                            "thought": True,
                            "thoughtSignature": "sig_abc",
                        }
                    ],
                    "role": "model",
                },
                "finishReason": "STOP",
            }
        ],
        "usageMetadata": {},
    }
    result = convert_response(gemini)
    block = result["content"][0]
    assert block["type"] == "thinking"
    assert block["signature"] == "sig_abc"


def test_function_call_with_id_preserved():
    """functionCall.id 被保留到 tool_use.id."""
    gemini = {
        "candidates": [
            {
                "content": {
                    "parts": [
                        {
                            "functionCall": {
                                "id": "fc_custom_1",
                                "name": "search",
                                "args": {"q": "test"},
                            }
                        }
                    ],
                    "role": "model",
                },
                "finishReason": "STOP",
            }
        ],
        "usageMetadata": {},
    }
    result = convert_response(gemini)
    assert result["content"][0]["id"] == "fc_custom_1"


def test_text_part_with_signature_only():
    """text 为空但有 signature 时生成 thinking 块."""
    gemini = {
        "candidates": [
            {
                "content": {
                    "parts": [{"text": "", "thoughtSignature": "sig_only"}],
                    "role": "model",
                },
                "finishReason": "STOP",
            }
        ],
        "usageMetadata": {},
    }
    result = convert_response(gemini)
    block = result["content"][0]
    assert block["type"] == "thinking"
    assert block["signature"] == "sig_only"


def test_mixed_text_and_function_call():
    """文本 + functionCall 混合响应生成两个 content blocks."""
    gemini = {
        "candidates": [
            {
                "content": {
                    "parts": [
                        {"text": "I'll search for you."},
                        {"functionCall": {"name": "search", "args": {"q": "gemini"}}},
                    ],
                    "role": "model",
                },
                "finishReason": "STOP",
            }
        ],
        "usageMetadata": {},
    }
    result = convert_response(gemini)
    assert len(result["content"]) == 2
    assert result["content"][0]["type"] == "text"
    assert result["content"][1]["type"] == "tool_use"
    # stop_reason 应为 tool_use（因为有 tool_use block）
    assert result["stop_reason"] == "tool_use"


def test_usage_cache_tokens_zero():
    """Gemini 不报告 cache tokens，保持为 0."""
    gemini = {
        "usageMetadata": {
            "promptTokenCount": 100,
            "candidatesTokenCount": 50,
        },
    }
    usage = extract_usage(gemini)
    assert usage["cache_creation_input_tokens"] == 0
    assert usage["cache_read_input_tokens"] == 0
