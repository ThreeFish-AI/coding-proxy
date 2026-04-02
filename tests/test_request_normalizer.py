"""请求规范化测试."""

from __future__ import annotations

from coding.proxy.server.request_normalizer import normalize_anthropic_request


def test_rewrites_server_tool_use_to_standard_tool_use():
    result = normalize_anthropic_request({
        "messages": [
            {
                "role": "assistant",
                "content": [
                    {
                        "type": "server_tool_use",
                        "id": "srvtoolu_bad_1",
                        "name": "bash",
                        "input": {"cmd": "pwd"},
                    },
                ],
            },
            {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "srvtoolu_bad_1",
                        "content": "ok",
                    },
                ],
            },
        ],
    })

    assistant_block = result.body["messages"][0]["content"][0]
    user_block = result.body["messages"][1]["content"][0]
    assert result.recoverable is True
    assert assistant_block["type"] == "tool_use"
    assert assistant_block["id"].startswith("toolu_normalized_")
    assert user_block["tool_use_id"] == assistant_block["id"]
    assert "server_tool_use_id_rewritten_for_anthropic" in result.adaptations
    assert "tool_result_tool_use_id_rewritten" in result.adaptations


def test_filters_vendor_delta_blocks():
    result = normalize_anthropic_request({
        "messages": [
            {
                "role": "assistant",
                "content": [
                    {"type": "text", "text": "before"},
                    {"type": "server_tool_use_delta", "partial_json": '{"cmd":"pwd"}'},
                    {"type": "text", "text": "after"},
                ],
            },
        ],
    })

    content = result.body["messages"][0]["content"]
    assert len(content) == 2
    assert [block["type"] for block in content] == ["text", "text"]
    assert "vendor_block_removed:server_tool_use_delta" in result.adaptations


def test_unknown_tool_result_id_marks_fatal_reason():
    result = normalize_anthropic_request({
        "messages": [
            {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "bad_unknown_id",
                        "content": "nope",
                    },
                ],
            },
        ],
    })

    assert result.recoverable is False
    assert result.fatal_reasons
