"""请求规范化测试."""

from __future__ import annotations

import copy

from coding.proxy.server.request_normalizer import (
    apply_anthropic_specific_fixes,
    normalize_anthropic_request,
)


def _apply_phase2(result):
    """在 deep copy 上执行 Phase 2（Anthropic 专属修复），返回 (body, fixes)."""
    body_copy = copy.deepcopy(result.body)
    fixes = apply_anthropic_specific_fixes(
        body_copy.get("messages", []),
        result.misplaced_tool_results,
        result.misplaced_log_info,
    )
    return body_copy, fixes


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


# ── Phase 1 仅规范化测试（vendor-agnostic）─────────────────────


class TestPhase1OnlyNormalization:
    """验证 Phase 1（vendor-agnostic）不执行 Anthropic 专属修复.

    Phase 1 仅执行 ID 重写、vendor block 移除、misplaced 信息收集。
    重定位和孤儿修复由 Phase 2（apply_anthropic_specific_fixes）延迟执行。
    """

    def test_phase1_keeps_misplaced_tool_result_in_place(self):
        """Phase 1 应保留 misplaced tool_result 在原位."""
        result = normalize_anthropic_request(
            {
                "messages": [
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
                                "content": "output",
                            },
                        ],
                    },
                ],
            }
        )

        assert result.recoverable is True
        assert "misplaced_tool_result_relocated" not in result.adaptations
        assert "orphaned_tool_use_repaired" not in result.adaptations
        # misplaced block 应仍在 assistant 消息中
        assistant_content = result.body["messages"][0]["content"]
        assert len(assistant_content) == 2
        assert assistant_content[1]["type"] == "tool_result"
        # Phase 2 上下文已收集
        assert len(result.misplaced_tool_results) == 1

    def test_phase1_does_not_repair_orphans(self):
        """Phase 1 不应为孤儿 tool_use 合成 tool_result."""
        result = normalize_anthropic_request(
            {
                "messages": [
                    {
                        "role": "assistant",
                        "content": [
                            {
                                "type": "tool_use",
                                "id": "toolu_123",
                                "name": "Bash",
                                "input": {"command": "ls"},
                            },
                        ],
                    },
                ],
            }
        )

        assert result.recoverable is True
        assert "orphaned_tool_use_repaired" not in result.adaptations
        assert len(result.body["messages"]) == 1  # 无合成 user 消息


# ── 跨供应商 tool_result 位置错位重定位测试（Phase 1 + Phase 2）──


