"""OpenAI (Copilot) -> Anthropic response format conversion tests."""

from coding.proxy.convert.openai_to_anthropic import convert_response


def test_response_with_reasoning_content_creates_thinking_block():
    openai_resp = {
        "id": "chatcmpl_1",
        "model": "claude-sonnet-4",
        "choices": [
            {
                "message": {
                    "role": "assistant",
                    "reasoning_content": "Let me think step by step...",
                    "content": "The answer is 42.",
                },
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 10, "completion_tokens": 20},
    }
    result = convert_response(openai_resp)
    assert result["content"][0]["type"] == "thinking"
    assert result["content"][0]["thinking"] == "Let me think step by step..."
    assert result["content"][1]["type"] == "text"
    assert result["content"][1]["text"] == "The answer is 42."


def test_response_with_empty_reasoning_content_ignored():
    """Empty reasoning_content should not produce a thinking block."""
    openai_resp = {
        "id": "chatcmpl_2",
        "model": "claude-sonnet-4",
        "choices": [
            {
                "message": {
                    "role": "assistant",
                    "reasoning_content": "   ",
                    "content": "Hello",
                },
                "finish_reason": "stop",
            }
        ],
        "usage": {},
    }
    result = convert_response(openai_resp)
    text_blocks = [b for b in result["content"] if b["type"] == "text"]
    thinking_blocks = [b for b in result["content"] if b["type"] == "thinking"]
    assert len(thinking_blocks) == 0
    assert len(text_blocks) == 1


def test_unknown_finish_reason_defaults_to_end_turn():
    openai_resp = {
        "choices": [
            {
                "message": {"role": "assistant", "content": "ok"},
                "finish_reason": "some_new_reason",
            }
        ],
        "usage": {},
    }
    result = convert_response(openai_resp)
    assert result["stop_reason"] == "end_turn"


def test_stop_finish_reason_maps_to_end_turn():
    openai_resp = {
        "choices": [
            {
                "message": {"role": "assistant", "content": "done"},
                "finish_reason": "stop",
            }
        ],
        "usage": {},
    }
    result = convert_response(openai_resp)
    assert result["stop_reason"] == "end_turn"


def test_length_finish_reason_maps_to_max_tokens():
    openai_resp = {
        "choices": [
            {
                "message": {"role": "assistant", "content": "partial..."},
                "finish_reason": "length",
            }
        ],
        "usage": {},
    }
    result = convert_response(openai_resp)
    assert result["stop_reason"] == "max_tokens"


def test_tool_calls_in_response():
    openai_resp = {
        "id": "chatcmpl_2",
        "model": "claude-sonnet-4",
        "choices": [
            {
                "message": {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "call_123",
                            "type": "function",
                            "function": {
                                "name": "get_weather",
                                "arguments": '{"city":"Tokyo"}',
                            },
                        }
                    ],
                },
                "finish_reason": "tool_calls",
            }
        ],
        "usage": {"prompt_tokens": 15, "completion_tokens": 30},
    }
    result = convert_response(openai_resp)
    assert result["content"][0]["type"] == "tool_use"
    assert result["content"][0]["name"] == "get_weather"
    assert result["content"][0]["input"] == {"city": "Tokyo"}
    assert result["stop_reason"] == "tool_use"


def test_tool_calls_with_invalid_json_arguments():
    """Tool call with invalid JSON arguments falls back to empty dict."""
    openai_resp = {
        "choices": [
            {
                "message": {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "call_bad",
                            "type": "function",
                            "function": {"name": "bad_tool", "arguments": "not-json"},
                        }
                    ],
                },
                "finish_reason": "tool_calls",
            }
        ],
        "usage": {},
    }
    result = convert_response(openai_resp)
    assert result["content"][0]["input"] == {}


def test_multiple_tool_calls_preserved():
    openai_resp = {
        "choices": [
            {
                "message": {
                    "role": "assistant",
                    "content": "I will check both.",
                    "tool_calls": [
                        {
                            "id": "call_1",
                            "type": "function",
                            "function": {
                                "name": "get_weather",
                                "arguments": '{"city":"Tokyo"}',
                            },
                        },
                        {
                            "id": "call_2",
                            "type": "function",
                            "function": {
                                "name": "get_time",
                                "arguments": '{"tz":"UTC"}',
                            },
                        },
                    ],
                },
                "finish_reason": "tool_calls",
            }
        ],
        "usage": {},
    }
    result = convert_response(openai_resp)
    tool_blocks = [b for b in result["content"] if b["type"] == "tool_use"]
    assert len(tool_blocks) == 2
    assert tool_blocks[0]["name"] == "get_weather"
    assert tool_blocks[1]["name"] == "get_time"


def test_usage_with_cached_tokens():
    openai_resp = {
        "choices": [
            {"message": {"role": "assistant", "content": "hi"}, "finish_reason": "stop"}
        ],
        "usage": {
            "prompt_tokens": 100,
            "completion_tokens": 20,
            "prompt_tokens_details": {"cached_tokens": 30},
        },
    }
    result = convert_response(openai_resp)
    assert result["usage"]["input_tokens"] == 70
    assert result["usage"]["output_tokens"] == 20
    assert result["usage"]["cache_read_input_tokens"] == 30


def test_request_id_from_openai_id():
    openai_resp = {
        "id": "chatcmpl_custom",
        "model": "claude-opus-4",
        "choices": [
            {"message": {"role": "assistant", "content": "ok"}, "finish_reason": "stop"}
        ],
        "usage": {},
    }
    result = convert_response(openai_resp)
    assert result["id"] == "chatcmpl_custom"


def test_content_filter_finish_reason_maps_to_end_turn():
    openai_resp = {
        "choices": [
            {
                "message": {"role": "assistant", "content": ""},
                "finish_reason": "content_filter",
            }
        ],
        "usage": {},
    }
    result = convert_response(openai_resp)
    assert result["stop_reason"] == "end_turn"


def test_null_finish_reason_in_choice_defaults():
    """Choice without finish_reason should still parse correctly."""
    openai_resp = {
        "choices": [{"message": {"role": "assistant", "content": "ok"}}],
        "usage": {},
    }
    result = convert_response(openai_resp)
    assert result["stop_reason"] == "end_turn"


def test_list_content_in_message():
    """OpenAI response with list content (multimodal) extracts text correctly."""
    openai_resp = {
        "choices": [
            {
                "message": {
                    "role": "assistant",
                    "content": [
                        {"type": "text", "text": "Part 1"},
                        {"type": "text", "text": "Part 2"},
                    ],
                },
                "finish_reason": "stop",
            }
        ],
        "usage": {},
    }
    result = convert_response(openai_resp)
    text_blocks = [b for b in result["content"] if b["type"] == "text"]
    assert len(text_blocks) == 2
    assert text_blocks[0]["text"] == "Part 1"
    assert text_blocks[1]["text"] == "Part 2"
