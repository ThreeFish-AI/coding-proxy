"""Anthropic → OpenAI (Copilot) 请求格式转换单元测试."""

from coding.proxy.convert.anthropic_to_openai import convert_request

# === Thinking / Extended Thinking 映射 ===


def test_extended_thinking_medium_maps_to_reasoning_effort():
    body = {
        "model": "claude-sonnet-4-20250514",
        "messages": [{"role": "user", "content": "Hi"}],
        "extended_thinking": {"budget_tokens": 1024, "effort": "medium"},
    }
    result = convert_request(body)
    assert result["reasoning_effort"] == "medium"


def test_extended_thinking_high_maps_to_reasoning_effort():
    body = {
        "model": "claude-sonnet-4-20250514",
        "messages": [{"role": "user", "content": "Hi"}],
        "extended_thinking": {"effort": "high"},
    }
    result = convert_request(body)
    assert result["reasoning_effort"] == "high"


def test_extended_thinking_low_maps_to_reasoning_effort():
    body = {
        "model": "claude-sonnet-4-20250514",
        "messages": [{"role": "user", "content": "Hi"}],
        "extended_thinking": {"effort": "low"},
    }
    result = convert_request(body)
    assert result["reasoning_effort"] == "low"


def test_extended_thinking_without_effort_produces_no_result():
    """extended_thinking 无 effort 字段时不输出 reasoning_effort."""
    body = {
        "model": "claude-sonnet-4-20250514",
        "messages": [{"role": "user", "content": "Hi"}],
        "extended_thinking": {"budget_tokens": 500},
    }
    result = convert_request(body)
    assert "reasoning_effort" not in result


def test_thinking_bool_maps_to_reasoning_effort_medium():
    body = {
        "model": "claude-sonnet-4-20250514",
        "messages": [{"role": "user", "content": "Hi"}],
        "thinking": True,
    }
    result = convert_request(body)
    assert result["reasoning_effort"] == "medium"


def test_thinking_dict_enabled_maps_to_reasoning_effort_medium():
    body = {
        "model": "claude-sonnet-4-20250514",
        "messages": [{"role": "user", "content": "Hi"}],
        "thinking": {"type": "enabled"},
    }
    result = convert_request(body)
    assert result["reasoning_effort"] == "medium"


def test_no_thinking_produces_no_reasoning_effort():
    body = {
        "model": "claude-sonnet-4-20250514",
        "messages": [{"role": "user", "content": "Hi"}],
    }
    result = convert_request(body)
    assert "reasoning_effort" not in result


# === System Prompt 结构化 ===


def test_system_with_cache_control_still_extracts_text():
    body = {
        "model": "claude-sonnet-4-20250514",
        "system": [
            {
                "type": "text",
                "text": "You are helpful.",
                "cache_control": {"type": "ephemeral"},
            },
            {"type": "text", "text": "Be concise."},
        ],
        "messages": [{"role": "user", "content": "Hi"}],
    }
    result = convert_request(body)
    system_msgs = [m for m in result["messages"] if m["role"] == "system"]
    assert len(system_msgs) == 1
    assert "You are helpful." in system_msgs[0]["content"]
    assert "Be concise." in system_msgs[0]["content"]


def test_system_string_passthrough():
    body = {
        "model": "claude-sonnet-4-20250514",
        "system": "Simple string system prompt",
        "messages": [{"role": "user", "content": "Hi"}],
    }
    result = convert_request(body)
    system_msgs = [m for m in result["messages"] if m["role"] == "system"]
    assert system_msgs[0]["content"] == "Simple string system prompt"


def test_system_empty_list_produces_no_system_message():
    body = {
        "model": "claude-sonnet-4-20250514",
        "system": [],
        "messages": [{"role": "user", "content": "Hi"}],
    }
    result = convert_request(body)
    system_msgs = [m for m in result["messages"] if m["role"] == "system"]
    assert len(system_msgs) == 0


# === Assistant Thinking Block 分离 ===


