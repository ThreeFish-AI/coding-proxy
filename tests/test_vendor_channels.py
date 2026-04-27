"""供应商跨供应商转换通道单元测试.

覆盖 :mod:`coding.proxy.convert.vendor_channels` 的转换通道函数和辅助函数:
- zhipu → anthropic 转换 (prepare_zhipu_to_anthropic)
- zhipu → copilot 转换 (prepare_zhipu_to_copilot)
- copilot → zhipu 转换 (prepare_copilot_to_zhipu)
- zhipu → zhipu 自清理 (prepare_zhipu_self_cleanup)
- anthropic → zhipu 转换 (prepare_anthropic_to_zhipu)
- 共享辅助函数 (strip_thinking_blocks, _strip_cache_control, _remove_vendor_blocks,
  _rewrite_srvtoolu_ids, enforce_anthropic_tool_pairing, infer_source_vendor_from_body)
- 转换注册表 (VENDOR_TRANSITIONS, get_transition_channel)
"""

from __future__ import annotations

import copy

from coding.proxy.convert.vendor_channels import (
    VENDOR_TRANSITIONS,
    _enforce_pairing_sanity_pass,
    _remove_vendor_blocks,
    _rewrite_srvtoolu_ids,
    _strip_cache_control,
    enforce_anthropic_tool_pairing,
    get_transition_channel,
    infer_source_vendor_from_body,
    prepare_anthropic_to_zhipu,
    prepare_copilot_to_zhipu,
    prepare_zhipu_self_cleanup,
    prepare_zhipu_to_anthropic,
    prepare_zhipu_to_copilot,
    strip_thinking_blocks,
)

# ── 辅助函数测试 ──────────────────────────────────────────────


class TestStripThinkingBlocks:
    """strip_thinking_blocks 单元测试."""

    def test_strips_thinking_blocks(self):
        body = {
            "messages": [
                {
                    "role": "assistant",
                    "content": [
                        {"type": "thinking", "thinking": "thought", "signature": "sig"},
                        {"type": "text", "text": "response"},
                    ],
                },
            ]
        }
        stripped = strip_thinking_blocks(body)
        assert stripped == 1
        assert body["messages"][0]["content"] == [
            {"type": "text", "text": "response"},
        ]

    def test_strips_redacted_thinking_blocks(self):
        body = {
            "messages": [
                {
                    "role": "assistant",
                    "content": [
                        {"type": "redacted_thinking", "data": "redacted"},
                    ],
                },
            ]
        }
        stripped = strip_thinking_blocks(body)
        assert stripped == 1
        # content 为空时插入占位 text block
        assert body["messages"][0]["content"] == [
            {"type": "text", "text": "[thinking]"},
        ]

    def test_inserts_placeholder_when_all_thinking(self):
        body = {
            "messages": [
                {
                    "role": "user",
                    "content": "hi",
                },
                {
                    "role": "assistant",
                    "content": [
                        {"type": "thinking", "thinking": "t1"},
                        {"type": "redacted_thinking", "data": "r1"},
                    ],
                },
            ]
        }
        stripped = strip_thinking_blocks(body)
        assert stripped == 2
        assert body["messages"][1]["content"] == [
            {"type": "text", "text": "[thinking]"},
        ]

    def test_no_change_when_no_thinking(self):
        body = {
            "messages": [
                {
                    "role": "assistant",
                    "content": [{"type": "text", "text": "hello"}],
                },
            ]
        }
        stripped = strip_thinking_blocks(body)
        assert stripped == 0
        assert body["messages"][0]["content"] == [{"type": "text", "text": "hello"}]

    def test_skips_non_assistant_messages(self):
        body = {
            "messages": [
                {"role": "user", "content": [{"type": "thinking", "thinking": "t"}]},
            ]
        }
        stripped = strip_thinking_blocks(body)
        assert stripped == 0

    def test_handles_string_content(self):
        body = {
            "messages": [
                {"role": "assistant", "content": "plain text"},
            ]
        }
        stripped = strip_thinking_blocks(body)
        assert stripped == 0


class TestStripCacheControl:
    """_strip_cache_control 单元测试."""

    def test_removes_cache_control_from_system(self):
        body = {
            "system": [
                {
                    "type": "text",
                    "text": "prompt",
                    "cache_control": {"type": "ephemeral"},
                },
            ],
            "messages": [],
        }
        removed = _strip_cache_control(body)
        assert removed == 1
        assert "cache_control" not in body["system"][0]

    def test_removes_cache_control_from_messages(self):
        body = {
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "text",
                            "text": "hi",
                            "cache_control": {"type": "ephemeral"},
                        },
                    ],
                },
            ],
        }
        removed = _strip_cache_control(body)
        assert removed == 1
        assert "cache_control" not in body["messages"][0]["content"][0]

    def test_removes_cache_control_from_tools(self):
        body = {
            "messages": [],
            "tools": [
                {
                    "name": "bash",
                    "input_schema": {},
                    "cache_control": {"type": "ephemeral"},
                },
            ],
        }
        removed = _strip_cache_control(body)
        assert removed == 1
        assert "cache_control" not in body["tools"][0]

    def test_removes_from_all_locations(self):
        body = {
            "system": [
                {"type": "text", "text": "sys", "cache_control": {"type": "ephemeral"}},
            ],
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "text",
                            "text": "msg",
                            "cache_control": {"type": "ephemeral"},
                        },
                    ],
                },
            ],
            "tools": [
                {
                    "name": "bash",
                    "input_schema": {},
                    "cache_control": {"type": "ephemeral"},
                },
            ],
        }
        removed = _strip_cache_control(body)
        assert removed == 3

    def test_no_change_when_no_cache_control(self):
        body = {
            "system": [{"type": "text", "text": "sys"}],
            "messages": [{"role": "user", "content": "hi"}],
            "tools": [{"name": "bash", "input_schema": {}}],
        }
        removed = _strip_cache_control(body)
        assert removed == 0


# ── copilot → zhipu 转换通道测试 ────────────────────────────────


class TestCopilotToZhipuChannel:
    """prepare_copilot_to_zhipu 转换通道单元测试."""

    def test_strips_thinking_blocks(self):
        body = {
            "messages": [
                {
                    "role": "assistant",
                    "content": [
                        {"type": "thinking", "thinking": "thought", "signature": "sig"},
                        {"type": "text", "text": "response"},
                    ],
                },
            ],
        }
        prepared, adaptations = prepare_copilot_to_zhipu(body)
        assert any("thinking_blocks" in a for a in adaptations)
        assert prepared["messages"][0]["content"] == [
            {"type": "text", "text": "response"},
        ]
        # 原始 body 未被修改
        assert body["messages"][0]["content"][0]["type"] == "thinking"

    def test_removes_cache_control(self):
        body = {
            "system": [
                {"type": "text", "text": "sys", "cache_control": {"type": "ephemeral"}},
            ],
            "messages": [],
        }
        prepared, adaptations = prepare_copilot_to_zhipu(body)
        assert any("cache_control" in a for a in adaptations)
        assert "cache_control" not in prepared["system"][0]

    def test_removes_thinking_params(self):
        body = {
            "messages": [],
            "thinking": {"type": "enabled", "budget_tokens": 10000},
            "extended_thinking": {"type": "enabled"},
        }
        prepared, adaptations = prepare_copilot_to_zhipu(body)
        assert "thinking" not in prepared
        assert "extended_thinking" not in prepared
        assert "removed_thinking_param" in adaptations
        assert "removed_extended_thinking_param" in adaptations

    def test_enforces_tool_pairing(self):
        body = {
            "messages": [
                {
                    "role": "assistant",
                    "content": [
                        {
                            "type": "tool_use",
                            "id": "toolu_1",
                            "name": "bash",
                            "input": {},
                        },
                    ],
                },
                {
                    "role": "user",
                    "content": "next turn",
                },
            ],
        }
        prepared, adaptations = prepare_copilot_to_zhipu(body)
        user_content = prepared["messages"][1]["content"]
        tool_results = [
            b
            for b in user_content
            if isinstance(b, dict) and b.get("type") == "tool_result"
        ]
        assert len(tool_results) == 1
        assert tool_results[0]["tool_use_id"] == "toolu_1"

    def test_combined_transformations(self):
        body = {
            "system": [
                {"type": "text", "text": "sys", "cache_control": {"type": "ephemeral"}},
            ],
            "messages": [
                {
                    "role": "assistant",
                    "content": [
                        {"type": "thinking", "thinking": "thought", "signature": "sig"},
                        {
                            "type": "tool_use",
                            "id": "toolu_1",
                            "name": "bash",
                            "input": {},
                        },
                    ],
                },
                {
                    "role": "user",
                    "content": "ok",
                },
            ],
            "thinking": {"type": "enabled", "budget_tokens": 10000},
        }
        prepared, adaptations = prepare_copilot_to_zhipu(body)
        assert all(
            b.get("type") not in ("thinking", "redacted_thinking")
            for b in prepared["messages"][0]["content"]
        )
        assert "cache_control" not in prepared["system"][0]
        assert "thinking" not in prepared
        user_content = prepared["messages"][1]["content"]
        tool_results = [
            b
            for b in user_content
            if isinstance(b, dict) and b.get("type") == "tool_result"
        ]
        assert len(tool_results) == 1

    def test_preserves_original_body(self):
        body = {
            "messages": [
                {
                    "role": "assistant",
                    "content": [
                        {"type": "thinking", "thinking": "t"},
                        {"type": "text", "text": "hi"},
                    ],
                },
            ],
            "thinking": {"type": "enabled"},
        }
        original = copy.deepcopy(body)
        prepare_copilot_to_zhipu(body)
        assert body == original

    def test_noop_when_clean(self):
        body = {
            "messages": [
                {"role": "user", "content": "hello"},
                {"role": "assistant", "content": [{"type": "text", "text": "hi"}]},
            ],
        }
        prepared, adaptations = prepare_copilot_to_zhipu(body)
        assert adaptations == []
        assert prepared == body

    def test_idempotency(self):
        body = {
            "messages": [
                {
                    "role": "assistant",
                    "content": [
                        {"type": "thinking", "thinking": "t"},
                        {
                            "type": "tool_use",
                            "id": "toolu_1",
                            "name": "bash",
                            "input": {},
                        },
                    ],
                },
                {
                    "role": "user",
                    "content": "ok",
                },
            ],
            "thinking": {"type": "enabled"},
        }
        prepared1, adaptations1 = prepare_copilot_to_zhipu(body)
        prepared2, adaptations2 = prepare_copilot_to_zhipu(prepared1)
        assert prepared2 == prepared1
        assert adaptations2 == []


