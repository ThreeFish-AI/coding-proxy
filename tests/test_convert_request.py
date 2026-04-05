"""Anthropic → Gemini 请求格式转换单元测试."""

from __future__ import annotations

from coding.proxy.convert.anthropic_to_gemini import convert_request


def _body(result):
    return result.body


def test_simple_text_message():
    result = _body(
        convert_request(
            {
                "model": "claude-sonnet-4-20250514",
                "messages": [{"role": "user", "content": "Hello"}],
                "max_tokens": 100,
            }
        )
    )
    assert result["contents"] == [{"role": "user", "parts": [{"text": "Hello"}]}]
    assert result["generationConfig"]["maxOutputTokens"] == 100
    assert "model" not in result


def test_multi_turn_conversation():
    result = _body(
        convert_request(
            {
                "messages": [
                    {"role": "user", "content": "What is 2+2?"},
                    {"role": "assistant", "content": "4"},
                    {"role": "user", "content": "And 3+3?"},
                ],
            }
        )
    )
    assert len(result["contents"]) == 3
    assert result["contents"][1]["role"] == "model"
    assert result["contents"][1]["parts"] == [{"text": "4"}]


def test_system_and_image_blocks():
    result = _body(
        convert_request(
            {
                "system": [{"type": "text", "text": "Rule 1"}],
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
                                    "data": "xxx",
                                },
                            },
                        ],
                    }
                ],
            }
        )
    )
    assert result["systemInstruction"]["parts"] == [{"text": "Rule 1"}]
    assert result["contents"][0]["parts"][1]["inlineData"]["mimeType"] == "image/png"


def test_tool_use_and_tool_result_blocks():
    result = _body(
        convert_request(
            {
                "messages": [
                    {
                        "role": "assistant",
                        "content": [
                            {
                                "type": "tool_use",
                                "id": "toolu_123",
                                "name": "get_weather",
                                "input": {"city": "Tokyo"},
                            }
                        ],
                    },
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "tool_result",
                                "tool_use_id": "toolu_123",
                                "content": "Sunny, 25°C",
                            }
                        ],
                    },
                ],
            }
        )
    )
    assert result["contents"][0]["parts"][0]["functionCall"]["name"] == "get_weather"
    assert (
        result["contents"][1]["parts"][0]["functionResponse"]["name"] == "get_weather"
    )


def test_generation_config_and_thinking_mapping():
    result = _body(
        convert_request(
            {
                "messages": [{"role": "user", "content": "Hi"}],
                "max_tokens": 2048,
                "temperature": 0.7,
                "top_p": 0.9,
                "top_k": 40,
                "stop_sequences": ["END", "STOP"],
                "extended_thinking": {"budget_tokens": 1000, "effort": "medium"},
            }
        )
    )
    gc = result["generationConfig"]
    assert gc["maxOutputTokens"] == 2048
    assert gc["temperature"] == 0.7
    assert gc["topP"] == 0.9
    assert gc["topK"] == 40
    assert gc["stopSequences"] == ["END", "STOP"]
    assert gc["thinkingConfig"]["thinkingBudget"] == 1000
    assert gc["thinkingConfig"]["thinkingLevel"] == "medium"


def test_tools_and_tool_choice_are_mapped():
    result = _body(
        convert_request(
            {
                "messages": [{"role": "user", "content": "Hi"}],
                "tools": [
                    {
                        "name": "tool1",
                        "description": "desc",
                        "input_schema": {"type": "object"},
                    }
                ],
                "tool_choice": {"type": "tool", "name": "tool1"},
            }
        )
    )
    assert result["tools"][0]["functionDeclarations"][0]["name"] == "tool1"
    assert result["toolConfig"]["functionCallingConfig"]["allowedFunctionNames"] == [
        "tool1"
    ]


