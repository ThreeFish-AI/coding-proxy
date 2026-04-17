"""供应商专属转换通道单元测试.

覆盖 :mod:`coding.proxy.convert.vendor_channels` 的通道函数和辅助函数:
- zhipu 通道 (prepare_for_zhipu)
- copilot 通道 (prepare_for_copilot)
- 共享辅助函数 (_strip_thinking_blocks_inplace, _strip_cache_control)
"""

from __future__ import annotations

import copy

from coding.proxy.convert.vendor_channels import (
    _strip_cache_control,
    _strip_thinking_blocks_inplace,
    prepare_for_copilot,
    prepare_for_zhipu,
)

# ── 辅助函数测试 ──────────────────────────────────────────────


class TestStripThinkingBlocksInplace:
    """_strip_thinking_blocks_inplace 单元测试."""

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
        stripped = _strip_thinking_blocks_inplace(body)
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
        stripped = _strip_thinking_blocks_inplace(body)
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
        stripped = _strip_thinking_blocks_inplace(body)
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
        stripped = _strip_thinking_blocks_inplace(body)
        assert stripped == 0
        assert body["messages"][0]["content"] == [{"type": "text", "text": "hello"}]

    def test_skips_non_assistant_messages(self):
        body = {
            "messages": [
                {"role": "user", "content": [{"type": "thinking", "thinking": "t"}]},
            ]
        }
        stripped = _strip_thinking_blocks_inplace(body)
        assert stripped == 0

    def test_handles_string_content(self):
        body = {
            "messages": [
                {"role": "assistant", "content": "plain text"},
            ]
        }
        stripped = _strip_thinking_blocks_inplace(body)
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


# ── zhipu 通道测试 ────────────────────────────────────────────


class TestZhipuChannel:
    """prepare_for_zhipu 通道单元测试."""

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
        prepared, adaptations = prepare_for_zhipu(body)
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
        prepared, adaptations = prepare_for_zhipu(body)
        assert any("cache_control" in a for a in adaptations)
        assert "cache_control" not in prepared["system"][0]

    def test_removes_thinking_params(self):
        body = {
            "messages": [],
            "thinking": {"type": "enabled", "budget_tokens": 10000},
            "extended_thinking": {"type": "enabled"},
        }
        prepared, adaptations = prepare_for_zhipu(body)
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
        prepared, adaptations = prepare_for_zhipu(body)
        # tool_use should have a corresponding tool_result
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
        prepared, adaptations = prepare_for_zhipu(body)
        # thinking blocks stripped
        assert all(
            b.get("type") not in ("thinking", "redacted_thinking")
            for b in prepared["messages"][0]["content"]
        )
        # cache_control removed
        assert "cache_control" not in prepared["system"][0]
        # thinking param removed
        assert "thinking" not in prepared
        # tool pairing enforced
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
        prepare_for_zhipu(body)
        assert body == original

    def test_noop_when_clean(self):
        body = {
            "messages": [
                {"role": "user", "content": "hello"},
                {"role": "assistant", "content": [{"type": "text", "text": "hi"}]},
            ],
        }
        prepared, adaptations = prepare_for_zhipu(body)
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
        prepared1, adaptations1 = prepare_for_zhipu(body)
        prepared2, adaptations2 = prepare_for_zhipu(prepared1)
        assert prepared2 == prepared1
        assert adaptations2 == []


# ── copilot 通道测试 ──────────────────────────────────────────


class TestCopilotChannel:
    """prepare_for_copilot 通道单元测试."""

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
        prepared, adaptations = prepare_for_copilot(body)
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
        prepared, adaptations = prepare_for_copilot(body)
        assert any("cache_control" in a for a in adaptations)
        assert "cache_control" not in prepared["messages"][0]["content"][0]

    def test_preserves_thinking_param(self):
        """copilot 通道不移除顶层 thinking 参数（由 converter 自行映射）."""
        body = {
            "messages": [],
            "thinking": {"type": "enabled", "budget_tokens": 10000},
        }
        prepared, adaptations = prepare_for_copilot(body)
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
        prepared, adaptations = prepare_for_copilot(body)
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
        prepare_for_copilot(body)
        assert body == original

    def test_noop_when_clean(self):
        body = {
            "messages": [
                {"role": "user", "content": "hello"},
                {"role": "assistant", "content": [{"type": "text", "text": "hi"}]},
            ],
        }
        prepared, adaptations = prepare_for_copilot(body)
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
        prepared1, _ = prepare_for_copilot(body)
        prepared2, adaptations2 = prepare_for_copilot(prepared1)
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
        prepared, adaptations = prepare_for_copilot(body)
        assert any("thinking_blocks" in a for a in adaptations)
        assert prepared["messages"][0]["content"] == [
            {"type": "text", "text": "response"},
        ]


# ── zhipu vs copilot 通道差异测试 ────────────────────────────


class TestChannelDifferences:
    """验证 zhipu 和 copilot 通道的关键行为差异."""

    def test_zhipu_removes_thinking_param_copilot_preserves(self):
        body = {
            "messages": [],
            "thinking": {"type": "enabled", "budget_tokens": 5000},
        }
        zhipu_result, zhipu_adapt = prepare_for_zhipu(body)
        copilot_result, copilot_adapt = prepare_for_copilot(body)

        assert "thinking" not in zhipu_result
        assert "removed_thinking_param" in zhipu_adapt

        assert "thinking" in copilot_result
        assert "removed_thinking_param" not in copilot_adapt

    def test_both_strip_thinking_blocks(self):
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
        zhipu_result, _ = prepare_for_zhipu(body)
        copilot_result, _ = prepare_for_copilot(body)

        # 两者都剥离 thinking blocks
        assert zhipu_result["messages"][0]["content"] == [
            {"type": "text", "text": "hi"}
        ]
        assert copilot_result["messages"][0]["content"] == [
            {"type": "text", "text": "hi"}
        ]