# ── zhipu → anthropic 转换通道测试 ────────────────────────────────


class TestZhipuToAnthropicChannel:
    """prepare_zhipu_to_anthropic 转换通道单元测试."""

    def test_enforces_tool_pairing(self):
        body = {
            "messages": [
                {
                    "role": "assistant",
                    "content": [
                        {
                            "type": "tool_use",
                            "id": "toolu_1",
                            "name": "bash",
                            "input": {},
                        },
                    ],
                },
                {
                    "role": "user",
                    "content": "next turn",
                },
            ],
        }
        prepared, adaptations = prepare_zhipu_to_anthropic(body)
        user_content = prepared["messages"][1]["content"]
        tool_results = [
            b
            for b in user_content
            if isinstance(b, dict) and b.get("type") == "tool_result"
        ]
        assert len(tool_results) == 1
        assert tool_results[0]["tool_use_id"] == "toolu_1"

    def test_strips_thinking_blocks(self):
        body = {
            "messages": [
                {
                    "role": "assistant",
                    "content": [
                        {"type": "thinking", "thinking": "thought", "signature": "sig"},
                        {"type": "text", "text": "response"},
                    ],
                },
            ],
        }
        prepared, adaptations = prepare_zhipu_to_anthropic(body)
        assert any("thinking_blocks" in a for a in adaptations)
        assert prepared["messages"][0]["content"] == [
            {"type": "text", "text": "response"}
        ]

    def test_combined_tool_pairing_and_thinking_strip(self):
        body = {
            "messages": [
                {
                    "role": "assistant",
                    "content": [
                        {"type": "thinking", "thinking": "t", "signature": "s"},
                        {
                            "type": "tool_use",
                            "id": "toolu_1",
                            "name": "bash",
                            "input": {},
                        },
                    ],
                },
                {
                    "role": "user",
                    "content": "ok",
                },
            ],
        }
        prepared, adaptations = prepare_zhipu_to_anthropic(body)
        assert all(
            b.get("type") not in ("thinking", "redacted_thinking")
            for b in prepared["messages"][0]["content"]
        )
        user_content = prepared["messages"][1]["content"]
        tool_results = [
            b
            for b in user_content
            if isinstance(b, dict) and b.get("type") == "tool_result"
        ]
        assert len(tool_results) == 1

    def test_preserves_original_body(self):
        body = {
            "messages": [
                {
                    "role": "assistant",
                    "content": [
                        {"type": "thinking", "thinking": "t"},
                        {"type": "text", "text": "hi"},
                    ],
                },
            ],
        }
        original = copy.deepcopy(body)
        prepare_zhipu_to_anthropic(body)
        assert body == original

    def test_noop_when_clean(self):
        body = {
            "messages": [
                {"role": "user", "content": "hello"},
                {"role": "assistant", "content": [{"type": "text", "text": "hi"}]},
            ],
        }
        prepared, adaptations = prepare_zhipu_to_anthropic(body)
        assert adaptations == []
        assert prepared == body

    def test_idempotency(self):
        body = {
            "messages": [
                {
                    "role": "assistant",
                    "content": [
                        {"type": "thinking", "thinking": "t"},
                        {
                            "type": "tool_use",
                            "id": "toolu_1",
                            "name": "bash",
                            "input": {},
                        },
                    ],
                },
                {
                    "role": "user",
                    "content": "ok",
                },
            ],
        }
        prepared1, _ = prepare_zhipu_to_anthropic(body)
        prepared2, adaptations2 = prepare_zhipu_to_anthropic(prepared1)
        assert prepared2 == prepared1
        assert adaptations2 == []

    def test_preserves_thinking_param(self):
        """zhipu → anthropic 通道不移除顶层 thinking 参数（Anthropic API 支持）."""
        body = {
            "messages": [],
            "thinking": {"type": "enabled", "budget_tokens": 5000},
        }
        prepared, _ = prepare_zhipu_to_anthropic(body)
        assert "thinking" in prepared


# ── zhipu → copilot 转换通道测试 ──────────────────────────────


class TestZhipuToCopilotChannel:
    """prepare_zhipu_to_copilot 转换通道单元测试."""

    def test_strips_thinking_blocks(self):
        body = {
            "messages": [
                {
                    "role": "assistant",
                    "content": [
                        {"type": "thinking", "thinking": "thought", "signature": "sig"},
                        {"type": "text", "text": "response"},
                    ],
                },
            ],
        }
        prepared, adaptations = prepare_zhipu_to_copilot(body)
        assert any("thinking_blocks" in a for a in adaptations)
        assert prepared["messages"][0]["content"] == [
            {"type": "text", "text": "response"},
        ]

    def test_removes_cache_control(self):
        body = {
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "text",
                            "text": "hi",
                            "cache_control": {"type": "ephemeral"},
                        },
                    ],
                },
            ],
        }
        prepared, adaptations = prepare_zhipu_to_copilot(body)
        assert any("cache_control" in a for a in adaptations)
        assert "cache_control" not in prepared["messages"][0]["content"][0]

    def test_preserves_thinking_param(self):
        """zhipu → copilot 通道不移除顶层 thinking 参数（由 converter 自行映射）."""
        body = {
            "messages": [],
            "thinking": {"type": "enabled", "budget_tokens": 10000},
        }
        prepared, adaptations = prepare_zhipu_to_copilot(body)
        assert "thinking" in prepared
        assert "removed_thinking_param" not in adaptations

    def test_enforces_tool_pairing(self):
        body = {
            "messages": [
                {
                    "role": "assistant",
                    "content": [
                        {
                            "type": "tool_use",
                            "id": "toolu_1",
                            "name": "read",
                            "input": {},
                        },
                    ],
                },
                {
                    "role": "user",
                    "content": "next",
                },
            ],
        }
        prepared, adaptations = prepare_zhipu_to_copilot(body)
        user_content = prepared["messages"][1]["content"]
        tool_results = [
            b
            for b in user_content
            if isinstance(b, dict) and b.get("type") == "tool_result"
        ]
        assert len(tool_results) == 1

    def test_preserves_original_body(self):
        body = {
            "messages": [
                {
                    "role": "assistant",
                    "content": [
                        {"type": "thinking", "thinking": "t"},
                        {"type": "text", "text": "hi"},
                    ],
                },
            ],
        }
        original = copy.deepcopy(body)
        prepare_zhipu_to_copilot(body)
        assert body == original

    def test_noop_when_clean(self):
        body = {
            "messages": [
                {"role": "user", "content": "hello"},
                {"role": "assistant", "content": [{"type": "text", "text": "hi"}]},
            ],
        }
        prepared, adaptations = prepare_zhipu_to_copilot(body)
        assert adaptations == []
        assert prepared == body

    def test_idempotency(self):
        body = {
            "messages": [
                {
                    "role": "assistant",
                    "content": [
                        {"type": "thinking", "thinking": "t"},
                        {
                            "type": "tool_use",
                            "id": "toolu_1",
                            "name": "bash",
                            "input": {},
                        },
                    ],
                },
                {
                    "role": "user",
                    "content": "ok",
                },
            ],
        }
        prepared1, _ = prepare_zhipu_to_copilot(body)
        prepared2, adaptations2 = prepare_zhipu_to_copilot(prepared1)
        assert prepared2 == prepared1
        assert adaptations2 == []

    def test_strips_redacted_thinking(self):
        body = {
            "messages": [
                {
                    "role": "assistant",
                    "content": [
                        {"type": "redacted_thinking", "data": "redacted"},
                        {"type": "text", "text": "response"},
                    ],
                },
            ],
        }
        prepared, adaptations = prepare_zhipu_to_copilot(body)
        assert any("thinking_blocks" in a for a in adaptations)
        assert prepared["messages"][0]["content"] == [
            {"type": "text", "text": "response"},
        ]