def test_search_tool_maps_to_google_search():
    result = convert_request(
        {
            "messages": [{"role": "user", "content": "Hi"}],
            "tools": [{"name": "web_search_20250305"}],
        }
    )
    assert result.body["tools"][0] == {"googleSearch": {}}
    assert "search_tool_mapped_to_google_search" in result.adaptations


def test_metadata_is_recorded_as_adaptation():
    result = convert_request(
        {
            "messages": [{"role": "user", "content": "Hi"}],
            "metadata": {"user_id": "u-1"},
        }
    )
    assert "metadata_user_id_not_forwarded" in result.adaptations


def test_unknown_content_block_type_skipped():
    result = _body(
        convert_request(
            {
                "messages": [
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": "Hello"},
                            {"type": "unknown_type", "data": "???"},
                        ],
                    }
                ],
            }
        )
    )
    assert result["contents"][0]["parts"] == [{"text": "Hello"}]


# ── 新增测试用例 ──────────────────────────────────────


def test_tool_choice_none_maps_to_NONE():
    """tool_choice {type: "none"} → Gemini mode NONE."""
    result = _body(
        convert_request(
            {
                "messages": [{"role": "user", "content": "Hi"}],
                "tools": [{"name": "tool1", "input_schema": {"type": "object"}}],
                "tool_choice": {"type": "none"},
            }
        )
    )
    assert result["toolConfig"]["functionCallingConfig"]["mode"] == "NONE"


def test_tool_choice_string_none():
    """tool_choice "none" 字符串 → mode NONE."""
    result = _body(
        convert_request(
            {
                "messages": [{"role": "user", "content": "Hi"}],
                "tools": [{"name": "tool1", "input_schema": {"type": "object"}}],
                "tool_choice": "none",
            }
        )
    )
    assert result["toolConfig"]["functionCallingConfig"]["mode"] == "NONE"


def test_cache_control_stripped_from_system():
    """system 中的 cache_control 被剥离并记录."""
    result = convert_request(
        {
            "system": [
                {
                    "type": "text",
                    "text": "Rule 1",
                    "cache_control": {"type": "ephemeral"},
                }
            ],
            "messages": [{"role": "user", "content": "Hi"}],
        }
    )
    assert "cache_control_stripped_from_system" in result.adaptations
    si_parts = result.body["systemInstruction"]["parts"]
    assert "cache_control" not in si_parts[0]


def test_cache_control_stripped_from_content():
    """message content 中的 cache_control 被剥离."""
    result = convert_request(
        {
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "text",
                            "text": "Hello",
                            "cache_control": {"type": "ephemeral"},
                        },
                    ],
                }
            ],
        }
    )
    assert "cache_control_stripped_from_content" in result.adaptations


def test_empty_user_message_produces_padding():
    """空 user message 触发 contents padding."""
    result = convert_request(
        {
            "messages": [{"role": "user", "content": ""}],
        }
    )
    assert "empty_contents_padded" in result.adaptations
    assert len(result.body["contents"]) == 1
    assert result.body["contents"][0]["parts"][0]["text"] == " "


def test_all_messages_empty_produces_padding():
    """所有消息内容为空时仍保证 contents 非空."""
    result = convert_request(
        {
            "messages": [
                {"role": "user", "content": ""},
                {"role": "assistant", "content": ""},
            ],
        }
    )
    assert "empty_contents_padded" in result.adaptations
    assert len(result.body["contents"]) >= 1


def test_response_format_json_object():
    """response_format json_object → responseMimeType application/json."""
    result = _body(
        convert_request(
            {
                "messages": [{"role": "user", "content": "Hi"}],
                "response_format": {"type": "json_object"},
            }
        )
    )
    assert result["generationConfig"]["responseMimeType"] == "application/json"


def test_response_format_json_schema():
    """response_format json_schema type → responseMimeType application/json."""
    result = _body(
        convert_request(
            {
                "messages": [{"role": "user", "content": "Hi"}],
                "response_format": {
                    "type": "json_schema",
                    "json_schema": {"type": "object"},
                },
            }
        )
    )
    assert result["generationConfig"]["responseMimeType"] == "application/json"
    assert (
        "response_format_json_mode"
        in convert_request(
            {
                "messages": [{"role": "user", "content": "Hi"}],
                "response_format": {
                    "type": "json_schema",
                    "json_schema": {"type": "object"},
                },
            }
        ).adaptations
    )


