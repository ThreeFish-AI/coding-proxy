"""请求规范化测试."""

from __future__ import annotations

import copy

from coding.proxy.convert.vendor_channels import (
    enforce_anthropic_tool_pairing,
    strip_thinking_blocks,
)
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


# ── Phase 1 仅规范化测试（vendor-agnostic）─────────────────────


class TestPhase1OnlyNormalization:
    """验证 Phase 1（vendor-agnostic）仅执行 ID 重写和 vendor block 移除.

    跨供应商的 tool_use/tool_result 配对修复由 vendor_channels.py 中的
    enforce_anthropic_tool_pairing() 在源→目标转换通道中处理。
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
        # misplaced block 应仍在 assistant 消息中（Phase 1 不处理）
        assistant_content = result.body["messages"][0]["content"]
        assert len(assistant_content) == 2
        assert assistant_content[1]["type"] == "tool_result"

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

    def test_phase1_then_enforce_pairing(self):
        """Phase 1 + enforce_anthropic_tool_pairing 端到端测试（模拟完整链路）."""
        result = normalize_anthropic_request(
            {
                "messages": [
                    {"role": "user", "content": "hello"},
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
                            {
                                "type": "tool_result",
                                "tool_use_id": "srvtoolu_X",
                                "content": "zhipu result",
                            },
                        ],
                    },
                    {"role": "user", "content": "continue"},
                ],
            }
        )
        assert result.recoverable

        # 在 deep copy 上执行 enforce_anthropic_tool_pairing（模拟 vendor channel）
        body_copy = copy.deepcopy(result.body)
        fixes = enforce_anthropic_tool_pairing(body_copy.get("messages", []))
        assert "misplaced_tool_result_relocated" in fixes
        assert "orphaned_tool_use_repaired" in fixes

        messages = body_copy["messages"]
        # assistant 只保留 2 个 tool_use
        assistant_content = messages[1]["content"]
        assert len(assistant_content) == 2
        assert all(b["type"] == "tool_use" for b in assistant_content)
        # user 消息包含 2 个 tool_result
        user_content = messages[2]["content"]
        result_blocks = [b for b in user_content if b.get("type") == "tool_result"]
        assert len(result_blocks) == 2
        expected_ids = {b["id"] for b in assistant_content}
        result_ids = {b["tool_use_id"] for b in result_blocks}
        assert result_ids == expected_ids


# ── strip_thinking_blocks 函数测试 ──────────────────────────────


class TestStripThinkingBlocks:
    """验证 strip_thinking_blocks 函数（从 vendors/anthropic.py 迁入）."""

    def test_strips_thinking_blocks(self):
        """剥离 assistant 消息中的 thinking blocks."""
        body = {
            "messages": [
                {
                    "role": "assistant",
                    "content": [
                        {
                            "type": "thinking",
                            "thinking": "Let me think...",
                            "signature": "sig",
                        },
                        {"type": "text", "text": "Here is my answer."},
                    ],
                },
            ],
        }
        stripped = strip_thinking_blocks(body)
        assert stripped == 1
        content = body["messages"][0]["content"]
        assert len(content) == 1
        assert content[0]["type"] == "text"

    def test_strips_redacted_thinking_blocks(self):
        """剥离 redacted_thinking blocks."""
        body = {
            "messages": [
                {
                    "role": "assistant",
                    "content": [
                        {"type": "redacted_thinking", "data": "base64"},
                        {"type": "text", "text": "response"},
                    ],
                },
            ],
        }
        stripped = strip_thinking_blocks(body)
        assert stripped == 1
        assert body["messages"][0]["content"][0]["type"] == "text"

    def test_inserts_placeholder_when_content_becomes_empty(self):
        """剥离后 content 为空时插入占位 text block."""
        body = {
            "messages": [
                {"role": "user", "content": "hello"},
                {
                    "role": "assistant",
                    "content": [
                        {
                            "type": "thinking",
                            "thinking": "thought",
                            "signature": "sig",
                        },
                    ],
                },
                {"role": "user", "content": "follow up"},
            ],
        }
        stripped = strip_thinking_blocks(body)
        assert stripped == 1
        content = body["messages"][1]["content"]
        assert len(content) == 1
        assert content[0]["type"] == "text"
        assert content[0]["text"] == "[thinking]"

    def test_preserves_user_messages(self):
        """user 消息不受影响."""
        body = {
            "messages": [
                {"role": "user", "content": [{"type": "text", "text": "hello"}]},
            ],
        }
        stripped = strip_thinking_blocks(body)
        assert stripped == 0
        assert body["messages"][0]["content"][0]["text"] == "hello"

    def test_preserves_top_level_thinking_param(self):
        """body 顶层的 thinking 参数不受影响."""
        body = {
            "thinking": {"type": "enabled", "budget_tokens": 10000},
            "messages": [
                {
                    "role": "assistant",
                    "content": [
                        {"type": "thinking", "thinking": "old", "signature": "sig"},
                        {"type": "text", "text": "response"},
                    ],
                },
            ],
        }
        stripped = strip_thinking_blocks(body)
        assert stripped == 1
        assert body["thinking"] == {"type": "enabled", "budget_tokens": 10000}

    def test_multi_turn_strips_all(self):
        """多轮对话中所有 assistant thinking blocks 均被剥离."""
        body = {
            "messages": [
                {
                    "role": "assistant",
                    "content": [
                        {"type": "thinking", "thinking": "t1", "signature": "s1"},
                        {"type": "text", "text": "r1"},
                    ],
                },
                {"role": "user", "content": "follow up"},
                {
                    "role": "assistant",
                    "content": [
                        {"type": "thinking", "thinking": "t2", "signature": "s2"},
                        {"type": "text", "text": "r2"},
                    ],
                },
            ],
        }
        stripped = strip_thinking_blocks(body)
        assert stripped == 2
        assert len(body["messages"][0]["content"]) == 1
        assert len(body["messages"][2]["content"]) == 1

    def test_returns_zero_when_no_thinking(self):
        """无 thinking blocks 时返回 0."""
        body = {
            "messages": [
                {
                    "role": "assistant",
                    "content": [{"type": "text", "text": "response"}],
                },
            ],
        }
        stripped = strip_thinking_blocks(body)
        assert stripped == 0


# ── enforce_anthropic_tool_pairing 函数测试 ──────────────────────


def _enforce_pairing(messages):
    """在 messages 上直接执行 enforce_anthropic_tool_pairing，返回 (messages, fixes)."""
    fixes = enforce_anthropic_tool_pairing(messages)
    return messages, fixes


class TestEnforceAnthropicToolPairing:
    """验证 enforce_anthropic_tool_pairing 单遍强制配对函数.

    通过单次正向遍历完成 tool_result 剥离、重定位和孤儿合成。
    """

    # ── 基础场景 ────────────────────────────────────────────

    def test_no_change_when_correctly_paired(self):
        """正确配对的 tool_use/tool_result 不受影响."""
        messages = [
            {
                "role": "assistant",
                "content": [
                    {
                        "type": "tool_use",
                        "id": "toolu_ok",
                        "name": "Bash",
                        "input": {"command": "ls"},
                    },
                ],
            },
            {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "toolu_ok",
                        "content": "file.txt",
                    },
                ],
            },
        ]
        _, fixes = _enforce_pairing(messages)
        assert not fixes
        assert len(messages) == 2
        assert messages[1]["content"][0]["tool_use_id"] == "toolu_ok"

    def test_strips_tool_result_from_assistant_and_relocates(self):
        """从 assistant 剥离 tool_result 并重定位到紧邻 user 消息."""
        messages = [
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
            {
                "role": "user",
                "content": "thanks",
            },
        ]
        _, fixes = _enforce_pairing(messages)
        assert "misplaced_tool_result_relocated" in fixes
        # assistant 只保留 tool_use
        assert len(messages[0]["content"]) == 1
        assert messages[0]["content"][0]["type"] == "tool_use"
        # user 消息包含原始文本 + 重定位的 tool_result
        user_content = messages[1]["content"]
        assert isinstance(user_content, list)
        text_blocks = [b for b in user_content if b.get("type") == "text"]
        result_blocks = [b for b in user_content if b.get("type") == "tool_result"]
        assert len(text_blocks) == 1
        assert text_blocks[0]["text"] == "thanks"
        assert len(result_blocks) == 1
        assert result_blocks[0]["tool_use_id"] == "toolu_123"

    def test_synthesizes_missing_tool_result(self):
        """缺失 tool_result 时合成 is_error=True 占位块."""
        messages = [
            {
                "role": "assistant",
                "content": [
                    {
                        "type": "tool_use",
                        "id": "toolu_orphan",
                        "name": "Bash",
                        "input": {"command": "pwd"},
                    },
                ],
            },
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "continue"},
                ],
            },
        ]
        _, fixes = _enforce_pairing(messages)
        assert "orphaned_tool_use_repaired" in fixes
        user_content = messages[1]["content"]
        synthetic = [b for b in user_content if b.get("is_error") is True]
        assert len(synthetic) == 1
        assert synthetic[0]["tool_use_id"] == "toolu_orphan"

    # ── Zhipu 特征场景 ──────────────────────────────────────

    def test_zhipu_3_tool_use_1_misplaced_result(self):
        """zhipu 典型产物: 3 tool_use + 1 misplaced tool_result，修复后完整配对."""
        messages = [
            {"role": "user", "content": "run tools"},
            {
                "role": "assistant",
                "content": [
                    {
                        "type": "tool_use",
                        "id": "toolu_normalized_5",
                        "name": "Bash",
                        "input": {},
                    },
                    {
                        "type": "tool_use",
                        "id": "toolu_normalized_6",
                        "name": "Read",
                        "input": {},
                    },
                    {
                        "type": "tool_use",
                        "id": "toolu_normalized_7",
                        "name": "Write",
                        "input": {},
                    },
                    {
                        "type": "tool_result",
                        "tool_use_id": "toolu_normalized_5",
                        "content": "result from zhipu",
                    },
                ],
            },
            {"role": "user", "content": "continue"},
        ]
        _, fixes = _enforce_pairing(messages)
        assert "misplaced_tool_result_relocated" in fixes
        assert "orphaned_tool_use_repaired" in fixes
        # assistant 只保留 3 个 tool_use
        assert len(messages[1]["content"]) == 3
        assert all(b["type"] == "tool_use" for b in messages[1]["content"])
        # user 消息包含所有 3 个 tool_result
        user_content = messages[2]["content"]
        result_ids = {
            b["tool_use_id"]
            for b in user_content
            if isinstance(b, dict) and b.get("type") == "tool_result"
        }
        assert result_ids == {
            "toolu_normalized_5",
            "toolu_normalized_6",
            "toolu_normalized_7",
        }

    def test_zhipu_all_3_results_in_assistant(self):
        """zhipu 产物: 3 tool_use + 3 tool_result 都在 assistant 中."""
        messages = [
            {
                "role": "assistant",
                "content": [
                    {"type": "tool_use", "id": "toolu_A", "name": "Bash", "input": {}},
                    {"type": "tool_use", "id": "toolu_B", "name": "Read", "input": {}},
                    {"type": "tool_use", "id": "toolu_C", "name": "Write", "input": {}},
                    {"type": "tool_result", "tool_use_id": "toolu_A", "content": "a"},
                    {"type": "tool_result", "tool_use_id": "toolu_B", "content": "b"},
                    {"type": "tool_result", "tool_use_id": "toolu_C", "content": "c"},
                ],
            },
            {"role": "user", "content": "thanks"},
        ]
        _, fixes = _enforce_pairing(messages)
        assert "misplaced_tool_result_relocated" in fixes
        assert "orphaned_tool_use_repaired" not in fixes
        # assistant 只保留 tool_use
        assert len(messages[0]["content"]) == 3
        # user 消息含所有 tool_result
        result_ids = {
            b["tool_use_id"]
            for b in messages[1]["content"]
            if b.get("type") == "tool_result"
        }
        assert result_ids == {"toolu_A", "toolu_B", "toolu_C"}

    def test_no_subsequent_user_message_inserts_synthetic(self):
        """assistant 是最后一条消息时插入合成 user 消息."""
        messages = [
            {
                "role": "assistant",
                "content": [
                    {
                        "type": "tool_use",
                        "id": "toolu_last",
                        "name": "Bash",
                        "input": {},
                    },
                    {
                        "type": "tool_result",
                        "tool_use_id": "toolu_last",
                        "content": "done",
                    },
                ],
            },
        ]
        _, fixes = _enforce_pairing(messages)
        assert "misplaced_tool_result_relocated" in fixes
        assert len(messages) == 2
        assert messages[1]["role"] == "user"
        assert messages[1]["content"][0]["tool_use_id"] == "toolu_last"

    # ── 边缘情况 ──────────────────────────────────────────

    def test_user_content_string_converted_to_list(self):
        """next user 消息 content 为字符串时正确转换为 list."""
        messages = [
            {
                "role": "assistant",
                "content": [
                    {
                        "type": "tool_use",
                        "id": "toolu_str",
                        "name": "Bash",
                        "input": {},
                    },
                ],
            },
            {"role": "user", "content": "hello"},
        ]
        _, fixes = _enforce_pairing(messages)
        assert "orphaned_tool_use_repaired" in fixes
        user_content = messages[1]["content"]
        assert isinstance(user_content, list)
        assert user_content[0] == {"type": "text", "text": "hello"}
        assert user_content[1]["type"] == "tool_result"
        assert user_content[1]["tool_use_id"] == "toolu_str"

    def test_assistant_content_becomes_empty_gets_placeholder(self):
        """assistant 仅含 tool_result 无 tool_use，剥离后插入占位块."""
        messages = [
            {
                "role": "assistant",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "toolu_stray",
                        "content": "x",
                    },
                ],
            },
            {"role": "user", "content": "next"},
        ]
        _, fixes = _enforce_pairing(messages)
        assert "misplaced_tool_result_relocated" in fixes
        # assistant content 不为空（有占位）
        assert len(messages[0]["content"]) == 1
        assert messages[0]["content"][0] == {"type": "text", "text": ""}

    def test_duplicate_tool_result_in_user_not_duplicated(self):
        """user 已有 tool_result 时不重复添加."""
        messages = [
            {
                "role": "assistant",
                "content": [
                    {
                        "type": "tool_use",
                        "id": "toolu_dup",
                        "name": "Bash",
                        "input": {},
                    },
                    {
                        "type": "tool_result",
                        "tool_use_id": "toolu_dup",
                        "content": "misplaced",
                    },
                ],
            },
            {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "toolu_dup",
                        "content": "correct",
                    },
                ],
            },
        ]
        _, fixes = _enforce_pairing(messages)
        # misplaced 被剥离但不重复添加（user 已有）
        result_blocks = [
            b for b in messages[1]["content"] if b.get("type") == "tool_result"
        ]
        assert len(result_blocks) == 1
        assert result_blocks[0]["content"] == "correct"

    def test_idempotent_multiple_runs(self):
        """多次调用结果一致（幂等性）."""
        messages = [
            {
                "role": "assistant",
                "content": [
                    {
                        "type": "tool_use",
                        "id": "toolu_idem",
                        "name": "Bash",
                        "input": {},
                    },
                ],
            },
            {"role": "user", "content": "go"},
        ]
        _enforce_pairing(messages)
        snapshot = copy.deepcopy(messages)
        _, fixes2 = _enforce_pairing(messages)
        # 第二次运行不应产生新的修复
        assert not fixes2
        assert messages == snapshot

    def test_complex_multi_turn_conversation(self):
        """模拟真实 bug 场景：多轮 tool_use，部分有 misplaced，部分纯孤儿."""
        messages = [
            {"role": "user", "content": "start"},
            {
                "role": "assistant",
                "content": [
                    {
                        "type": "tool_use",
                        "id": "toolu_normalized_1",
                        "name": "Bash",
                        "input": {},
                    },
                    {
                        "type": "tool_use",
                        "id": "toolu_normalized_2",
                        "name": "Read",
                        "input": {},
                    },
                ],
            },
            {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "toolu_normalized_1",
                        "content": "ok1",
                    },
                    {
                        "type": "tool_result",
                        "tool_use_id": "toolu_normalized_2",
                        "content": "ok2",
                    },
                ],
            },
            {
                "role": "assistant",
                "content": [
                    {"type": "text", "text": "Now running more tools..."},
                    {
                        "type": "tool_use",
                        "id": "toolu_normalized_5",
                        "name": "Bash",
                        "input": {},
                    },
                    {
                        "type": "tool_use",
                        "id": "toolu_normalized_6",
                        "name": "Read",
                        "input": {},
                    },
                    {
                        "type": "tool_use",
                        "id": "toolu_normalized_7",
                        "name": "Write",
                        "input": {},
                    },
                    {
                        "type": "tool_result",
                        "tool_use_id": "toolu_normalized_5",
                        "content": "zhipu inline result",
                    },
                ],
            },
            {"role": "user", "content": "continue"},
        ]
        _, fixes = _enforce_pairing(messages)
        # 第一个 assistant（索引 1）已正确配对，不受影响
        assert len(messages[1]["content"]) == 2
        assert messages[2]["content"][0]["tool_use_id"] == "toolu_normalized_1"
        # 第二个 assistant（索引 3）: misplaced 被剥离，孤儿被合成
        assert "misplaced_tool_result_relocated" in fixes
        assert "orphaned_tool_use_repaired" in fixes
        assistant_content = messages[3]["content"]
        assert all(b["type"] != "tool_result" for b in assistant_content)
        assert assistant_content[0] == {
            "type": "text",
            "text": "Now running more tools...",
        }
        # user 消息（索引 4）包含所有 3 个 tool_result
        user_content = messages[4]["content"]
        result_ids = {
            b["tool_use_id"]
            for b in user_content
            if isinstance(b, dict) and b.get("type") == "tool_result"
        }
        assert result_ids == {
            "toolu_normalized_5",
            "toolu_normalized_6",
            "toolu_normalized_7",
        }

    def test_next_message_is_assistant_inserts_user(self):
        """下一条消息不是 user 而是 assistant 时，插入合成 user 消息."""
        messages = [
            {
                "role": "assistant",
                "content": [
                    {
                        "type": "tool_use",
                        "id": "toolu_gap",
                        "name": "Bash",
                        "input": {},
                    },
                ],
            },
            {
                "role": "assistant",
                "content": [{"type": "text", "text": "follow up"}],
            },
        ]
        _, fixes = _enforce_pairing(messages)
        assert "orphaned_tool_use_repaired" in fixes
        assert len(messages) == 3
        assert messages[0]["content"][0]["type"] == "tool_use"
        assert messages[1]["role"] == "user"
        assert messages[1]["content"][0]["type"] == "tool_result"
        assert messages[2]["role"] == "assistant"