def test_assistant_thinking_block_separated_from_text():
    body = {
        "model": "claude-sonnet-4-20250514",
        "messages": [
            {
                "role": "assistant",
                "content": [
                    {"type": "thinking", "thinking": "Let me analyze..."},
                    {"type": "text", "text": "The answer is 42."},
                ],
            }
        ],
    }
    result = convert_request(body)
    assistant_msgs = [m for m in result["messages"] if m["role"] == "assistant"]
    assert len(assistant_msgs) == 1
    content = assistant_msgs[0]["content"]
    assert "[Thinking]" in content
    assert "Let me analyze..." in content
    assert "The answer is 42." in content


def test_assistant_only_thinking_becomes_content():
    body = {
        "model": "claude-sonnet-4-20250514",
        "messages": [
            {
                "role": "assistant",
                "content": [
                    {"type": "thinking", "thinking": "Just thinking..."},
                ],
            }
        ],
    }
    result = convert_request(body)
    assistant_msgs = [m for m in result["messages"] if m["role"] == "assistant"]
    assert assistant_msgs[0]["content"] == "Just thinking..."


def test_assistant_thinking_with_tool_uses_drops_thinking():
    """有 tool_use 时 thinking 内容被丢弃（工具调用场景不需要历史思考过程）."""
    body = {
        "model": "claude-sonnet-4-20250514",
        "messages": [
            {
                "role": "assistant",
                "content": [
                    {"type": "thinking", "thinking": "I should call a tool."},
                    {
                        "type": "tool_use",
                        "id": "toolu_123",
                        "name": "get_weather",
                        "input": {"city": "Tokyo"},
                    },
                ],
            }
        ],
    }
    result = convert_request(body)
    assistant_msgs = [m for m in result["messages"] if m["role"] == "assistant"]
    assert len(assistant_msgs) == 1
    assert "tool_calls" in assistant_msgs[0]
    # thinking 不应出现在 content 中
    assert assistant_msgs[0]["content"] is None or "I should call a tool" not in (
        assistant_msgs[0]["content"] or ""
    )


# === Tool Result is_error ===


def test_tool_result_is_error_injected_into_content():
    body = {
        "model": "claude-sonnet-4-20250514",
        "messages": [
            {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "toolu_123",
                        "content": "API rate limited",
                        "is_error": True,
                    }
                ],
            }
        ],
    }
    result = convert_request(body)
    tool_msgs = [m for m in result["messages"] if m["role"] == "tool"]
    assert len(tool_msgs) == 1
    assert tool_msgs[0]["tool_call_id"] == "toolu_123"
    assert "[ERROR]" in tool_msgs[0]["content"]
    assert "API rate limited" in tool_msgs[0]["content"]


def test_tool_result_non_error_not_injected():
    body = {
        "model": "claude-sonnet-4-20250514",
        "messages": [
            {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "toolu_123",
                        "content": "Sunny, 25°C",
                    }
                ],
            }
        ],
    }
    result = convert_request(body)
    tool_msgs = [m for m in result["messages"] if m["role"] == "tool"]
    assert "[ERROR]" not in tool_msgs[0]["content"]
    assert tool_msgs[0]["content"] == "Sunny, 25°C"


# === Metadata 透传 ===


def test_metadata_full_forwarded():
    body = {
        "model": "claude-sonnet-4-20250514",
        "messages": [{"role": "user", "content": "Hi"}],
        "metadata": {"user_id": "u-1", "session_id": "sess_42"},
    }
    result = convert_request(body)
    assert result["user"] == "u-1"
    assert result["metadata"]["session_id"] == "sess_42"
    assert result["metadata"]["user_id"] == "u-1"


def test_metadata_user_id_only():
    body = {
        "model": "claude-sonnet-4-20250514",
        "messages": [{"role": "user", "content": "Hi"}],
        "metadata": {"user_id": "u-1"},
    }
    result = convert_request(body)
    assert result["user"] == "u-1"
    assert "metadata" not in result


# === 模型名映射精细化 ===


def test_model_name_copilot_format_passthrough():
    body = {
        "model": "claude-sonnet-4.6",
        "messages": [{"role": "user", "content": "Hi"}],
    }
    result = convert_request(body)
    assert result["model"] == "claude-sonnet-4.6"


def test_model_name_opus_46_passthrough():
    body = {
        "model": "claude-opus-4.6",
        "messages": [{"role": "user", "content": "Hi"}],
    }
    result = convert_request(body)
    assert result["model"] == "claude-opus-4.6"


