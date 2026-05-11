"""供应商跨供应商转换通道单元测试.

覆盖 :mod:`coding.proxy.convert.vendor_channels` 的转换通道函数和辅助函数:
- zhipu → anthropic 转换 (prepare_zhipu_to_anthropic)
- zhipu → copilot 转换 (prepare_zhipu_to_copilot)
- copilot → zhipu 转换 (prepare_copilot_to_zhipu)
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
    prepare_copilot_to_zhipu,
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

    def test_two_pass_handles_inline_tool_result_before_server_tool_use(self):
        """乱序回归: 同一 assistant content 内 tool_result 出现在 server_tool_use 之前.

        Zhipu GLM-5 流式响应中已观察到的真实形态。若使用单遍扫描，
        Case B 在 tool_result 块上执行时 ``id_map`` 尚未被 Case A 填入，
        会漏改 ``tool_result.tool_use_id``，留下旧的 ``srvtoolu_*`` 引用，
        最终触发 Anthropic API 的 ``messages.x: tool_use ids were found
        without tool_result blocks immediately after`` 400 错误。

        修复后的两遍扫描必须保证 ``id_map`` 在 Pass 1 完整建立、
        Pass 2 再统一改写 tool_result.tool_use_id, 与块出现顺序无关。
        """
        body = {
            "messages": [
                {"role": "user", "content": "ask"},
                {
                    "role": "assistant",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": "srvtoolu_oof",
                            "content": "out",
                        },
                        {
                            "type": "server_tool_use",
                            "id": "srvtoolu_oof",
                            "name": "bash",
                            "input": {},
                        },
                    ],
                },
            ],
        }
        count, id_map = _rewrite_srvtoolu_ids(body)
        assert count == 1
        new_id = id_map["srvtoolu_oof"]
        assert new_id.startswith("toolu_normalized_")

        blocks = body["messages"][1]["content"]
        tool_result_block = next(b for b in blocks if b.get("type") == "tool_result")
        tool_use_block = next(b for b in blocks if b.get("type") == "tool_use")
        assert tool_result_block["tool_use_id"] == new_id
        assert tool_use_block["id"] == new_id
        assert tool_use_block["type"] == "tool_use"

    def test_two_pass_handles_tool_result_in_earlier_user_message(self):
        """跨消息边界乱序: tool_result 在更早的 user 消息中先出现.

        旧单遍扫描遍历到 msg[1] 的 user tool_result 时 ``id_map`` 还未含
        ``srvtoolu_late``（对应 tool_use 在 msg[2]），导致漏改;
        两遍扫描必须保证此场景下 tool_result.tool_use_id 仍能正确改写.
        """
        body = {
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": "srvtoolu_late",
                            "content": "prefetched",
                        },
                    ],
                },
                {
                    "role": "assistant",
                    "content": [
                        {
                            "type": "server_tool_use",
                            "id": "srvtoolu_late",
                            "name": "bash",
                            "input": {},
                        },
                    ],
                },
            ],
        }
        count, id_map = _rewrite_srvtoolu_ids(body)
        assert count == 1
        new_id = id_map["srvtoolu_late"]
        assert body["messages"][0]["content"][0]["tool_use_id"] == new_id, (
            "Pass 2 必须改写出现位置早于 tool_use 的 tool_result.tool_use_id"
        )
        assert body["messages"][1]["content"][0]["id"] == new_id


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

    def test_detects_zhipu_by_server_tool_use_type(self):
        body = {
            "messages": [
                {
                    "role": "assistant",
                    "content": [
                        {
                            "type": "server_tool_use",
                            "id": "toolu_any",
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


# ── _enforce_pairing_sanity_pass 单元测试（纵深防御兜底层） ─────────────


class TestEnforcePairingSanityPass:
    """``_enforce_pairing_sanity_pass`` 单元测试.

    这层是 enforce 主循环结束后的纵深防御。直接以 helper 为被测单元，
    确保即使主循环未来重构出现遗漏，sanity 仍能稳定守住 Anthropic 配对约束。
    """

    def test_noop_when_all_paired(self):
        """所有 tool_use 都已正确配对时返回空列表，不修改输入."""
        messages = [
            {
                "role": "assistant",
                "content": [
                    {"type": "tool_use", "id": "toolu_x", "name": "bash", "input": {}}
                ],
            },
            {
                "role": "user",
                "content": [
                    {"type": "tool_result", "tool_use_id": "toolu_x", "content": "ok"}
                ],
            },
        ]
        snapshot = copy.deepcopy(messages)
        result = _enforce_pairing_sanity_pass(messages)
        assert result == []
        assert messages == snapshot

    def test_appends_is_error_placeholder_when_user_lacks_tool_result(self):
        """assistant tool_use 但 user 缺 tool_result 时追加 is_error 占位."""
        messages = [
            {
                "role": "assistant",
                "content": [
                    {"type": "tool_use", "id": "toolu_x", "name": "bash", "input": {}}
                ],
            },
            {"role": "user", "content": [{"type": "text", "text": "ok"}]},
        ]
        result = _enforce_pairing_sanity_pass(messages)
        assert result == ["pairing_sanity_repaired"]
        user_content = messages[1]["content"]
        appended = next(b for b in user_content if b.get("type") == "tool_result")
        assert appended == {
            "type": "tool_result",
            "tool_use_id": "toolu_x",
            "content": "",
            "is_error": True,
        }

    def test_repairs_only_missing_ids_when_partially_paired(self):
        """3 tool_use 但 user 只配 2 个 tool_result 时仅补缺失项."""
        messages = [
            {
                "role": "assistant",
                "content": [
                    {"type": "tool_use", "id": "toolu_a", "name": "bash", "input": {}},
                    {"type": "tool_use", "id": "toolu_b", "name": "read", "input": {}},
                    {"type": "tool_use", "id": "toolu_c", "name": "write", "input": {}},
                ],
            },
            {
                "role": "user",
                "content": [
                    {"type": "tool_result", "tool_use_id": "toolu_a", "content": "a"},
                    {"type": "tool_result", "tool_use_id": "toolu_c", "content": "c"},
                ],
            },
        ]
        result = _enforce_pairing_sanity_pass(messages)
        assert result == ["pairing_sanity_repaired"]
        result_ids = {
            b["tool_use_id"]
            for b in messages[1]["content"]
            if b.get("type") == "tool_result"
        }
        assert result_ids == {"toolu_a", "toolu_b", "toolu_c"}
        # 仅 toolu_b 是兜底合成的 is_error 占位
        b_block = next(
            b for b in messages[1]["content"] if b.get("tool_use_id") == "toolu_b"
        )
        assert b_block.get("is_error") is True
        a_block = next(
            b for b in messages[1]["content"] if b.get("tool_use_id") == "toolu_a"
        )
        assert a_block.get("is_error") is not True

    def test_warns_when_next_message_not_user(self, caplog):
        """next 非 user 时只发 WARNING、不修改、不返回 adaptation.

        主循环正常情况下已保证 next 为 user；这是退化场景的可观测性兜底。
        """
        messages = [
            {
                "role": "assistant",
                "content": [
                    {"type": "tool_use", "id": "toolu_x", "name": "bash", "input": {}}
                ],
            },
            {
                "role": "assistant",
                "content": [{"type": "text", "text": "weird"}],
            },
        ]
        snapshot = copy.deepcopy(messages)
        import logging

        with caplog.at_level(
            logging.WARNING, logger="coding.proxy.convert.vendor_channels"
        ):
            result = _enforce_pairing_sanity_pass(messages)
        assert result == []
        assert messages == snapshot
        assert any("Sanity pass" in rec.message for rec in caplog.records)

    def test_normalizes_user_string_content_before_repair(self):
        """user content 为 string 时归一化为 list 再补占位."""
        messages = [
            {
                "role": "assistant",
                "content": [
                    {"type": "tool_use", "id": "toolu_x", "name": "bash", "input": {}}
                ],
            },
            {"role": "user", "content": "ack"},
        ]
        result = _enforce_pairing_sanity_pass(messages)
        assert result == ["pairing_sanity_repaired"]
        user_content = messages[1]["content"]
        assert isinstance(user_content, list)
        assert user_content[0] == {"type": "text", "text": "ack"}
        assert user_content[1]["tool_use_id"] == "toolu_x"
        assert user_content[1]["is_error"] is True

    def test_skips_non_assistant_messages(self):
        """user / system / 异常消息一律跳过."""
        messages = [
            {"role": "user", "content": "hi"},
            {"role": "system", "content": "ctx"},
            "not a dict",  # type: ignore[list-item]
        ]
        snapshot = copy.deepcopy(messages)
        result = _enforce_pairing_sanity_pass(messages)
        assert result == []
        assert messages == snapshot

    def test_skips_assistant_without_tool_use(self):
        """assistant 纯文本（无 tool_use）短路，不影响下一条 user."""
        messages = [
            {
                "role": "assistant",
                "content": [{"type": "text", "text": "just chatting"}],
            },
            {"role": "user", "content": "ok"},
        ]
        snapshot = copy.deepcopy(messages)
        result = _enforce_pairing_sanity_pass(messages)
        assert result == []
        assert messages == snapshot

    def test_enforce_main_loop_chains_sanity_helper(self):
        """主 enforce 流程末尾应当调用 sanity helper，标签会出现在 adaptations."""
        # 构造主循环无法剥离/合成的退化场景：直接放一个未配对 tool_use，
        # 且 user 端事先放无关 tool_result，绕过主循环的 existing check
        messages = [
            {
                "role": "assistant",
                "content": [
                    {
                        "type": "tool_use",
                        "id": "toolu_main",
                        "name": "bash",
                        "input": {},
                    }
                ],
            },
            {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "toolu_unrelated",
                        "content": "x",
                    }
                ],
            },
        ]
        fixes = enforce_anthropic_tool_pairing(messages)
        # 主循环 F 步会先合成 orphaned_tool_use_repaired, sanity 不再触发
        assert "orphaned_tool_use_repaired" in fixes
        assert "pairing_sanity_repaired" not in fixes
        # 但 toolu_main 必须最终有对应 tool_result
        result_ids = {
            b["tool_use_id"]
            for b in messages[1]["content"]
            if b.get("type") == "tool_result"
        }
        assert "toolu_main" in result_ids


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

    def test_handles_out_of_order_inline_tool_result_end_to_end(self):
        """端到端复现日志故障场景: assistant content 内 tool_result 排在 server_tool_use 之前.

        生产日志 `messages.3: tool_use ids were found without tool_result blocks
        immediately after: toolu_normalized_2` 错误的等价最小复现.

        旧单遍 ``_rewrite_srvtoolu_ids`` 会漏改这种 misplaced tool_result 的
        ``tool_use_id``，使 enforce 在 extracted_tool_results 字典中以旧 ID 作 key，
        而 tool_use_ids 已是新 ID，造成 pairing 错位; 修复后两遍扫描确保
        每个 assistant.tool_use_id 与下一条 user.tool_result.tool_use_id
        一一匹配，且消息体内不再残留任何 ``srvtoolu_*`` / ``server_tool_use``。
        """
        body = {
            "messages": [
                {"role": "user", "content": "begin"},
                # 第一轮: 普通配对，建立 toolu_normalized_1
                {
                    "role": "assistant",
                    "content": [
                        {
                            "type": "thinking",
                            "thinking": "...",
                            "signature": "zhipu_sig_1",
                        },
                        {
                            "type": "server_tool_use",
                            "id": "srvtoolu_first",
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
                            "tool_use_id": "srvtoolu_first",
                            "content": "first ok",
                        }
                    ],
                },
                # 第二轮: 故障形态，tool_result 内联在 server_tool_use 之前
                {
                    "role": "assistant",
                    "content": [
                        {
                            "type": "thinking",
                            "thinking": "...",
                            "signature": "zhipu_sig_2",
                        },
                        {
                            "type": "tool_result",
                            "tool_use_id": "srvtoolu_second",
                            "content": "inline glm5",
                        },
                        {
                            "type": "server_tool_use",
                            "id": "srvtoolu_second",
                            "name": "bash",
                            "input": {},
                        },
                    ],
                },
                {"role": "user", "content": "continue"},
            ],
        }
        prepared, adaptations = prepare_zhipu_to_anthropic(body)
        messages = prepared["messages"]

        # 所有 assistant 消息不得残留 server_tool_use / srvtoolu_* / tool_result
        for msg in messages:
            if msg.get("role") != "assistant":
                continue
            for b in msg.get("content", []):
                assert isinstance(b, dict)
                assert b.get("type") != "server_tool_use"
                assert b.get("type") != "tool_result"
                bid = b.get("id")
                if isinstance(bid, str):
                    assert not bid.startswith("srvtoolu_"), (
                        f"assistant content 残留 srvtoolu_* ID: {bid}"
                    )

        # 任意 tool_result.tool_use_id 不得保留为 srvtoolu_* 形式
        for msg in messages:
            for b in msg.get("content") or []:
                if isinstance(b, dict) and b.get("type") == "tool_result":
                    tid = b.get("tool_use_id")
                    assert isinstance(tid, str)
                    assert not tid.startswith("srvtoolu_"), (
                        f"tool_result 残留旧 srvtoolu_* 引用: {tid}"
                    )

        # 每个 assistant 的 tool_use.id 都能在下一条 user 的 tool_result 中找到匹配
        for i, msg in enumerate(messages):
            if msg.get("role") != "assistant":
                continue
            tool_use_ids = [
                b["id"]
                for b in (msg.get("content") or [])
                if isinstance(b, dict) and b.get("type") == "tool_use" and b.get("id")
            ]
            if not tool_use_ids:
                continue
            next_msg = messages[i + 1]
            assert next_msg.get("role") == "user"
            next_tool_result_ids = {
                b["tool_use_id"]
                for b in (next_msg.get("content") or [])
                if isinstance(b, dict)
                and b.get("type") == "tool_result"
                and b.get("tool_use_id")
            }
            for uid in tool_use_ids:
                assert uid in next_tool_result_ids, (
                    f"messages[{i}].tool_use_id={uid} 在 messages[{i + 1}] 中"
                    f"找不到对应 tool_result（next ids = {next_tool_result_ids}）"
                )

        # adaptations 覆盖关键变换
        assert any("srvtoolu_ids" in a for a in adaptations)
        assert any("misplaced_tool_result_relocated" in a for a in adaptations)
        assert any("thinking_blocks" in a for a in adaptations)
        # sanity 不应触发: 两遍扫描 + 主 enforce 已经把所有配对补齐
        assert "pairing_sanity_repaired" not in adaptations
        # 主 enforce 应当能正确把内联 tool_result 重定位、配对完整
        assert "orphaned_tool_use_repaired" not in adaptations


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