# ── zhipu → zhipu 自清理通道测试 ─────────────────────────────────


class TestZhipuSelfCleanupChannel:
    """prepare_zhipu_self_cleanup 单元测试.

    自清理通道的核心契约: **仅** 修复 zhipu 自身拒绝的产物
    (server_tool_use_delta, 错位 tool_result), 保留所有 zhipu 原生支持
    的特性 (srvtoolu_* ID, thinking signature, cache_control, 顶层 thinking).
    """

    def test_strips_server_tool_use_delta(self):
        body = {
            "messages": [
                {
                    "role": "assistant",
                    "content": [
                        {"type": "text", "text": "thinking..."},
                        {"type": "server_tool_use_delta", "partial_json": "{}"},
                    ],
                },
            ],
        }
        prepared, adaptations = prepare_zhipu_self_cleanup(body)
        content = prepared["messages"][0]["content"]
        assert all(b.get("type") != "server_tool_use_delta" for b in content)
        assert any("zhipu_vendor_blocks" in a for a in adaptations)

    def test_relocates_misplaced_tool_result(self):
        """assistant 内联 tool_result 应被搬迁到下一个 user 消息."""
        body = {
            "messages": [
                {
                    "role": "assistant",
                    "content": [
                        {
                            "type": "tool_use",
                            "id": "srvtoolu_a",
                            "name": "bash",
                            "input": {},
                        },
                        {
                            "type": "tool_result",
                            "tool_use_id": "srvtoolu_a",
                            "content": "ok",
                        },
                    ],
                },
                {"role": "user", "content": []},
            ],
        }
        prepared, adaptations = prepare_zhipu_self_cleanup(body)

        # assistant 消息中应不再包含 tool_result
        assistant_content = prepared["messages"][0]["content"]
        assert all(b.get("type") != "tool_result" for b in assistant_content)
        # tool_result 已搬到下一个 user 消息
        user_content = prepared["messages"][1]["content"]
        assert any(
            b.get("type") == "tool_result" and b.get("tool_use_id") == "srvtoolu_a"
            for b in user_content
        )
        assert "misplaced_tool_result_relocated" in adaptations

    def test_preserves_srvtoolu_ids(self):
        """zhipu 原生 srvtoolu_* ID 与 server_tool_use 类型必须保留."""
        body = {
            "messages": [
                {
                    "role": "assistant",
                    "content": [
                        {
                            "type": "server_tool_use",
                            "id": "srvtoolu_xyz",
                            "name": "bash",
                            "input": {},
                        },
                    ],
                },
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": "srvtoolu_xyz",
                            "content": "ok",
                        },
                    ],
                },
            ],
        }
        prepared, adaptations = prepare_zhipu_self_cleanup(body)

        block = prepared["messages"][0]["content"][0]
        assert block["id"] == "srvtoolu_xyz"
        assert block["type"] == "server_tool_use"
        # 无任何 srvtoolu 改写或 server_tool_use 类型纠正
        assert not any("srvtoolu_ids" in a for a in adaptations)

    def test_preserves_thinking_blocks(self):
        """zhipu 自签 thinking signature 必须保留."""
        body = {
            "messages": [
                {
                    "role": "assistant",
                    "content": [
                        {
                            "type": "thinking",
                            "thinking": "let me think",
                            "signature": "zhipu_sig_abc",
                        },
                        {"type": "text", "text": "answer"},
                    ],
                },
            ],
        }
        prepared, adaptations = prepare_zhipu_self_cleanup(body)
        content = prepared["messages"][0]["content"]
        assert any(b.get("type") == "thinking" for b in content)
        assert not any("thinking_blocks" in a for a in adaptations)

    def test_preserves_cache_control(self):
        """cache_control 字段必须保留 (GLM 原生支持, 已实证 cache_read)."""
        body = {
            "system": [
                {
                    "type": "text",
                    "text": "system prompt",
                    "cache_control": {"type": "ephemeral"},
                },
            ],
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "text",
                            "text": "hi",
                            "cache_control": {"type": "ephemeral"},
                        },
                    ],
                },
            ],
            "tools": [
                {
                    "name": "bash",
                    "description": "",
                    "input_schema": {"type": "object"},
                    "cache_control": {"type": "ephemeral"},
                },
            ],
        }
        prepared, adaptations = prepare_zhipu_self_cleanup(body)
        assert prepared["system"][0].get("cache_control") == {"type": "ephemeral"}
        assert prepared["messages"][0]["content"][0].get("cache_control") == {
            "type": "ephemeral"
        }
        assert prepared["tools"][0].get("cache_control") == {"type": "ephemeral"}
        assert not any("cache_control" in a for a in adaptations)

    def test_preserves_thinking_param(self):
        """顶层 thinking / extended_thinking 参数必须保留."""
        body = {
            "messages": [],
            "thinking": {"type": "enabled", "budget_tokens": 5000},
            "extended_thinking": {"foo": "bar"},
        }
        prepared, _ = prepare_zhipu_self_cleanup(body)
        assert prepared["thinking"] == {
            "type": "enabled",
            "budget_tokens": 5000,
        }
        assert prepared["extended_thinking"] == {"foo": "bar"}

    def test_idempotency(self):
        """二次调用幂等: 已清洗的 body 不再产生新 adaptations."""
        body = {
            "messages": [
                {
                    "role": "assistant",
                    "content": [
                        {
                            "type": "tool_use",
                            "id": "srvtoolu_a",
                            "name": "bash",
                            "input": {},
                        },
                        {
                            "type": "tool_result",
                            "tool_use_id": "srvtoolu_a",
                            "content": "ok",
                        },
                        {"type": "server_tool_use_delta", "partial_json": "{}"},
                    ],
                },
            ],
        }
        first_pass, first_adapt = prepare_zhipu_self_cleanup(body)
        assert first_adapt  # 首次调用应产生变换
        _, second_adapt = prepare_zhipu_self_cleanup(first_pass)
        assert second_adapt == []

    def test_noop_when_clean(self):
        """纯净 body (无 zhipu 产物) 应不产生任何 adaptations."""
        body = {
            "messages": [
                {"role": "user", "content": "hello"},
                {
                    "role": "assistant",
                    "content": [{"type": "text", "text": "hi"}],
                },
            ],
        }
        original = copy.deepcopy(body)
        prepared, adaptations = prepare_zhipu_self_cleanup(body)
        assert adaptations == []
        assert prepared == original

    def test_does_not_mutate_input(self):
        """通道返回深拷贝, 输入 body 必须保持原状."""
        body = {
            "messages": [
                {
                    "role": "assistant",
                    "content": [
                        {"type": "server_tool_use_delta", "partial_json": "{}"},
                    ],
                },
            ],
        }
        original = copy.deepcopy(body)
        prepare_zhipu_self_cleanup(body)
        assert body == original

    def test_combined_artifacts(self):
        """端到端: server_tool_use_delta 被剥, server_tool_use 保留, 错位 tool_result 搬迁.

        典型场景: Claude Code 的客户端工具 (Bash/Read 等) 以 ``tool_use`` 形式
        emit, 其错位的 ``tool_result`` 应被重定位; zhipu 原生 ``server_tool_use``
        块不需要客户端 tool_result, 仅需保留原状.
        """
        body = {
            "messages": [
                {
                    "role": "assistant",
                    "content": [
                        {"type": "server_tool_use_delta", "partial_json": "{}"},
                        {
                            "type": "server_tool_use",
                            "id": "srvtoolu_native",
                            "name": "web_search",
                            "input": {},
                        },
                        {
                            "type": "tool_use",
                            "id": "toolu_bash_001",
                            "name": "bash",
                            "input": {"command": "ls"},
                        },
                        {
                            "type": "tool_result",
                            "tool_use_id": "toolu_bash_001",
                            "content": "ok",
                        },
                    ],
                },
            ],
        }
        prepared, adaptations = prepare_zhipu_self_cleanup(body)

        assistant_content = prepared["messages"][0]["content"]
        # delta 被剥离
        assert all(b.get("type") != "server_tool_use_delta" for b in assistant_content)
        # 错位 tool_result 被搬出 assistant
        assert all(b.get("type") != "tool_result" for b in assistant_content)
        # server_tool_use 与其 srvtoolu_* ID 完整保留
        srv_block = next(
            b for b in assistant_content if b.get("type") == "server_tool_use"
        )
        assert srv_block["id"] == "srvtoolu_native"
        # tool_use ID 同样保留
        tool_use_block = next(
            b for b in assistant_content if b.get("type") == "tool_use"
        )
        assert tool_use_block["id"] == "toolu_bash_001"
        # 后续 user 消息已被插入并包含 tool_result
        assert prepared["messages"][1]["role"] == "user"
        assert any(
            b.get("type") == "tool_result" and b.get("tool_use_id") == "toolu_bash_001"
            for b in prepared["messages"][1]["content"]
        )
        # 关键 adaptation 标签均出现
        assert any("zhipu_vendor_blocks" in a for a in adaptations)
        assert "misplaced_tool_result_relocated" in adaptations