def test_model_name_haiku_45_passthrough():
    body = {
        "model": "claude-haiku-4.5",
        "messages": [{"role": "user", "content": "Hi"}],
    }
    result = convert_request(body)
    assert result["model"] == "claude-haiku-4.5"


def test_model_name_with_minor_version_normalized():
    body = {
        "model": "claude-sonnet-4.6-20250514",
        "messages": [{"role": "user", "content": "Hi"}],
    }
    result = convert_request(body)
    assert result["model"] == "claude-sonnet-4.6"


def test_model_name_opus_with_minor_version_normalized():
    body = {
        "model": "claude-opus-4.6-20250514",
        "messages": [{"role": "user", "content": "Hi"}],
    }
    result = convert_request(body)
    assert result["model"] == "claude-opus-4.6"


def test_model_name_haiku_date_suffix_stripped():
    body = {
        "model": "claude-haiku-4-20250514",
        "messages": [{"role": "user", "content": "Hi"}],
    }
    result = convert_request(body)
    assert result["model"] == "claude-haiku-4"


def test_non_claude_model_passthrough():
    body = {
        "model": "gpt-5.2",
        "messages": [{"role": "user", "content": "Hi"}],
    }
    result = convert_request(body)
    assert result["model"] == "gpt-5.2"


# === 回归测试：基础功能不受影响 ===


def test_simple_text_message_still_works():
    result = convert_request(
        {
            "model": "claude-sonnet-4-20250514",
            "messages": [{"role": "user", "content": "Hello"}],
            "max_tokens": 100,
        }
    )
    assert result["model"] == "claude-sonnet-4"
    assert len(result["messages"]) == 1
    assert result["messages"][0]["role"] == "user"
    assert result["max_tokens"] == 100


def test_tool_use_conversion_still_works():
    result = convert_request(
        {
            "model": "claude-sonnet-4-20250514",
            "messages": [{"role": "user", "content": "Hi"}],
            "tools": [
                {
                    "name": "get_weather",
                    "description": "Get weather",
                    "input_schema": {"type": "object"},
                },
            ],
        }
    )
    assert "tools" in result
    assert result["tools"][0]["type"] == "function"
    assert result["tools"][0]["function"]["name"] == "get_weather"


def test_tool_choice_auto_still_works():
    result = convert_request(
        {
            "model": "claude-sonnet-4-20250514",
            "messages": [{"role": "user", "content": "Hi"}],
            "tool_choice": {"type": "auto"},
        }
    )
    assert result["tool_choice"] == "auto"


def test_tool_choice_specific_tool_still_works():
    result = convert_request(
        {
            "model": "claude-sonnet-4-20250514",
            "messages": [{"role": "user", "content": "Hi"}],
            "tool_choice": {"type": "tool", "name": "get_weather"},
        }
    )
    assert result["tool_choice"]["type"] == "function"
    assert result["tool_choice"]["function"]["name"] == "get_weather"


def test_stop_sequences_mapped_to_stop():
    result = convert_request(
        {
            "model": "claude-sonnet-4-20250514",
            "messages": [{"role": "user", "content": "Hi"}],
            "stop_sequences": ["END", "STOP"],
        }
    )
    assert result["stop"] == ["END", "STOP"]


def test_temperature_and_top_p_still_works():
    result = convert_request(
        {
            "model": "claude-sonnet-4-20250514",
            "messages": [{"role": "user", "content": "Hi"}],
            "temperature": 0.7,
            "top_p": 0.9,
        }
    )
    assert result["temperature"] == 0.7
    assert result["top_p"] == 0.9


def test_stream_options_included_when_streaming():
    result = convert_request(
        {
            "model": "claude-sonnet-4-20250514",
            "messages": [{"role": "user", "content": "Hi"}],
            "stream": True,
        }
    )
    assert result["stream"] is True
    assert result["stream_options"] == {"include_usage": True}


def test_multi_turn_conversation_still_works():
    result = convert_request(
        {
            "model": "claude-sonnet-4-20250514",
            "messages": [
                {"role": "user", "content": "What is 2+2?"},
                {"role": "assistant", "content": "4"},
                {"role": "user", "content": "And 3+3?"},
            ],
        }
    )
    assert len(result["messages"]) == 3
    assert result["messages"][0]["role"] == "user"
    assert result["messages"][1]["role"] == "assistant"
    assert result["messages"][2]["role"] == "user"