def test_thinking_without_budget_defaults():
    """thinking 启用但无 budget_tokens 时默认 10000."""
    result = _body(
        convert_request(
            {
                "messages": [{"role": "user", "content": "Hi"}],
                "thinking": {"type": "enabled"},
            }
        )
    )
    assert result["generationConfig"]["thinkingConfig"]["thinkingBudget"] == 10000
    assert (
        "thinking_budget_defaulted_to_10k"
        in convert_request(
            {
                "messages": [{"role": "user", "content": "Hi"}],
                "thinking": {"type": "enabled"},
            },
        ).adaptations
    )


def test_large_tool_set_warning_logged():
    """超过 100 个工具时记录 adaptation."""
    tools = [
        {"name": f"tool_{i}", "input_schema": {"type": "object"}} for i in range(150)
    ]
    result = convert_request(
        {
            "messages": [{"role": "user", "content": "Hi"}],
            "tools": tools,
        }
    )
    assert any("large_tool_set_" in a for a in result.adaptations)
    assert "150" in [a for a in result.adaptations if "large_tool_set_" in a][0]


def test_tool_result_with_image_content():
    """tool_result 含图片块时降级为占位符."""
    result = _body(
        convert_request(
            {
                "messages": [
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "tool_result",
                                "tool_use_id": "toolu_1",
                                "content": [
                                    {"type": "text", "text": "result text"},
                                    {
                                        "type": "image",
                                        "source": {"type": "base64", "data": "abc123"},
                                    },
                                ],
                            }
                        ],
                    }
                ],
            }
        )
    )
    fr = result["contents"][0]["parts"][0]["functionResponse"]["response"]["result"]
    assert "[image]" in fr
    assert "result text" in fr


def test_safety_settings_in_result():
    """默认 safetySettings 存在于转换结果中."""
    result = _body(
        convert_request(
            {
                "messages": [{"role": "user", "content": "Hi"}],
            }
        )
    )
    assert "safetySettings" in result
    assert len(result["safetySettings"]) == 4
    categories = {s["category"] for s in result["safetySettings"]}
    assert "HARM_CATEGORY_HARASSMENT" in categories
    assert "HARM_CATEGORY_DANGEROUS_CONTENT" in categories


def test_custom_safety_settings_override():
    """自定义 safety_settings 覆盖默认值."""
    custom = {"HARM_CATEGORY_HARASSMENT": "BLOCK_MEDIUM_AND_ABOVE"}
    result = _body(
        convert_request(
            {"messages": [{"role": "user", "content": "Hi"}]},
            safety_settings=custom,
        )
    )
    assert len(result["safetySettings"]) == 1
    assert result["safetySettings"][0]["threshold"] == "BLOCK_MEDIUM_AND_ABOVE"


def test_metadata_empty_dict():
    """空 metadata dict 记录为 adaptation."""
    result = convert_request(
        {
            "messages": [{"role": "user", "content": "Hi"}],
            "metadata": {},
        }
    )
    assert "metadata_ignored" in result.adaptations


def test_multiple_search_tools():
    """多个搜索工具映射为单一 googleSearch."""
    result = _body(
        convert_request(
            {
                "messages": [{"role": "user", "content": "Hi"}],
                "tools": [
                    {"name": "web_search"},
                    {"name": "google_search"},
                    {"name": "tool_normal", "input_schema": {"type": "object"}},
                ],
            }
        )
    )
    search_tools = [t for t in result["tools"] if "googleSearch" in t]
    assert len(search_tools) == 1
    func_decls = result["tools"][0].get("functionDeclarations", [])
    assert any(d["name"] == "tool_normal" for d in func_decls)