# ── 转换注册表测试 ────────────────────────────────────────────


class TestTransitionRegistry:
    """VENDOR_TRANSITIONS / get_transition_channel 单元测试."""

    def test_all_transitions_registered(self):
        assert ("zhipu", "anthropic") in VENDOR_TRANSITIONS
        assert ("zhipu", "copilot") in VENDOR_TRANSITIONS
        assert ("copilot", "zhipu") in VENDOR_TRANSITIONS
        assert ("zhipu", "zhipu") in VENDOR_TRANSITIONS
        assert ("anthropic", "zhipu") in VENDOR_TRANSITIONS
        assert len(VENDOR_TRANSITIONS) == 5

    def test_get_transition_channel_returns_function(self):
        assert (
            get_transition_channel("zhipu", "anthropic") is prepare_zhipu_to_anthropic
        )
        assert get_transition_channel("zhipu", "copilot") is prepare_zhipu_to_copilot
        assert get_transition_channel("copilot", "zhipu") is prepare_copilot_to_zhipu
        assert get_transition_channel("zhipu", "zhipu") is prepare_zhipu_self_cleanup
        assert (
            get_transition_channel("anthropic", "zhipu") is prepare_anthropic_to_zhipu
        )

    def test_get_transition_channel_returns_none_for_unregistered(self):
        assert get_transition_channel("copilot", "anthropic") is None
        assert get_transition_channel("unknown", "target") is None
        assert get_transition_channel("antigravity", "copilot") is None
        # 未注册的同 vendor 自转换仍返回 None
        assert get_transition_channel("anthropic", "anthropic") is None
        assert get_transition_channel("copilot", "copilot") is None

    def test_transition_functions_share_signature(self):
        body = {"messages": []}
        for key, fn in VENDOR_TRANSITIONS.items():
            result = fn(body)
            assert isinstance(result, tuple) and len(result) == 2
            assert isinstance(result[0], dict)
            assert isinstance(result[1], list)


# ── 转换通道差异测试 ──────────────────────────────────────────


class TestTransitionDifferences:
    """验证不同转换通道的关键行为差异."""

    def test_copilot_to_zhipu_removes_thinking_param_zhipu_to_copilot_preserves(self):
        body = {
            "messages": [],
            "thinking": {"type": "enabled", "budget_tokens": 5000},
        }
        copilot_to_zhipu_result, copilot_to_zhipu_adapt = prepare_copilot_to_zhipu(body)
        zhipu_to_copilot_result, zhipu_to_copilot_adapt = prepare_zhipu_to_copilot(body)

        assert "thinking" not in copilot_to_zhipu_result
        assert "removed_thinking_param" in copilot_to_zhipu_adapt

        assert "thinking" in zhipu_to_copilot_result
        assert "removed_thinking_param" not in zhipu_to_copilot_adapt

    def test_cross_vendor_transitions_strip_thinking_blocks(self):
        """跨 vendor 通道一律剥离 thinking blocks（自清理通道刻意保留，故排除）."""
        body = {
            "messages": [
                {
                    "role": "assistant",
                    "content": [
                        {"type": "thinking", "thinking": "t"},
                        {"type": "text", "text": "hi"},
                    ],
                },
            ],
        }
        for key, fn in VENDOR_TRANSITIONS.items():
            if key[0] == key[1]:
                # 自转换通道（如 zhipu→zhipu）保留 thinking signature，跳过
                continue
            result, adaptations = fn(body)
            assert result["messages"][0]["content"] == [
                {"type": "text", "text": "hi"}
            ], f"Transition {key} failed to strip thinking blocks"


# ── _remove_vendor_blocks 单元测试 ────────────────────────────────


