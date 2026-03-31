"""Anthropic → Gemini 请求格式转换单元测试."""

from __future__ import annotations

import logging

from coding.proxy.convert.anthropic_to_gemini import convert_request


# --- 基础消息转换 ---


def test_simple_text_message():
    """纯文本 user 消息."""
    body = {
        "model": "claude-sonnet-4-20250514",
        "messages": [{"role": "user", "content": "Hello"}],
        "max_tokens": 100,
    }
    result = convert_request(body)
    assert result["contents"] == [{"role": "user", "parts": [{"text": "Hello"}]}]
    assert result["generationConfig"]["maxOutputTokens"] == 100
    # model 不在转换结果中（由后端控制端点）
    assert "model" not in result


def test_multi_turn_conversation():
    """多轮对话 user/assistant 交替."""
    body = {
        "messages": [
            {"role": "user", "content": "What is 2+2?"},
            {"role": "assistant", "content": "4"},
            {"role": "user", "content": "And 3+3?"},
        ],
    }
    result = convert_request(body)
    assert len(result["contents"]) == 3
    assert result["contents"][0]["role"] == "user"
    assert result["contents"][1]["role"] == "model"  # assistant → model
    assert result["contents"][2]["role"] == "user"
    assert result["contents"][1]["parts"] == [{"text": "4"}]


def test_empty_messages():
    """空消息列表."""
    result = convert_request({"messages": []})
    assert result["contents"] == []


def test_empty_content_string():
    """空字符串内容被跳过."""
    body = {"messages": [{"role": "user", "content": ""}]}
    result = convert_request(body)
    # 空内容消息不产生 parts，因此不产生 contents 条目
    assert result["contents"] == []


# --- system prompt 转换 ---


def test_system_string():
    """字符串形式的 system prompt."""
    body = {
        "system": "You are helpful.",
        "messages": [{"role": "user", "content": "Hi"}],
    }
    result = convert_request(body)
    assert result["systemInstruction"] == {
        "parts": [{"text": "You are helpful."}],
    }


def test_system_list():
    """列表形式的 system prompt."""
    body = {
        "system": [
            {"type": "text", "text": "Rule 1"},
            {"type": "text", "text": "Rule 2"},
        ],
        "messages": [{"role": "user", "content": "Hi"}],
    }
    result = convert_request(body)
    assert result["systemInstruction"]["parts"] == [
        {"text": "Rule 1"},
        {"text": "Rule 2"},
    ]


def test_system_none():
    """无 system prompt."""
    body = {"messages": [{"role": "user", "content": "Hi"}]}
    result = convert_request(body)
    assert "systemInstruction" not in result


# --- 结构化内容块 ---


def test_text_content_block():
    """text 类型内容块."""
    body = {
        "messages": [{
            "role": "user",
            "content": [{"type": "text", "text": "Hello world"}],
        }],
    }
    result = convert_request(body)
    assert result["contents"][0]["parts"] == [{"text": "Hello world"}]


def test_image_content_block():
    """base64 图片内容块."""
    body = {
        "messages": [{
            "role": "user",
            "content": [{
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": "image/jpeg",
                    "data": "abc123==",
                },
            }],
        }],
    }
    result = convert_request(body)
    parts = result["contents"][0]["parts"]
    assert len(parts) == 1
    assert parts[0]["inlineData"]["mimeType"] == "image/jpeg"
    assert parts[0]["inlineData"]["data"] == "abc123=="


def test_tool_use_content_block():
    """tool_use 内容块 → functionCall."""
    body = {
        "messages": [{
            "role": "assistant",
            "content": [{
                "type": "tool_use",
                "id": "toolu_123",
                "name": "get_weather",
                "input": {"city": "Tokyo"},
            }],
        }],
    }
    result = convert_request(body)
    parts = result["contents"][0]["parts"]
    assert parts[0]["functionCall"]["name"] == "get_weather"
    assert parts[0]["functionCall"]["args"] == {"city": "Tokyo"}


def test_tool_result_content_block():
    """tool_result 内容块 → functionResponse."""
    body = {
        "messages": [{
            "role": "user",
            "content": [{
                "type": "tool_result",
                "tool_use_id": "toolu_123",
                "content": "Sunny, 25°C",
            }],
        }],
    }
    result = convert_request(body)
    parts = result["contents"][0]["parts"]
    assert parts[0]["functionResponse"]["name"] == "toolu_123"
    assert parts[0]["functionResponse"]["response"]["result"] == "Sunny, 25°C"


def test_mixed_content_blocks():
    """混合内容块（text + image）."""
    body = {
        "messages": [{
            "role": "user",
            "content": [
                {"type": "text", "text": "What is this?"},
                {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "xxx"}},
            ],
        }],
    }
    result = convert_request(body)
    parts = result["contents"][0]["parts"]
    assert len(parts) == 2
    assert parts[0] == {"text": "What is this?"}
    assert "inlineData" in parts[1]


# --- generationConfig 参数映射 ---


def test_all_generation_params():
    """所有生成参数映射."""
    body = {
        "messages": [{"role": "user", "content": "Hi"}],
        "max_tokens": 2048,
        "temperature": 0.7,
        "top_p": 0.9,
        "top_k": 40,
        "stop_sequences": ["END", "STOP"],
    }
    result = convert_request(body)
    gc = result["generationConfig"]
    assert gc["maxOutputTokens"] == 2048
    assert gc["temperature"] == 0.7
    assert gc["topP"] == 0.9
    assert gc["topK"] == 40
    assert gc["stopSequences"] == ["END", "STOP"]


def test_no_generation_params():
    """无生成参数时不产生 generationConfig."""
    body = {"messages": [{"role": "user", "content": "Hi"}]}
    result = convert_request(body)
    assert "generationConfig" not in result


def test_partial_generation_params():
    """仅部分生成参数."""
    body = {
        "messages": [{"role": "user", "content": "Hi"}],
        "temperature": 0.5,
    }
    result = convert_request(body)
    gc = result["generationConfig"]
    assert gc == {"temperature": 0.5}


# --- 不支持字段处理 ---


def test_unsupported_fields_logged(caplog):
    """不支持的字段被记录 WARNING."""
    body = {
        "messages": [{"role": "user", "content": "Hi"}],
        "tools": [{"name": "tool1"}],
        "extended_thinking": {"budget_tokens": 1000},
    }
    with caplog.at_level(logging.WARNING, logger="coding.proxy.convert.anthropic_to_gemini"):
        result = convert_request(body)
    assert "tools" not in result
    assert "extended_thinking" not in result
    assert any("tools" in r.message for r in caplog.records)
    assert any("extended_thinking" in r.message for r in caplog.records)


def test_stream_field_not_in_result():
    """stream 字段不出现在转换结果中."""
    body = {
        "messages": [{"role": "user", "content": "Hi"}],
        "stream": True,
    }
    result = convert_request(body)
    assert "stream" not in result


# --- 未知内容块类型 ---


def test_unknown_content_block_type_skipped():
    """未知内容块类型被跳过."""
    body = {
        "messages": [{
            "role": "user",
            "content": [
                {"type": "text", "text": "Hello"},
                {"type": "unknown_type", "data": "???"},
            ],
        }],
    }
    result = convert_request(body)
    parts = result["contents"][0]["parts"]
    assert len(parts) == 1  # 只有 text 被转换
    assert parts[0] == {"text": "Hello"}
