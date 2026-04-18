"""供应商跨供应商转换通道单元测试.

覆盖 :mod:`coding.proxy.convert.vendor_channels` 的转换通道函数和辅助函数:
- zhipu → anthropic 转换 (prepare_zhipu_to_anthropic)
- zhipu → copilot 转换 (prepare_zhipu_to_copilot)
- copilot → zhipu 转换 (prepare_copilot_to_zhipu)
- 共享辅助函数 (_strip_thinking_blocks_inplace, _strip_cache_control)
- 转换注册表 (VENDOR_TRANSITIONS, get_transition_channel)
"""

from __future__ import annotations

import copy

from coding.proxy.convert.vendor_channels import (
    VENDOR_TRANSITIONS,
    _strip_cache_control,
    _strip_thinking_blocks_inplace,
    get_transition_channel,
    prepare_copilot_to_zhipu,
    prepare_zhipu_to_anthropic,
    prepare_zhipu_to_copilot,
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


# ── 转换注册表测试 ────────────────────────────────────────────


class TestTransitionRegistry:
    """VENDOR_TRANSITIONS / get_transition_channel 单元测试."""

    def test_all_transitions_registered(self):
        assert ("zhipu", "anthropic") in VENDOR_TRANSITIONS
        assert ("zhipu", "copilot") in VENDOR_TRANSITIONS
        assert ("copilot", "zhipu") in VENDOR_TRANSITIONS
        assert len(VENDOR_TRANSITIONS) == 3

    def test_get_transition_channel_returns_function(self):
        assert (
            get_transition_channel("zhipu", "anthropic") is prepare_zhipu_to_anthropic
        )
        assert get_transition_channel("zhipu", "copilot") is prepare_zhipu_to_copilot
        assert get_transition_channel("copilot", "zhipu") is prepare_copilot_to_zhipu

    def test_get_transition_channel_returns_none_for_unregistered(self):
        assert get_transition_channel("anthropic", "zhipu") is None
        assert get_transition_channel("copilot", "anthropic") is None
        assert get_transition_channel("unknown", "target") is None
        assert get_transition_channel("antigravity", "copilot") is None

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

    def test_all_transitions_strip_thinking_blocks(self):
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
            result, adaptations = fn(body)
            assert result["messages"][0]["content"] == [
                {"type": "text", "text": "hi"}
            ], f"Transition {key} failed to strip thinking blocks"