class TestRemoveVendorBlocks:
    """_remove_vendor_blocks 就地剥离指定 type 内容块."""

    def test_removes_single_type(self):
        body = {
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
        removed = _remove_vendor_blocks(body, {"server_tool_use_delta"})
        assert removed == 1
        assert [b["type"] for b in body["messages"][0]["content"]] == ["text", "text"]

    def test_removes_multiple_types(self):
        body = {
            "messages": [
                {
                    "role": "assistant",
                    "content": [
                        {"type": "text", "text": "keep"},
                        {"type": "foo", "data": "drop"},
                        {"type": "bar", "data": "drop"},
                    ],
                },
            ],
        }
        removed = _remove_vendor_blocks(body, {"foo", "bar"})
        assert removed == 2
        assert body["messages"][0]["content"] == [{"type": "text", "text": "keep"}]

    def test_noop_when_no_match(self):
        body = {
            "messages": [
                {
                    "role": "assistant",
                    "content": [{"type": "text", "text": "clean"}],
                },
            ],
        }
        removed = _remove_vendor_blocks(body, {"server_tool_use_delta"})
        assert removed == 0
        assert body["messages"][0]["content"] == [{"type": "text", "text": "clean"}]

    def test_handles_string_content(self):
        body = {"messages": [{"role": "user", "content": "hi"}]}
        removed = _remove_vendor_blocks(body, {"whatever"})
        assert removed == 0

    def test_inserts_placeholder_when_all_blocks_stripped(self):
        """assistant 消息仅含 vendor 块时插入占位 text block."""
        body = {
            "messages": [
                {
                    "role": "assistant",
                    "content": [
                        {
                            "type": "server_tool_use",
                            "id": "toolu_1",
                            "name": "ws",
                            "input": {},
                        },
                    ],
                },
            ],
        }
        removed = _remove_vendor_blocks(body, {"server_tool_use"})
        assert removed == 1
        assert body["messages"][0]["content"] == [
            {"type": "text", "text": "[vendor_block_removed]"},
        ]

    def test_does_not_mutate_unrelated_messages(self):
        """仅含 vendor 块的消息被修改，其他消息不受影响."""
        body = {
            "messages": [
                {"role": "user", "content": [{"type": "text", "text": "hi"}]},
                {
                    "role": "assistant",
                    "content": [
                        {"type": "server_tool_use_delta", "partial_json": "{}"},
                    ],
                },
            ],
        }
        _remove_vendor_blocks(body, {"server_tool_use_delta"})
        assert body["messages"][0]["content"] == [{"type": "text", "text": "hi"}]
        assert body["messages"][1]["content"] == [
            {"type": "text", "text": "[vendor_block_removed]"},
        ]


# ── _rewrite_srvtoolu_ids 单元测试 ─────────────────────────────────


class TestRewriteSrvtooluIds:
    """_rewrite_srvtoolu_ids 将 srvtoolu_* ID 与 server_tool_use 类型标准化."""

    def test_rewrites_server_tool_use_and_result_pair(self):
        body = {
            "messages": [
                {
                    "role": "assistant",
                    "content": [
                        {
                            "type": "server_tool_use",
                            "id": "srvtoolu_xyz",
                            "name": "bash",
                            "input": {"cmd": "ls"},
                        },
                    ],
                },
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": "srvtoolu_xyz",
                            "content": "ok",
                        },
                    ],
                },
            ],
        }
        count, id_map = _rewrite_srvtoolu_ids(body)
        assert count == 1
        assistant_block = body["messages"][0]["content"][0]
        user_block = body["messages"][1]["content"][0]
        assert assistant_block["type"] == "tool_use"
        assert assistant_block["id"].startswith("toolu_normalized_")
        assert user_block["tool_use_id"] == assistant_block["id"]
        assert id_map == {"srvtoolu_xyz": assistant_block["id"]}

    def test_rewrites_non_standard_tool_use_id_with_name(self):
        """非标准 ID（非 toolu_ / srvtoolu_）且具备 name → 改写为 toolu_normalized_*."""
        body = {
            "messages": [
                {
                    "role": "assistant",
                    "content": [
                        {
                            "type": "tool_use",
                            "id": "custom_bad_id",
                            "name": "bash",
                            "input": {},
                        },
                    ],
                },
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": "custom_bad_id",
                            "content": "ok",
                        },
                    ],
                },
            ],
        }
        count, id_map = _rewrite_srvtoolu_ids(body)
        assert count == 1
        new_id = body["messages"][0]["content"][0]["id"]
        assert new_id.startswith("toolu_normalized_")
        assert body["messages"][1]["content"][0]["tool_use_id"] == new_id

    def test_preserves_standard_tool_use_id(self):
        body = {
            "messages": [
                {
                    "role": "assistant",
                    "content": [
                        {
                            "type": "tool_use",
                            "id": "toolu_abc",
                            "name": "bash",
                            "input": {},
                        },
                    ],
                },
            ],
        }
        count, id_map = _rewrite_srvtoolu_ids(body)
        assert count == 0
        assert body["messages"][0]["content"][0]["id"] == "toolu_abc"

    def test_corrects_server_tool_use_type_with_standard_id(self):
        """type 为 server_tool_use 但 ID 已是 toolu_* 时仅纠正 type."""
        body = {
            "messages": [
                {
                    "role": "assistant",
                    "content": [
                        {
                            "type": "server_tool_use",
                            "id": "toolu_okay",
                            "name": "bash",
                            "input": {},
                        },
                    ],
                },
            ],
        }
        count, _ = _rewrite_srvtoolu_ids(body)
        # 既不是 srvtoolu_*，ID 也合法 → 不计入 count，但 type 应被校正
        assert count == 0
        assert body["messages"][0]["content"][0]["type"] == "tool_use"

    def test_rewrites_multiple_pairs_with_unique_ids(self):
        body = {
            "messages": [
                {
                    "role": "assistant",
                    "content": [
                        {
                            "type": "server_tool_use",
                            "id": "srvtoolu_a",
                            "name": "bash",
                            "input": {},
                        },
                        {
                            "type": "server_tool_use",
                            "id": "srvtoolu_b",
                            "name": "read",
                            "input": {},
                        },
                    ],
                },
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": "srvtoolu_a",
                            "content": "a",
                        },
                        {
                            "type": "tool_result",
                            "tool_use_id": "srvtoolu_b",
                            "content": "b",
                        },
                    ],
                },
            ],
        }
        count, id_map = _rewrite_srvtoolu_ids(body)
        assert count == 2
        assert len(set(id_map.values())) == 2
        assistant_ids = [b["id"] for b in body["messages"][0]["content"]]
        result_ids = [b["tool_use_id"] for b in body["messages"][1]["content"]]
        assert assistant_ids == result_ids

    def test_skips_non_matching_user_tool_result(self):
        """tool_result.tool_use_id 不在 id_map 时保留原样."""
        body = {
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": "toolu_other",
                            "content": "unrelated",
                        },
                    ],
                },
            ],
        }
        count, _ = _rewrite_srvtoolu_ids(body)
        assert count == 0
        assert body["messages"][0]["content"][0]["tool_use_id"] == "toolu_other"

    def test_rewrites_inline_tool_result_before_tool_use(self):
        """块顺序鲁棒性回归保护: inline tool_result 在 tool_use 之前时仍正确改名.

        GLM-5 偶发将 inline tool_result 输出在本消息 tool_use 之前 (流式断片).
        若 _rewrite 用单遍扫描, 处理 inline tool_result 时 id_map 尚未填入对应
        srvtoolu_* → 漏改名 → enforce 阶段 extracted dict key 与 tool_use_ids
        错位 → dangling tool_use 漏报 → anthropic 报 'tool_use ids without
        tool_result blocks immediately after'.

        修复后采用两遍扫描: 先全量收集 id_map (仅处理 tool_use), 再统一改写
        所有 tool_result.tool_use_id 引用。
        """
        body = {
            "messages": [
                {
                    "role": "assistant",
                    "content": [
                        # inline tool_result 在 server_tool_use 之前!
                        {
                            "type": "tool_result",
                            "tool_use_id": "srvtoolu_X",
                            "content": "inline-X",
                        },
                        {
                            "type": "server_tool_use",
                            "id": "srvtoolu_X",
                            "name": "bash",
                            "input": {},
                        },
                    ],
                },
            ],
        }
        count, id_map = _rewrite_srvtoolu_ids(body)
        assert count == 1
        new_id = id_map["srvtoolu_X"]
        assert new_id.startswith("toolu_normalized_")
        # 关键断言: inline tool_result 也被改名 (即使在 tool_use 之前)
        inline_result = body["messages"][0]["content"][0]
        assert inline_result["type"] == "tool_result"
        assert inline_result["tool_use_id"] == new_id
        # tool_use 也被改名
        tool_use_block = body["messages"][0]["content"][1]
        assert tool_use_block["type"] == "tool_use"
        assert tool_use_block["id"] == new_id

    def test_rewrites_tool_result_in_assistant_role(self):
        """assistant role 内的 tool_result 也应被改名 (Pass 2 全量扫描所有消息)."""
        body = {
            "messages": [
                {
                    "role": "assistant",
                    "content": [
                        {
                            "type": "server_tool_use",
                            "id": "srvtoolu_M",
                            "name": "bash",
                            "input": {},
                        },
                    ],
                },
                {
                    "role": "assistant",  # 异常: 连续 assistant
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": "srvtoolu_M",
                            "content": "M-result",
                        },
                    ],
                },
            ],
        }
        count, id_map = _rewrite_srvtoolu_ids(body)
        new_id = id_map["srvtoolu_M"]
        # 后续 assistant 内的 tool_result 也被改名
        assert body["messages"][1]["content"][0]["tool_use_id"] == new_id


# ── infer_source_vendor_from_body 单元测试 ─────────────────────────