class TestMisplacedToolResultRelocation:
    """验证 Phase 1 + Phase 2 组合能正确重定位 misplaced tool_result.

    Phase 1 收集 misplaced 信息，Phase 2 执行实际重定位。
    此类测试模拟 executor 在 Anthropic tier 时的完整行为。
    """

    def test_relocates_tool_result_from_assistant_message(self):
        """assistant 消息中的 tool_result 经 Phase 2 重定位到新建的 user 消息."""
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

        # Phase 2
        body, fixes = _apply_phase2(result)
        assert "misplaced_tool_result_relocated" in fixes

        messages = body["messages"]
        assert len(messages) == 3
        # assistant 消息应只保留 tool_use
        assistant_content = messages[1]["content"]
        assert len(assistant_content) == 1
        assert assistant_content[0]["type"] == "tool_use"
        assert assistant_content[0]["id"] == "toolu_123"
        # 新增一条 user 消息包含被重定位的 tool_result
        assert messages[2]["role"] == "user"
        relocated_block = messages[2]["content"][0]
        assert relocated_block["type"] == "tool_result"
        assert relocated_block["tool_use_id"] == "toolu_123"
        assert relocated_block["content"] == "file1.txt\nfile2.txt"

    def test_relocates_tool_result_preserves_other_blocks(self):
        """重定位 tool_result 时保留同消息中的其他内容块."""
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

        # Phase 2
        body, fixes = _apply_phase2(result)
        assert "misplaced_tool_result_relocated" in fixes

        messages = body["messages"]
        # assistant 消息保留 text + tool_use + text
        assistant_content = messages[0]["content"]
        assert len(assistant_content) == 3
        types = [b["type"] for b in assistant_content]
        assert types == ["text", "tool_use", "text"]
        # 新增 user 消息包含被重定位的 tool_result
        assert len(messages) == 2
        assert messages[1]["role"] == "user"
        assert messages[1]["content"][0]["type"] == "tool_result"
        assert messages[1]["content"][0]["tool_use_id"] == "toolu_456"

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
        assert not result.misplaced_tool_results  # 无 misplaced 块
        user_content = result.body["messages"][1]["content"]
        assert len(user_content) == 1
        assert user_content[0]["type"] == "tool_result"
        assert "misplaced_tool_result_relocated" not in result.adaptations

    def test_mixed_scenario_relocates_to_existing_user_message(self):
        """assistant 和 user 消息中同时有 tool_result 时，assistant 中的被重定位到 user 消息."""
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

        # Phase 2
        body, fixes = _apply_phase2(result)
        assert "misplaced_tool_result_relocated" in fixes

        # assistant 中的 tool_result 被移除
        assistant_content = body["messages"][0]["content"]
        assert len(assistant_content) == 1
        assert assistant_content[0]["type"] == "tool_use"
        # user 消息现在包含两个 tool_result（原有的 + 重定位来的）
        user_content = body["messages"][1]["content"]
        assert len(user_content) == 2
        assert all(b["type"] == "tool_result" for b in user_content)

    def test_deep_conversation_with_misplaced_tool_result_at_index_105(self):
        """模拟长对话（105+ 消息）中 tool_result 出现在 assistant 消息的场景."""
        # 构建一个 106 条消息的对话历史
        messages = []
        for i in range(104):
            role = "user" if i % 2 == 0 else "assistant"
            messages.append(
                {
                    "role": role,
                    "content": f"message {i}",
                }
            )

        # 在消息 104（assistant）中放置 tool_use + tool_result
        messages.append(
            {
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
            }
        )
        # 消息 105（user）
        messages.append(
            {
                "role": "user",
                "content": "thanks",
            }
        )

        result = normalize_anthropic_request({"messages": messages})

        # Phase 2
        body, fixes = _apply_phase2(result)
        assert "misplaced_tool_result_relocated" in fixes

        # 消息 104 中的 tool_result 应被移除
        assert len(body["messages"][104]["content"]) == 1
        assert body["messages"][104]["content"][0]["type"] == "tool_use"
        # 消息 105（user）应包含原始文本 + 被重定位的 tool_result
        user_content = body["messages"][105]["content"]
        assert isinstance(user_content, list)
        tool_result_blocks = [b for b in user_content if b.get("type") == "tool_result"]
        text_blocks = [b for b in user_content if b.get("type") == "text"]
        assert len(text_blocks) == 1
        assert text_blocks[0]["text"] == "thanks"
        assert len(tool_result_blocks) == 1
        assert tool_result_blocks[0]["tool_use_id"] == "toolu_deep_1"
        assert tool_result_blocks[0]["content"] == "/var/log/system.log"

    def test_rewrites_srvtoolu_id_when_relocating(self):
        """重定位时同时重写 srvtoolu_ 前缀的 tool_use_id."""
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
                            {
                                "type": "tool_result",
                                "tool_use_id": "srvtoolu_bad_1",
                                "content": "/home/user",
                            },
                        ],
                    },
                ],
            }
        )

        # Phase 2
        body, fixes = _apply_phase2(result)
        assert "misplaced_tool_result_relocated" in fixes
        assert "tool_result_tool_use_id_rewritten" in result.adaptations

        messages = body["messages"]
        # assistant 中的 tool_use ID 已重写
        new_id = messages[0]["content"][0]["id"]
        assert new_id.startswith("toolu_normalized_")
        # 重定位到新 user 消息的 tool_result 使用相同的重写 ID
        relocated = messages[1]["content"][0]
        assert relocated["type"] == "tool_result"
        assert relocated["tool_use_id"] == new_id


# ── 孤儿 tool_use 修复测试（Phase 1 + Phase 2）─────────────────────


