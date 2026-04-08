"""请求规范化测试."""

from __future__ import annotations

from coding.proxy.server.request_normalizer import normalize_anthropic_request


def test_rewrites_server_tool_use_to_standard_tool_use():
    result = normalize_anthropic_request(
        {
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
        }
    )

    assistant_block = result.body["messages"][0]["content"][0]
    user_block = result.body["messages"][1]["content"][0]
    assert result.recoverable is True
    assert assistant_block["type"] == "tool_use"
    assert assistant_block["id"].startswith("toolu_normalized_")
    assert user_block["tool_use_id"] == assistant_block["id"]
    assert "server_tool_use_id_rewritten_for_anthropic" in result.adaptations
    assert "tool_result_tool_use_id_rewritten" in result.adaptations


def test_filters_vendor_delta_blocks():
    result = normalize_anthropic_request(
        {
            "messages": [
                {
                    "role": "assistant",
                    "content": [
                        {"type": "text", "text": "before"},
                        {
                            "type": "server_tool_use_delta",
                            "partial_json": '{"cmd":"pwd"}',
                        },
                        {"type": "text", "text": "after"},
                    ],
                },
            ],
        }
    )

    content = result.body["messages"][0]["content"]
    assert len(content) == 2
    assert [block["type"] for block in content] == ["text", "text"]
    assert "vendor_block_removed:server_tool_use_delta" in result.adaptations


def test_unknown_tool_result_id_marks_fatal_reason():
    result = normalize_anthropic_request(
        {
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
        }
    )

    assert result.recoverable is False
    assert result.fatal_reasons


# ── 跨供应商 tool_result 位置错位剥离测试 ──────────────────────


class TestMisplacedToolResultStripping:
    """验证 tool_result 出现在非 user 消息中时被正确剥离.

    典型触发场景：Zhipu GLM-5 通过 Anthropic 兼容端点返回的 assistant 响应中
    同时包含 tool_use 和 tool_result，Claude Code 将其存入对话历史后，
    后续请求的 assistant message 中包含 tool_result。
    Anthropic API 严格要求 tool_result 只能出现在 user 消息中，
    因此必须从非 user 消息中剥离。
    """

    def test_strips_tool_result_from_assistant_message(self):
        """assistant 消息中的 tool_result 应被剥离，保留 tool_use."""
        result = normalize_anthropic_request(
            {
                "messages": [
                    {
                        "role": "user",
                        "content": "run ls",
                    },
                    {
                        "role": "assistant",
                        "content": [
                            {
                                "type": "tool_use",
                                "id": "toolu_123",
                                "name": "Bash",
                                "input": {"command": "ls"},
                            },
                            {
                                "type": "tool_result",
                                "tool_use_id": "toolu_123",
                                "content": "file1.txt\nfile2.txt",
                            },
                        ],
                    },
                ],
            }
        )

        assert result.recoverable is True
        # assistant 消息应只保留 tool_use
        assistant_content = result.body["messages"][1]["content"]
        assert len(assistant_content) == 1
        assert assistant_content[0]["type"] == "tool_use"
        assert assistant_content[0]["id"] == "toolu_123"
        assert "misplaced_tool_result_stripped" in result.adaptations

    def test_strips_tool_result_preserves_other_blocks(self):
        """剥离 tool_result 时保留同消息中的其他内容块."""
        result = normalize_anthropic_request(
            {
                "messages": [
                    {
                        "role": "assistant",
                        "content": [
                            {"type": "text", "text": "Let me check."},
                            {
                                "type": "tool_use",
                                "id": "toolu_456",
                                "name": "Read",
                                "input": {"path": "/etc/hosts"},
                            },
                            {
                                "type": "tool_result",
                                "tool_use_id": "toolu_456",
                                "content": "127.0.0.1 localhost",
                            },
                            {"type": "text", "text": "Done."},
                        ],
                    },
                ],
            }
        )

        assert result.recoverable is True
        assistant_content = result.body["messages"][0]["content"]
        assert len(assistant_content) == 3
        types = [b["type"] for b in assistant_content]
        assert types == ["text", "tool_use", "text"]
        assert "misplaced_tool_result_stripped" in result.adaptations

    def test_tool_result_in_user_message_untouched(self):
        """user 消息中的 tool_result 不受影响."""
        result = normalize_anthropic_request(
            {
                "messages": [
                    {
                        "role": "assistant",
                        "content": [
                            {
                                "type": "tool_use",
                                "id": "toolu_789",
                                "name": "Bash",
                                "input": {"command": "echo hi"},
                            },
                        ],
                    },
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "tool_result",
                                "tool_use_id": "toolu_789",
                                "content": "hi",
                            },
                        ],
                    },
                ],
            }
        )

        assert result.recoverable is True
        user_content = result.body["messages"][1]["content"]
        assert len(user_content) == 1
        assert user_content[0]["type"] == "tool_result"
        assert "misplaced_tool_result_stripped" not in result.adaptations

    def test_mixed_scenario_assistant_and_user_tool_results(self):
        """assistant 和 user 消息中同时有 tool_result 时，仅剥离 assistant 中的."""
        result = normalize_anthropic_request(
            {
                "messages": [
                    {
                        "role": "assistant",
                        "content": [
                            {
                                "type": "tool_use",
                                "id": "toolu_100",
                                "name": "Bash",
                                "input": {"command": "ls"},
                            },
                            {
                                "type": "tool_result",
                                "tool_use_id": "toolu_100",
                                "content": "misplaced result",
                            },
                        ],
                    },
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "tool_result",
                                "tool_use_id": "toolu_100",
                                "content": "correct result",
                            },
                        ],
                    },
                ],
            }
        )

        assert result.recoverable is True
        # assistant 中的 tool_result 被剥离
        assistant_content = result.body["messages"][0]["content"]
        assert len(assistant_content) == 1
        assert assistant_content[0]["type"] == "tool_use"
        # user 中的 tool_result 保留
        user_content = result.body["messages"][1]["content"]
        assert len(user_content) == 1
        assert user_content[0]["type"] == "tool_result"
        assert user_content[0]["content"] == "correct result"
        assert "misplaced_tool_result_stripped" in result.adaptations

    def test_deep_conversation_with_misplaced_tool_result_at_index_105(self):
        """模拟长对话（105+ 消息）中 tool_result 出现在 assistant 消息的场景.

        重现原始 bug 报告中的错误：messages.105 处 tool_result 位置不合规。
        """
        # 构建一个 106 条消息的对话历史
        messages = []
        for i in range(104):
            role = "user" if i % 2 == 0 else "assistant"
            messages.append({
                "role": role,
                "content": f"message {i}",
            })

        # 在消息 104（assistant）中放置 tool_use + tool_result
        messages.append({
            "role": "assistant",
            "content": [
                {
                    "type": "tool_use",
                    "id": "toolu_deep_1",
                    "name": "Bash",
                    "input": {"command": "find / -name '*.log'"},
                },
                {
                    "type": "tool_result",
                    "tool_use_id": "toolu_deep_1",
                    "content": "/var/log/system.log",
                },
            ],
        })
        # 消息 105（user）
        messages.append({
            "role": "user",
            "content": "thanks",
        })

        result = normalize_anthropic_request({"messages": messages})

        assert result.recoverable is True
        # 消息 104 中的 tool_result 应被剥离
        assert len(result.body["messages"][104]["content"]) == 1
        assert result.body["messages"][104]["content"][0]["type"] == "tool_use"
        assert "misplaced_tool_result_stripped" in result.adaptations