class TestInferSourceVendorFromBody:
    """infer_source_vendor_from_body 内容感知启发式推断."""

    def test_detects_zhipu_by_srvtoolu_id(self):
        body = {
            "messages": [
                {
                    "role": "assistant",
                    "content": [
                        {
                            "type": "tool_use",
                            "id": "srvtoolu_abc",
                            "name": "bash",
                            "input": {},
                        },
                    ],
                },
            ],
        }
        assert infer_source_vendor_from_body(body) == "zhipu"

    def test_detects_zhipu_by_server_tool_use_with_non_standard_id(self):
        """server_tool_use + 非 toolu_/srvtoolu_ ID → 兜底归 zhipu."""
        body = {
            "messages": [
                {
                    "role": "assistant",
                    "content": [
                        {
                            "type": "server_tool_use",
                            "id": "custom_non_standard",
                            "name": "bash",
                            "input": {},
                        },
                    ],
                },
            ],
        }
        assert infer_source_vendor_from_body(body) == "zhipu"

    def test_detects_zhipu_by_server_tool_use_delta(self):
        body = {
            "messages": [
                {
                    "role": "assistant",
                    "content": [
                        {
                            "type": "server_tool_use_delta",
                            "partial_json": "{}",
                        },
                    ],
                },
            ],
        }
        assert infer_source_vendor_from_body(body) == "zhipu"

    def test_detects_zhipu_by_tool_result_tool_use_id(self):
        """tool_result 块中 tool_use_id 为 srvtoolu_* 也可识别."""
        body = {
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": "srvtoolu_x",
                            "content": "",
                        },
                    ],
                },
            ],
        }
        assert infer_source_vendor_from_body(body) == "zhipu"

    def test_returns_none_for_pristine_anthropic_body(self):
        body = {
            "messages": [
                {"role": "user", "content": "hi"},
                {
                    "role": "assistant",
                    "content": [
                        {
                            "type": "tool_use",
                            "id": "toolu_standard",
                            "name": "bash",
                            "input": {},
                        },
                    ],
                },
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": "toolu_standard",
                            "content": "ok",
                        },
                    ],
                },
            ],
        }
        assert infer_source_vendor_from_body(body) is None

    def test_returns_none_for_empty_body(self):
        assert infer_source_vendor_from_body({}) is None
        assert infer_source_vendor_from_body({"messages": []}) is None

    def test_readonly_does_not_mutate_body(self):
        body = {
            "messages": [
                {
                    "role": "assistant",
                    "content": [
                        {
                            "type": "server_tool_use",
                            "id": "srvtoolu_abc",
                            "name": "bash",
                            "input": {},
                        },
                    ],
                },
            ],
        }
        snapshot = copy.deepcopy(body)
        infer_source_vendor_from_body(body)
        assert body == snapshot

    def test_handles_string_content(self):
        body = {
            "messages": [
                {"role": "user", "content": "just text"},
            ],
        }
        assert infer_source_vendor_from_body(body) is None

    def test_detects_anthropic_by_server_tool_use_with_toolu_id(self):
        """server_tool_use + toolu_* ID（Anthropic beta 功能产物）→ anthropic."""
        body = {
            "messages": [
                {
                    "role": "assistant",
                    "content": [
                        {
                            "type": "server_tool_use",
                            "id": "toolu_web_search_1",
                            "name": "web_search",
                            "input": {"query": "test"},
                        },
                    ],
                },
            ],
        }
        assert infer_source_vendor_from_body(body) == "anthropic"

    def test_zhipu_srvtoolu_takes_priority_over_anthropic_detection(self):
        """srvtoolu_* ID 优先识别为 zhipu（即使 block type 为 server_tool_use）."""
        body = {
            "messages": [
                {
                    "role": "assistant",
                    "content": [
                        {
                            "type": "server_tool_use",
                            "id": "srvtoolu_x",
                            "name": "bash",
                            "input": {},
                        },
                    ],
                },
            ],
        }
        assert infer_source_vendor_from_body(body) == "zhipu"


# ── enforce_anthropic_tool_pairing 单元测试（从 test_request_normalizer.py 迁入） ─


def _enforce_pairing(messages):
    """在 messages 上直接执行 enforce_anthropic_tool_pairing，返回 (messages, fixes)."""
    fixes = enforce_anthropic_tool_pairing(messages)
    return messages, fixes


class TestEnforceAnthropicToolPairing:
    """验证 enforce_anthropic_tool_pairing 单遍强制配对函数.

    通过单次正向遍历完成 tool_result 剥离、重定位和孤儿合成。
    """

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
        assert len(messages[0]["content"]) == 1
        assert messages[0]["content"][0]["type"] == "tool_use"
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
        assert len(messages[1]["content"]) == 3
        assert all(b["type"] == "tool_use" for b in messages[1]["content"])
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
        assert len(messages[0]["content"]) == 3
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
        assert len(messages[1]["content"]) == 2
        assert messages[2]["content"][0]["tool_use_id"] == "toolu_normalized_1"
        assert "misplaced_tool_result_relocated" in fixes
        assert "orphaned_tool_use_repaired" in fixes
        assistant_content = messages[3]["content"]
        assert all(b["type"] != "tool_result" for b in assistant_content)
        assert assistant_content[0] == {
            "type": "text",
            "text": "Now running more tools...",
        }
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

    def test_sanity_check_does_not_false_fire_on_correctly_paired_messages(self):
        """正常配对消息走完主循环后, sanity G 段不应误触发.

        主循环 F 步已正确合成/搬迁所有 tool_result 时, sanity 视角下 next_user
        的 nu_result_ids 已覆盖全部 tool_use_ids, 走 ``if uid in nu_result_ids:
        continue`` 分支, 不会重复合成占位、也不应打 ``pairing_sanity_repaired``
        标签 → 验证 sanity 的幂等性 / 不重复合成保证。
        """
        messages = [
            {"role": "user", "content": "task"},
            {
                "role": "assistant",
                "content": [
                    {"type": "tool_use", "id": "toolu_a", "name": "bash", "input": {}},
                ],
            },
            {
                "role": "user",
                "content": [
                    {"type": "tool_result", "tool_use_id": "toolu_a", "content": "ok"},
                ],
            },
        ]
        _, fixes = _enforce_pairing(messages)
        # 一切正常, sanity 不应介入
        assert "pairing_sanity_repaired" not in fixes
        assert "orphaned_tool_use_repaired" not in fixes
        assert "misplaced_tool_result_relocated" not in fixes


class TestEnforcePairingSanityPass:
    """_enforce_pairing_sanity_pass 正向兜底路径单元测试.

    主循环 F 步在当前实现下能覆盖所有 dangling tool_use, 因此 sanity 在公开
    ``enforce_anthropic_tool_pairing`` API 调用中不会被实际触发. 抽出为独立
    helper 后可绕过主循环, 直接对兜底合成路径建立正向回归保护, 防止 G 段
    被未来重构「优化掉」时静默失效。
    """

    def test_synthesizes_is_error_for_dangling_tool_use(self):
        """next_user 缺对应 tool_result 时, sanity 直接合成 is_error 占位."""
        messages = [
            {
                "role": "assistant",
                "content": [
                    {
                        "type": "tool_use",
                        "id": "toolu_dangling",
                        "name": "bash",
                        "input": {},
                    },
                ],
            },
            {"role": "user", "content": []},
        ]
        sanity_synthesized = _enforce_pairing_sanity_pass(messages)

        assert sanity_synthesized == ["toolu_dangling"]
        # next_user 已被注入 is_error 占位
        user_content = messages[1]["content"]
        assert len(user_content) == 1
        placeholder = user_content[0]
        assert placeholder["type"] == "tool_result"
        assert placeholder["tool_use_id"] == "toolu_dangling"
        assert placeholder["is_error"] is True
        assert placeholder["content"] == ""

    def test_inserts_user_message_when_next_is_not_user(self):
        """assistant 后无 user 消息时, sanity 应当插入空 user 再合成占位."""
        messages = [
            {
                "role": "assistant",
                "content": [
                    {"type": "tool_use", "id": "toolu_x", "name": "bash", "input": {}},
                ],
            },
            # 没有后续消息 → sanity 应插入空 user 并合成占位
        ]
        sanity_synthesized = _enforce_pairing_sanity_pass(messages)

        assert sanity_synthesized == ["toolu_x"]
        assert len(messages) == 2
        assert messages[1]["role"] == "user"
        results = messages[1]["content"]
        assert len(results) == 1
        assert results[0]["tool_use_id"] == "toolu_x"
        assert results[0]["is_error"] is True

    def test_inserts_user_message_when_next_is_assistant(self):
        """assistant 后紧跟另一个 assistant (非 user) 时, sanity 应插入空 user."""
        messages = [
            {
                "role": "assistant",
                "content": [
                    {"type": "tool_use", "id": "toolu_a", "name": "bash", "input": {}},
                ],
            },
            {
                "role": "assistant",
                "content": [{"type": "text", "text": "stray"}],
            },
        ]
        sanity_synthesized = _enforce_pairing_sanity_pass(messages)

        assert sanity_synthesized == ["toolu_a"]
        assert messages[1]["role"] == "user"  # 新插入的空 user
        assert messages[2]["role"] == "assistant"  # 原 stray 后移
        assert messages[1]["content"][0]["tool_use_id"] == "toolu_a"

    def test_skips_when_tool_result_already_present(self):
        """next_user 已含对应 tool_result 时不应重复合成."""
        messages = [
            {
                "role": "assistant",
                "content": [
                    {"type": "tool_use", "id": "toolu_a", "name": "bash", "input": {}},
                ],
            },
            {
                "role": "user",
                "content": [
                    {"type": "tool_result", "tool_use_id": "toolu_a", "content": "ok"},
                ],
            },
        ]
        original_user_content = list(messages[1]["content"])
        sanity_synthesized = _enforce_pairing_sanity_pass(messages)

        assert sanity_synthesized == []
        assert messages[1]["content"] == original_user_content  # 未被改动

    def test_handles_string_content_in_next_user(self):
        """next_user.content 是字符串时, sanity 先转为 text 块再合成占位."""
        messages = [
            {
                "role": "assistant",
                "content": [
                    {"type": "tool_use", "id": "toolu_a", "name": "bash", "input": {}},
                ],
            },
            {"role": "user", "content": "free text"},
        ]
        sanity_synthesized = _enforce_pairing_sanity_pass(messages)

        assert sanity_synthesized == ["toolu_a"]
        user_content = messages[1]["content"]
        assert isinstance(user_content, list)
        # 原字符串保留为 text 块, 占位追加在末尾
        assert user_content[0] == {"type": "text", "text": "free text"}
        assert user_content[-1]["type"] == "tool_result"
        assert user_content[-1]["tool_use_id"] == "toolu_a"

    def test_partial_repair_only_synthesizes_missing_uids(self):
        """next_user 已含部分 tool_result 时, sanity 仅为缺失的 uid 合成占位."""
        messages = [
            {
                "role": "assistant",
                "content": [
                    {"type": "tool_use", "id": "toolu_a", "name": "bash", "input": {}},
                    {"type": "tool_use", "id": "toolu_b", "name": "bash", "input": {}},
                ],
            },
            {
                "role": "user",
                "content": [
                    {"type": "tool_result", "tool_use_id": "toolu_a", "content": "ok"},
                    # toolu_b 缺失
                ],
            },
        ]
        sanity_synthesized = _enforce_pairing_sanity_pass(messages)

        assert sanity_synthesized == ["toolu_b"]
        results = messages[1]["content"]
        assert len(results) == 2
        # 原 toolu_a 不变
        assert results[0]["tool_use_id"] == "toolu_a"
        assert results[0].get("is_error") is not True
        # 新合成 toolu_b is_error 占位
        assert results[1]["tool_use_id"] == "toolu_b"
        assert results[1]["is_error"] is True

    def test_skips_assistant_without_tool_use(self):
        """assistant 不含 tool_use 时 sanity 应当短路, 不插入空 user."""
        messages = [
            {
                "role": "assistant",
                "content": [{"type": "text", "text": "just talking"}],
            },
        ]
        sanity_synthesized = _enforce_pairing_sanity_pass(messages)

        assert sanity_synthesized == []
        # 不应插入空 user
        assert len(messages) == 1

    def test_skips_non_assistant_messages(self):
        """非 assistant 消息 (user/system) 不参与 sanity 检查."""
        messages = [
            {"role": "user", "content": "hi"},
            {"role": "system", "content": "rules"},
        ]
        sanity_synthesized = _enforce_pairing_sanity_pass(messages)
        assert sanity_synthesized == []
        assert len(messages) == 2  # 不被改动