class TestOrphanedToolUseRepair:
    """验证 Phase 1 + Phase 2 组合能正确修复孤儿 tool_use.

    Phase 1 收集上下文（如 tool_id_map），Phase 2 执行孤儿修复。
    此类测试模拟 executor 在 Anthropic tier 时的完整行为。
    """

    def test_synthesizes_result_for_orphaned_tool_use(self):
        """assistant 有 tool_use 且无后续 user 消息时，合成 user 消息含占位 tool_result."""
        result = normalize_anthropic_request(
            {
                "messages": [
                    {
                        "role": "assistant",
                        "content": [
                            {
                                "type": "tool_use",
                                "id": "toolu_123",
                                "name": "Bash",
                                "input": {"command": "ls"},
                            },
                        ],
                    },
                ],
            }
        )

        # Phase 2（手动调用，模拟 Anthropic tier 行为）
        body, fixes = _apply_phase2(result)
        assert "orphaned_tool_use_repaired" in fixes

        messages = body["messages"]
        assert len(messages) == 2
        assert messages[1]["role"] == "user"
        synthetic = messages[1]["content"][0]
        assert synthetic["type"] == "tool_result"
        assert synthetic["tool_use_id"] == "toolu_123"
        assert synthetic["is_error"] is True

    def test_synthesizes_result_appends_to_existing_user(self):
        """assistant 有 tool_use 且后续 user 消息为 text 时，追加合成 tool_result."""
        result = normalize_anthropic_request(
            {
                "messages": [
                    {
                        "role": "assistant",
                        "content": [
                            {
                                "type": "tool_use",
                                "id": "toolu_456",
                                "name": "Bash",
                                "input": {"command": "pwd"},
                            },
                        ],
                    },
                    {
                        "role": "user",
                        "content": "continue",
                    },
                ],
            }
        )

        # Phase 2（手动调用，模拟 Anthropic tier 行为）
        body, fixes = _apply_phase2(result)
        assert "orphaned_tool_use_repaired" in fixes

        messages = body["messages"]
        user_content = messages[1]["content"]
        assert isinstance(user_content, list)
        # 原始文本转为 text block + 合成的 tool_result
        assert len(user_content) == 2
        assert user_content[0]["type"] == "text"
        assert user_content[0]["text"] == "continue"
        assert user_content[1]["type"] == "tool_result"
        assert user_content[1]["tool_use_id"] == "toolu_456"
        assert user_content[1]["is_error"] is True

    def test_synthesizes_only_missing_results(self):
        """assistant 有 2 个 tool_use，user 仅有 1 个 tool_result 时，仅为缺失的合成."""
        result = normalize_anthropic_request(
            {
                "messages": [
                    {
                        "role": "assistant",
                        "content": [
                            {
                                "type": "tool_use",
                                "id": "toolu_A",
                                "name": "Bash",
                                "input": {"command": "ls"},
                            },
                            {
                                "type": "tool_use",
                                "id": "toolu_B",
                                "name": "Read",
                                "input": {"path": "/etc/hosts"},
                            },
                        ],
                    },
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "tool_result",
                                "tool_use_id": "toolu_A",
                                "content": "file1.txt",
                            },
                        ],
                    },
                ],
            }
        )

        # Phase 2（手动调用，模拟 Anthropic tier 行为）
        body, fixes = _apply_phase2(result)
        assert "orphaned_tool_use_repaired" in fixes

        user_content = body["messages"][1]["content"]
        assert len(user_content) == 2
        # 原有 tool_result 保持不变
        assert user_content[0]["tool_use_id"] == "toolu_A"
        # 合成的 tool_result 仅针对 toolu_B
        assert user_content[1]["type"] == "tool_result"
        assert user_content[1]["tool_use_id"] == "toolu_B"
        assert user_content[1]["is_error"] is True

    def test_no_repair_when_all_results_present(self):
        """正常配对场景不应触发修复."""
        result = normalize_anthropic_request(
            {
                "messages": [
                    {
                        "role": "assistant",
                        "content": [
                            {
                                "type": "tool_use",
                                "id": "toolu_ok",
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
                                "tool_use_id": "toolu_ok",
                                "content": "hi",
                            },
                        ],
                    },
                ],
            }
        )

        assert result.recoverable is True
        assert not result.misplaced_tool_results
        # Phase 2 不应有任何修复
        body, fixes = _apply_phase2(result)
        assert "orphaned_tool_use_repaired" not in fixes

    def test_repair_with_normalized_ids(self):
        """跨供应商降级场景：srvtoolu_ ID 被重写后仍需修复孤儿 tool_use.

        此场景下 tool_id_map 有条目，has_anthropic_fixes 为 True，
        executor 会自动触发 Phase 2。
        """
        result = normalize_anthropic_request(
            {
                "messages": [
                    {
                        "role": "assistant",
                        "content": [
                            {
                                "type": "server_tool_use",
                                "id": "srvtoolu_X",
                                "name": "Bash",
                                "input": {"command": "ls"},
                            },
                            {
                                "type": "server_tool_use",
                                "id": "srvtoolu_Y",
                                "name": "Read",
                                "input": {"path": "/tmp"},
                            },
                        ],
                    },
                    {
                        "role": "user",
                        "content": "continue",
                    },
                ],
            }
        )

        # has_anthropic_fixes 为 True（因 tool_id_map 有条目）
        assert result.has_anthropic_fixes is True

        # Phase 2
        body, fixes = _apply_phase2(result)
        assert "orphaned_tool_use_repaired" in fixes

        messages = body["messages"]
        # assistant 中 tool_use ID 已重写
        new_ids = [b["id"] for b in messages[0]["content"]]
        assert len(new_ids) == 2
        assert all(id_.startswith("toolu_normalized_") for id_ in new_ids)
        # user 消息应包含原始文本 + 两个合成的 tool_result
        user_content = messages[1]["content"]
        text_blocks = [b for b in user_content if b.get("type") == "text"]
        synthetic_results = [
            b
            for b in user_content
            if b.get("type") == "tool_result" and b.get("is_error") is True
        ]
        assert len(text_blocks) == 1
        assert len(synthetic_results) == 2
        assert {r["tool_use_id"] for r in synthetic_results} == set(new_ids)
