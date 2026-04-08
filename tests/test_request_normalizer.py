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


class TestRelocateMisplacedToolResults:
    """:func:`_relocate_misplaced_tool_results` 测试 — 覆盖 Anthropic 400
    ``tool_result can only be in user messages`` 错误的修复场景.
    """

    def test_relocates_tool_result_from_assistant_to_user(self):
        """assistant 消息中的 tool_result 应被迁移到最近的前置 user 消息."""
        result = normalize_anthropic_request(
            {
                "messages": [
                    {"role": "user", "content": [{"type": "text", "text": "hello"}]},
                    {
                        "role": "assistant",
                        "content": [
                            {"type": "text", "text": "let me check"},
                            {
                                "type": "tool_result",
                                "tool_use_id": "toolu_01",
                                "content": "result data",
                            },
                        ],
                    },
                ],
            }
        )

        assert result.recoverable is True
        msgs = result.body["messages"]
        # assistant 消息中不应再有 tool_result
        assistant_content = msgs[1]["content"]
        assert not any(
            b.get("type") == "tool_result" for b in assistant_content if isinstance(b, dict)
        )
        # tool_result 应出现在 user 消息中
        user_content = msgs[0]["content"]
        tool_results = [
            b for b in user_content if isinstance(b, dict) and b.get("type") == "tool_result"
        ]
        assert len(tool_results) == 1
        assert tool_results[0]["tool_use_id"] == "toolu_01"
        assert any("tool_result_relocated" in a for a in result.adaptations)

    def test_relocates_multiple_tool_results_from_assistant(self):
        """assistant 消息中的多个 tool_result 块应全部迁移."""
        result = normalize_anthropic_request(
            {
                "messages": [
                    {"role": "user", "content": [{"type": "text", "text": "hi"}]},
                    {
                        "role": "assistant",
                        "content": [
                            {
                                "type": "tool_result",
                                "tool_use_id": "toolu_01",
                                "content": "result1",
                            },
                            {
                                "type": "tool_result",
                                "tool_use_id": "toolu_02",
                                "content": "result2",
                            },
                        ],
                    },
                ],
            }
        )

        assert result.recoverable is True
        user_content = result.body["messages"][0]["content"]
        tool_results = [
            b for b in user_content if isinstance(b, dict) and b.get("type") == "tool_result"
        ]
        assert len(tool_results) == 2

    def test_creates_new_user_message_when_no_preceding_user(self):
        """无前置 user 消息时，应在头部创建新 user 消息容纳错位 tool_result."""
        result = normalize_anthropic_request(
            {
                "messages": [
                    {
                        "role": "assistant",
                        "content": [
                            {"type": "text", "text": "thinking..."},
                            {
                                "type": "tool_result",
                                "tool_use_id": "toolu_01",
                                "content": "orphan result",
                            },
                        ],
                    },
                ],
            }
        )

        assert result.recoverable is True
        msgs = result.body["messages"]
        # 新创建的 user 消息应在索引 0
        assert msgs[0]["role"] == "user"
        new_user_content = msgs[0]["content"]
        tool_results = [
            b
            for b in new_user_content
            if isinstance(b, dict) and b.get("type") == "tool_result"
        ]
        assert len(tool_results) == 1
        assert tool_results[0]["tool_use_id"] == "toolu_01"

    def test_finds_nearest_user_message_across_multiple_messages(self):
        """应跳过中间的 assistant 消息，找到最近的前置 user 消息."""
        result = normalize_anthropic_request(
            {
                "messages": [
                    {"role": "user", "content": [{"type": "text", "text": "first"}]},
                    {
                        "role": "assistant",
                        "content": [{"type": "tool_use", "id": "tu_1", "name": "bash", "input": {}}],
                    },
                    {"role": "user", "content": [{"type": "text", "text": "second"}]},
                    {
                        "role": "assistant",
                        "content": [
                            {"type": "text", "text": "processing"},
                            {
                                "type": "tool_result",
                                "tool_use_id": "tu_1",
                                "content": "bash output",
                            },
                        ],
                    },
                ],
            }
        )

        assert result.recoverable is True
        # tool_result 应被迁移到 messages[2]（第二个 user 消息），而非 messages[0]
        target_user = result.body["messages"][2]
        tool_results = [
            b
            for b in target_user["content"]
            if isinstance(b, dict) and b.get("type") == "tool_result"
        ]
        assert len(tool_results) == 1
        # 第一个 user 消息不应有新增的 tool_result
        first_user = result.body["messages"][0]
        first_trs = [
            b
            for b in first_user["content"]
            if isinstance(b, dict) and b.get("type") == "tool_result"
        ]
        assert len(first_trs) == 0

    def test_noop_when_tool_results_already_in_user_messages(self):
        """tool_result 已在正确位置时，不应触发迁移逻辑."""
        result = normalize_anthropic_request(
            {
                "messages": [
                    {"role": "user", "content": [{"type": "text", "text": "hello"}]},
                    {
                        "role": "assistant",
                        "content": [
                            {"type": "tool_use", "id": "tu_1", "name": "bash", "input": {}}
                        ],
                    },
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "tool_result",
                                "tool_use_id": "tu_1",
                                "content": "ok",
                            }
                        ],
                    },
                ],
            }
        )

        assert result.recoverable is True
        assert not any("tool_result_relocated" in a for a in result.adaptations)

    def test_preserves_existing_user_message_structure(self):
        """迁移不应破坏目标 user 消息的现有内容结构."""
        result = normalize_anthropic_request(
            {
                "messages": [
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": "original text"},
                            {"type": "image", "source": {"type": "base64", "data": "abc", "media_type": "img/png"}},
                        ],
                    },
                    {
                        "role": "assistant",
                        "content": [
                            {
                                "type": "tool_result",
                                "tool_use_id": "toolu_01",
                                "content": "moved result",
                            },
                        ],
                    },
                ],
            }
        )

        assert result.recoverable is True
        user_content = result.body["messages"][0]["content"]
        # 原有内容应保留
        assert any(isinstance(b, dict) and b.get("type") == "text" for b in user_content)
        assert any(isinstance(b, dict) and b.get("type") == "image" for b in user_content)
        # 迁移的 tool_result 应追加在末尾
        tool_results = [
            b for b in user_content if isinstance(b, dict) and b.get("type") == "tool_result"
        ]
        assert len(tool_results) == 1

    def test_handles_system_role_with_tool_result(self):
        """system 角色消息中的 tool_result 也应被迁移."""
        result = normalize_anthropic_request(
            {
                "messages": [
                    {"role": "user", "content": [{"type": "text", "text": "help"}]},
                    {
                        "role": "system",
                        "content": [
                            {
                                "type": "tool_result",
                                "tool_use_id": "toolu_01",
                                "content": "sys result",
                            },
                        ],
                    },
                ],
            }
        )

        assert result.recoverable is True
        # system 消息中不再有 tool_result
        sys_content = result.body["messages"][1]["content"]
        assert not any(
            b.get("type") == "tool_result" for b in sys_content if isinstance(b, dict)
        )
        # tool_result 在 user 消息中
        user_content = result.body["messages"][0]["content"]
        assert any(
            isinstance(b, dict) and b.get("type") == "tool_result" for b in user_content
        )