# ── 通道层端到端集成（zhipu 产物全量清洗） ───────────────────────────


class TestZhipuToAnthropicChannelFullCleanup:
    """验证 prepare_zhipu_to_anthropic 对完整 zhipu 产物集合的清洗."""

    def test_rewrites_srvtoolu_and_strips_vendor_delta(self):
        body = {
            "messages": [
                {"role": "user", "content": "run tools"},
                {
                    "role": "assistant",
                    "content": [
                        {
                            "type": "server_tool_use",
                            "id": "srvtoolu_alpha",
                            "name": "bash",
                            "input": {"cmd": "ls"},
                        },
                        {
                            "type": "server_tool_use_delta",
                            "partial_json": '{"cmd":"ls"}',
                        },
                    ],
                },
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": "srvtoolu_alpha",
                            "content": "output",
                        },
                    ],
                },
            ],
        }
        prepared, adaptations = prepare_zhipu_to_anthropic(body)
        # server_tool_use_delta 已被剥离
        assert any(
            b.get("type") == "tool_use" for b in prepared["messages"][1]["content"]
        )
        assert not any(
            b.get("type") == "server_tool_use_delta"
            for b in prepared["messages"][1]["content"]
        )
        # srvtoolu_* ID 已重写
        new_id = prepared["messages"][1]["content"][0]["id"]
        assert new_id.startswith("toolu_normalized_")
        assert prepared["messages"][1]["content"][0]["type"] == "tool_use"
        # tool_result 引用同步更新
        assert prepared["messages"][2]["content"][0]["tool_use_id"] == new_id
        # adaptations 覆盖完整清洗项
        assert any("zhipu_vendor_blocks" in a for a in adaptations)
        assert any("srvtoolu_ids" in a for a in adaptations)

    def test_full_zhipu_artifacts_combined(self):
        """srvtoolu_* + server_tool_use_delta + misplaced tool_result + thinking."""
        body = {
            "messages": [
                {"role": "user", "content": "start"},
                {
                    "role": "assistant",
                    "content": [
                        {
                            "type": "thinking",
                            "thinking": "...",
                            "signature": "zhipu_sig",
                        },
                        {
                            "type": "server_tool_use",
                            "id": "srvtoolu_x",
                            "name": "bash",
                            "input": {},
                        },
                        {
                            "type": "server_tool_use_delta",
                            "partial_json": "{}",
                        },
                        {
                            "type": "tool_result",
                            "tool_use_id": "srvtoolu_x",
                            "content": "inline",
                        },
                    ],
                },
                {"role": "user", "content": "ok"},
            ],
        }
        prepared, adaptations = prepare_zhipu_to_anthropic(body)
        assistant_content = prepared["messages"][1]["content"]
        # thinking/server_tool_use_delta/tool_result 均被剥离
        types = {b.get("type") for b in assistant_content}
        assert types == {"tool_use"}
        assert len(assistant_content) == 1
        new_id = assistant_content[0]["id"]
        assert new_id.startswith("toolu_normalized_")
        # tool_result 被重定位到 user 消息（索引 2）
        user_content = prepared["messages"][2]["content"]
        assert isinstance(user_content, list)
        relocated = [b for b in user_content if b.get("type") == "tool_result"]
        assert len(relocated) == 1
        assert relocated[0]["tool_use_id"] == new_id
        assert any("misplaced_tool_result_relocated" in a for a in adaptations)

    def test_inline_tool_result_before_tool_use_pairs_correctly(self):
        """日志现象回归保护: GLM-5 输出 [inline tool_result, tool_use] 块顺序时,
        修复前 _rewrite 单遍扫描漏改 inline.tool_use_id, enforce 阶段 dict key
        与 tool_use_ids 错位, 导致最终 anthropic 报 'tool_use ids without
        tool_result blocks immediately after'.

        修复后两遍扫描确保 inline tool_result 与 tool_use 同步改名, enforce 能
        正确将 inline 搬迁到 next user, 不需合成 is_error 占位 (无 orphan 标签).
        """
        body = {
            "messages": [
                {"role": "user", "content": "task"},
                # 上一轮完成
                {
                    "role": "assistant",
                    "content": [
                        {
                            "type": "server_tool_use",
                            "id": "srvtoolu_A",
                            "name": "bash",
                            "input": {},
                        },
                    ],
                },
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": "srvtoolu_A",
                            "content": "A-ok",
                        },
                    ],
                },
                # 当前轮: inline tool_result 在 server_tool_use 之前 (流式断片)
                {
                    "role": "assistant",
                    "content": [
                        {
                            "type": "thinking",
                            "thinking": "...",
                            "signature": "zhipu_sig",
                        },
                        # inline tool_result 在 tool_use 之前!
                        {
                            "type": "tool_result",
                            "tool_use_id": "srvtoolu_B",
                            "content": "B-inline",
                        },
                        {
                            "type": "server_tool_use",
                            "id": "srvtoolu_B",
                            "name": "bash",
                            "input": {},
                        },
                    ],
                },
                # 客户端没回 B 的 tool_result (因为已被 inline)
                {"role": "user", "content": []},
            ],
        }
        prepared, adaptations = prepare_zhipu_to_anthropic(body)

        # 关键断言: 仅有 misplaced_tool_result_relocated, 无 orphaned_tool_use_repaired
        # (因 inline 真实内容被正确搬迁, 无需合成 is_error 占位)
        assert "misplaced_tool_result_relocated" in adaptations
        assert "orphaned_tool_use_repaired" not in adaptations
        assert "pairing_sanity_repaired" not in adaptations

        # 验证 messages[3] 的 tool_use 在 messages[4] 有匹配 tool_result
        m3 = prepared["messages"][3]
        m4 = prepared["messages"][4]
        m3_tool_uses = [
            b["id"]
            for b in m3["content"]
            if isinstance(b, dict) and b.get("type") == "tool_use"
        ]
        m4_results = {
            b.get("tool_use_id")
            for b in m4["content"]
            if isinstance(b, dict) and b.get("type") == "tool_result"
        }
        assert len(m3_tool_uses) == 1
        assert m3_tool_uses[0] in m4_results

        # 搬迁的 tool_result 应保留原始内容 ("B-inline"), 而非合成的空 is_error
        relocated = next(
            b
            for b in m4["content"]
            if isinstance(b, dict) and b.get("type") == "tool_result"
        )
        assert relocated["content"] == "B-inline"
        assert relocated.get("is_error") is not True