def test_image_block_converted_to_image_url():
    result = convert_request(
        {
            "model": "claude-sonnet-4-20250514",
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "What is this?"},
                        {
                            "type": "image",
                            "source": {
                                "type": "base64",
                                "media_type": "image/png",
                                "data": "abc123",
                            },
                        },
                    ],
                }
            ],
        }
    )
    user_msg = [m for m in result["messages"] if m["role"] == "user"][0]
    assert isinstance(user_msg["content"], list)
    image_part = [p for p in user_msg["content"] if p.get("type") == "image_url"]
    assert len(image_part) == 1
    assert "data:image/png;base64,abc123" in image_part[0]["image_url"]["url"]


# === Defensive tool_use.input serialization ===


def test_tool_use_input_none_defaults_to_empty_dict():
    """input=None 应被降级为 {} 而非序列化为 'null'."""
    body = {
        "model": "claude-sonnet-4-20250514",
        "messages": [
            {
                "role": "assistant",
                "content": [
                    {
                        "type": "tool_use",
                        "id": "toolu_001",
                        "name": "read_file",
                        "input": None,
                    }
                ],
            }
        ],
    }
    result = convert_request(body)
    assistant_msgs = [m for m in result["messages"] if m["role"] == "assistant"]
    assert len(assistant_msgs) == 1
    assert "tool_calls" in assistant_msgs[0]
    tc = assistant_msgs[0]["tool_calls"][0]
    assert tc["function"]["arguments"] == "{}"


def test_tool_use_input_string_defaults_to_empty_dict():
    """input='some string' 应被降级为 {} 而非序列化为 '"some string"'."""
    body = {
        "model": "claude-sonnet-4-20250514",
        "messages": [
            {
                "role": "assistant",
                "content": [
                    {
                        "type": "tool_use",
                        "id": "toolu_002",
                        "name": "run_cmd",
                        "input": "not a dict",
                    }
                ],
            }
        ],
    }
    result = convert_request(body)
    assistant_msgs = [m for m in result["messages"] if m["role"] == "assistant"]
    tc = assistant_msgs[0]["tool_calls"][0]
    assert tc["function"]["arguments"] == "{}"


def test_tool_use_input_missing_defaults_to_empty_dict():
    """input key 不存在时，block.get('input') 返回 None，应降级为 {}."""
    body = {
        "model": "claude-sonnet-4-20250514",
        "messages": [
            {
                "role": "assistant",
                "content": [
                    {
                        "type": "tool_use",
                        "id": "toolu_003",
                        "name": "search",
                    }
                ],
            }
        ],
    }
    result = convert_request(body)
    assistant_msgs = [m for m in result["messages"] if m["role"] == "assistant"]
    tc = assistant_msgs[0]["tool_calls"][0]
    assert tc["function"]["arguments"] == "{}"


def test_tool_use_input_int_defaults_to_empty_dict():
    """input=42 应被降级为 {} 而非序列化为 '42'."""
    body = {
        "model": "claude-sonnet-4-20250514",
        "messages": [
            {
                "role": "assistant",
                "content": [
                    {
                        "type": "tool_use",
                        "id": "toolu_004",
                        "name": "calc",
                        "input": 42,
                    }
                ],
            }
        ],
    }
    result = convert_request(body)
    assistant_msgs = [m for m in result["messages"] if m["role"] == "assistant"]
    tc = assistant_msgs[0]["tool_calls"][0]
    assert tc["function"]["arguments"] == "{}"


def test_tool_use_valid_dict_input_preserved():
    """正常 dict input 应保持原样."""
    body = {
        "model": "claude-sonnet-4-20250514",
        "messages": [
            {
                "role": "assistant",
                "content": [
                    {
                        "type": "tool_use",
                        "id": "toolu_005",
                        "name": "read_file",
                        "input": {"path": "/tmp/test.txt", "offset": 10},
                    }
                ],
            }
        ],
    }
    result = convert_request(body)
    assistant_msgs = [m for m in result["messages"] if m["role"] == "assistant"]
    tc = assistant_msgs[0]["tool_calls"][0]
    import json

    assert json.loads(tc["function"]["arguments"]) == {
        "path": "/tmp/test.txt",
        "offset": 10,
    }
