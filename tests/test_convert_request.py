"""Anthropic → Gemini 请求格式转换单元测试."""

from __future__ import annotations

from coding.proxy.convert.anthropic_to_gemini import convert_request


def _body(result):
    return result.body


def test_simple_text_message():
    result = _body(convert_request({
        "model": "claude-sonnet-4-20250514",
        "messages": [{"role": "user", "content": "Hello"}],
        "max_tokens": 100,
    }))
    assert result["contents"] == [{"role": "user", "parts": [{"text": "Hello"}]}]
    assert result["generationConfig"]["maxOutputTokens"] == 100
    assert "model" not in result


def test_multi_turn_conversation():
    result = _body(convert_request({
        "messages": [
            {"role": "user", "content": "What is 2+2?"},
            {"role": "assistant", "content": "4"},
            {"role": "user", "content": "And 3+3?"},
        ],
    }))
    assert len(result["contents"]) == 3
    assert result["contents"][1]["role"] == "model"
    assert result["contents"][1]["parts"] == [{"text": "4"}]


def test_system_and_image_blocks():
    result = _body(convert_request({
        "system": [{"type": "text", "text": "Rule 1"}],
        "messages": [{
            "role": "user",
            "content": [
                {"type": "text", "text": "What is this?"},
                {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "xxx"}},
            ],
        }],
    }))
    assert result["systemInstruction"]["parts"] == [{"text": "Rule 1"}]
    assert result["contents"][0]["parts"][1]["inlineData"]["mimeType"] == "image/png"


def test_tool_use_and_tool_result_blocks():
    result = _body(convert_request({
        "messages": [
            {
                "role": "assistant",
                "content": [{
                    "type": "tool_use",
                    "id": "toolu_123",
                    "name": "get_weather",
                    "input": {"city": "Tokyo"},
                }],
            },
            {
                "role": "user",
                "content": [{
                    "type": "tool_result",
                    "tool_use_id": "toolu_123",
                    "content": "Sunny, 25°C",
                }],
            },
        ],
    }))
    assert result["contents"][0]["parts"][0]["functionCall"]["name"] == "get_weather"
    assert result["contents"][1]["parts"][0]["functionResponse"]["name"] == "get_weather"


def test_generation_config_and_thinking_mapping():
    result = _body(convert_request({
        "messages": [{"role": "user", "content": "Hi"}],
        "max_tokens": 2048,
        "temperature": 0.7,
        "top_p": 0.9,
        "top_k": 40,
        "stop_sequences": ["END", "STOP"],
        "extended_thinking": {"budget_tokens": 1000, "effort": "medium"},
    }))
    gc = result["generationConfig"]
    assert gc["maxOutputTokens"] == 2048
    assert gc["temperature"] == 0.7
    assert gc["topP"] == 0.9
    assert gc["topK"] == 40
    assert gc["stopSequences"] == ["END", "STOP"]
    assert gc["thinkingConfig"]["thinkingBudget"] == 1000
    assert gc["thinkingConfig"]["thinkingLevel"] == "medium"


def test_tools_and_tool_choice_are_mapped():
    result = _body(convert_request({
        "messages": [{"role": "user", "content": "Hi"}],
        "tools": [{"name": "tool1", "description": "desc", "input_schema": {"type": "object"}}],
        "tool_choice": {"type": "tool", "name": "tool1"},
    }))
    assert result["tools"][0]["functionDeclarations"][0]["name"] == "tool1"
    assert result["toolConfig"]["functionCallingConfig"]["allowedFunctionNames"] == ["tool1"]


def test_search_tool_maps_to_google_search():
    result = convert_request({
        "messages": [{"role": "user", "content": "Hi"}],
        "tools": [{"name": "web_search_20250305"}],
    })
    assert result.body["tools"][0] == {"googleSearch": {}}
    assert "search_tool_mapped_to_google_search" in result.adaptations


def test_metadata_is_recorded_as_adaptation():
    result = convert_request({
        "messages": [{"role": "user", "content": "Hi"}],
        "metadata": {"user_id": "u-1"},
    })
    assert "metadata_user_id_not_forwarded" in result.adaptations


def test_unknown_content_block_type_skipped():
    result = _body(convert_request({
        "messages": [{
            "role": "user",
            "content": [
                {"type": "text", "text": "Hello"},
                {"type": "unknown_type", "data": "???"},
            ],
        }],
    }))
    assert result["contents"][0]["parts"] == [{"text": "Hello"}]