class TestZhipuToCopilotChannelFullCleanup:
    """验证 prepare_zhipu_to_copilot 对 zhipu 产物的完整清洗."""

    def test_rewrites_srvtoolu_and_strips_vendor_delta(self):
        body = {
            "messages": [
                {
                    "role": "assistant",
                    "content": [
                        {
                            "type": "server_tool_use",
                            "id": "srvtoolu_beta",
                            "name": "read",
                            "input": {},
                        },
                        {
                            "type": "server_tool_use_delta",
                            "partial_json": "{}",
                        },
                    ],
                },
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": "srvtoolu_beta",
                            "content": "ok",
                        },
                    ],
                },
            ],
        }
        prepared, adaptations = prepare_zhipu_to_copilot(body)
        assistant_content = prepared["messages"][0]["content"]
        assert {b.get("type") for b in assistant_content} == {"tool_use"}
        new_id = assistant_content[0]["id"]
        assert new_id.startswith("toolu_normalized_")
        assert prepared["messages"][1]["content"][0]["tool_use_id"] == new_id
        assert any("zhipu_vendor_blocks" in a for a in adaptations)
        assert any("srvtoolu_ids" in a for a in adaptations)


# ── anthropic → zhipu 转换通道测试 ──────────────────────────────


class TestAnthropicToZhipuChannel:
    """prepare_anthropic_to_zhipu 转换通道单元测试."""

    def test_strips_server_tool_use_blocks(self):
        """Anthropic 的 server_tool_use（web search, computer use）应被剥离."""
        body = {
            "messages": [
                {
                    "role": "assistant",
                    "content": [
                        {"type": "text", "text": "Let me search..."},
                        {
                            "type": "server_tool_use",
                            "id": "toolu_web_search_123",
                            "name": "web_search",
                            "input": {"query": "python async"},
                        },
                    ],
                },
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": "toolu_web_search_123",
                            "content": "search results",
                        },
                    ],
                },
            ],
        }
        prepared, adaptations = prepare_anthropic_to_zhipu(body)
        assert any("server_tool_use" in a for a in adaptations)
        assistant_content = prepared["messages"][0]["content"]
        assert all(b.get("type") != "server_tool_use" for b in assistant_content)
        assert assistant_content == [{"type": "text", "text": "Let me search..."}]

    def test_strips_thinking_blocks(self):
        """Anthropic 签发的 thinking blocks 应被剥离（zhipu 可能无法验证 signature）."""
        body = {
            "messages": [
                {
                    "role": "assistant",
                    "content": [
                        {"type": "thinking", "thinking": "thought", "signature": "sig"},
                        {"type": "text", "text": "response"},
                    ],
                },
            ],
        }
        prepared, adaptations = prepare_anthropic_to_zhipu(body)
        assert any("thinking_blocks" in a for a in adaptations)
        assert prepared["messages"][0]["content"] == [
            {"type": "text", "text": "response"},
        ]

    def test_removes_cache_control(self):
        body = {
            "system": [
                {"type": "text", "text": "sys", "cache_control": {"type": "ephemeral"}},
            ],
            "messages": [],
        }
        prepared, adaptations = prepare_anthropic_to_zhipu(body)
        assert any("cache_control" in a for a in adaptations)
        assert "cache_control" not in prepared["system"][0]

    def test_removes_thinking_params(self):
        body = {
            "messages": [],
            "thinking": {"type": "enabled", "budget_tokens": 10000},
            "extended_thinking": {"type": "enabled"},
        }
        prepared, adaptations = prepare_anthropic_to_zhipu(body)
        assert "thinking" not in prepared
        assert "extended_thinking" not in prepared
        assert "removed_thinking_param" in adaptations
        assert "removed_extended_thinking_param" in adaptations

    def test_enforces_tool_pairing(self):
        body = {
            "messages": [
                {
                    "role": "assistant",
                    "content": [
                        {
                            "type": "tool_use",
                            "id": "toolu_1",
                            "name": "bash",
                            "input": {},
                        },
                    ],
                },
                {"role": "user", "content": "next"},
            ],
        }
        prepared, adaptations = prepare_anthropic_to_zhipu(body)
        assert "orphaned_tool_use_repaired" in adaptations
        user_results = [
            b
            for b in prepared["messages"][1]["content"]
            if isinstance(b, dict) and b.get("type") == "tool_result"
        ]
        assert len(user_results) == 1
        assert user_results[0]["tool_use_id"] == "toolu_1"

    def test_preserves_original_body(self):
        body = {
            "messages": [
                {
                    "role": "assistant",
                    "content": [
                        {
                            "type": "server_tool_use",
                            "id": "toolu_1",
                            "name": "web_search",
                            "input": {},
                        },
                        {"type": "text", "text": "hi"},
                    ],
                },
            ],
        }
        original = copy.deepcopy(body)
        prepare_anthropic_to_zhipu(body)
        assert body == original

    def test_noop_when_clean(self):
        body = {
            "messages": [
                {"role": "user", "content": "hello"},
                {"role": "assistant", "content": [{"type": "text", "text": "hi"}]},
            ],
        }
        prepared, adaptations = prepare_anthropic_to_zhipu(body)
        assert adaptations == []
        assert prepared == body

    def test_idempotency(self):
        body = {
            "messages": [
                {
                    "role": "assistant",
                    "content": [
                        {"type": "thinking", "thinking": "t"},
                        {
                            "type": "server_tool_use",
                            "id": "toolu_1",
                            "name": "web_search",
                            "input": {},
                        },
                    ],
                },
            ],
            "thinking": {"type": "enabled"},
        }
        prepared1, adaptations1 = prepare_anthropic_to_zhipu(body)
        prepared2, adaptations2 = prepare_anthropic_to_zhipu(prepared1)
        assert prepared2 == prepared1
        assert adaptations2 == []

    def test_strips_multiple_server_tool_use_blocks(self):
        """多个 server_tool_use 块（web search + computer use）全部剥离."""
        body = {
            "messages": [
                {
                    "role": "assistant",
                    "content": [
                        {
                            "type": "server_tool_use",
                            "id": "toolu_ws_1",
                            "name": "web_search",
                            "input": {"query": "test"},
                        },
                        {
                            "type": "server_tool_use",
                            "id": "toolu_cu_1",
                            "name": "computer",
                            "input": {"action": "click"},
                        },
                    ],
                },
            ],
        }
        prepared, adaptations = prepare_anthropic_to_zhipu(body)
        assert not any(
            b.get("type") == "server_tool_use"
            for b in prepared["messages"][0]["content"]
        )
        assert "removed_2_server_tool_use" in adaptations[0]

    def test_inserts_placeholder_when_all_blocks_stripped(self):
        """assistant 消息仅含 server_tool_use 时插入占位 text block."""
        body = {
            "messages": [
                {
                    "role": "assistant",
                    "content": [
                        {
                            "type": "server_tool_use",
                            "id": "toolu_1",
                            "name": "web_search",
                            "input": {},
                        },
                    ],
                },
            ],
        }
        prepared, _ = prepare_anthropic_to_zhipu(body)
        assert prepared["messages"][0]["content"] == [
            {"type": "text", "text": "[vendor_block_removed]"},
        ]

    def test_combined_server_tool_use_and_thinking(self):
        """server_tool_use + thinking + cache_control 的组合清洗."""
        body = {
            "system": [
                {"type": "text", "text": "sys", "cache_control": {"type": "ephemeral"}},
            ],
            "messages": [
                {
                    "role": "assistant",
                    "content": [
                        {"type": "thinking", "thinking": "t", "signature": "s"},
                        {
                            "type": "server_tool_use",
                            "id": "toolu_cu_1",
                            "name": "computer",
                            "input": {},
                        },
                        {"type": "text", "text": "done"},
                    ],
                },
            ],
            "thinking": {"type": "enabled"},
        }
        prepared, adaptations = prepare_anthropic_to_zhipu(body)
        assert all(
            b.get("type") not in ("thinking", "redacted_thinking", "server_tool_use")
            for b in prepared["messages"][0]["content"]
        )
        assert "cache_control" not in prepared["system"][0]
        assert "thinking" not in prepared
        assert any("server_tool_use" in a for a in adaptations)
        assert any("thinking_blocks" in a for a in adaptations)
