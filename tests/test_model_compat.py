"""coding.proxy.model.compat 数据类型单元测试.

覆盖 compat 模块中所有枚举、frozen / 非 frozen dataclass 的构造行为、
不可变性约束、嵌套类型关系及 to_dict() 序列化输出。
"""

from __future__ import annotations

import dataclasses

import pytest

from coding.proxy.model.compat import (
    CanonicalMessagePart,
    CanonicalPartType,
    CanonicalRequest,
    CanonicalThinking,
    CanonicalToolCall,
    CompatibilityDecision,
    CompatibilityProfile,
    CompatibilityStatus,
    CompatibilityTrace,
    CompatSessionRecord,
)


# ═══════════════════════════════════════════════════════════════
# 1. 枚举值完整性
# ═══════════════════════════════════════════════════════════════


class TestCanonicalPartTypeEnum:
    """CanonicalPartType 枚举值与字符串表示."""

    def test_all_values_exist(self) -> None:
        assert set(CanonicalPartType) == {
            CanonicalPartType.TEXT,
            CanonicalPartType.THINKING,
            CanonicalPartType.IMAGE,
            CanonicalPartType.TOOL_USE,
            CanonicalPartType.TOOL_RESULT,
            CanonicalPartType.UNKNOWN,
        }

    def test_string_values(self) -> None:
        assert CanonicalPartType.TEXT.value == "text"
        assert CanonicalPartType.THINKING.value == "thinking"
        assert CanonicalPartType.IMAGE.value == "image"
        assert CanonicalPartType.TOOL_USE.value == "tool_use"
        assert CanonicalPartType.TOOL_RESULT.value == "tool_result"
        assert CanonicalPartType.UNKNOWN.value == "unknown"

    def test_is_str_enum(self) -> None:
        """验证 str(Enum) 行为: 枚举实例可直接在字符串上下文中使用."""
        # str(Enum) 在 Python 3.11+ 返回枚举名; .value 始终返回底层字符串
        assert f"{CanonicalPartType.TEXT}" in ("text", "CanonicalPartType.TEXT")
        assert CanonicalPartType.TEXT.value == "text"


class TestCompatibilityStatusEnum:
    """CompatibilityStatus 枚举值与字符串表示."""

    def test_all_values_exist(self) -> None:
        assert set(CompatibilityStatus) == {
            CompatibilityStatus.NATIVE,
            CompatibilityStatus.SIMULATED,
            CompatibilityStatus.UNSAFE,
            CompatibilityStatus.UNKNOWN,
        }

    def test_string_values(self) -> None:
        assert CompatibilityStatus.NATIVE.value == "native"
        assert CompatibilityStatus.SIMULATED.value == "simulated"
        assert CompatibilityStatus.UNSAFE.value == "unsafe"
        assert CompatibilityStatus.UNKNOWN.value == "unknown"


# ═══════════════════════════════════════════════════════════════
# 2. Frozen dataclass 默认构造
# ═══════════════════════════════════════════════════════════════


class TestCanonicalThinkingDefaults:
    """CanonicalThinking 默认值."""

    def test_defaults(self) -> None:
        t = CanonicalThinking()
        assert t.enabled is False
        assert t.budget_tokens is None
        assert t.effort is None
        assert t.source_field is None


class TestCanonicalToolCallDefaults:
    """CanonicalToolCall 最小构造 (必填字段)."""

    def test_minimal_construction(self) -> None:
        tc = CanonicalToolCall(tool_id="tc_1", name="read_file")
        assert tc.tool_id == "tc_1"
        assert tc.name == "read_file"
        assert tc.arguments == {}
        assert tc.provider_tool_id is None
        assert tc.provider_kind == "function"


class TestCanonicalMessagePartDefaults:
    """CanonicalMessagePart 最小构造 (必填字段)."""

    def test_text_part_defaults(self) -> None:
        part = CanonicalMessagePart(type=CanonicalPartType.TEXT, role="user")
        assert part.type is CanonicalPartType.TEXT
        assert part.role == "user"
        assert part.text == ""
        assert part.tool_call is None
        assert part.tool_result_id is None
        assert part.raw_block is None


class TestCompatibilityProfileDefaults:
    """CompatibilityProfile 全部维度默认为 UNKNOWN."""

    def test_all_fields_default_unknown(self) -> None:
        profile = CompatibilityProfile()
        assert profile.thinking is CompatibilityStatus.UNKNOWN
        assert profile.tool_calling is CompatibilityStatus.UNKNOWN
        assert profile.tool_streaming is CompatibilityStatus.UNKNOWN
        assert profile.mcp_tools is CompatibilityStatus.UNKNOWN
        assert profile.images is CompatibilityStatus.UNKNOWN
        assert profile.metadata is CompatibilityStatus.UNKNOWN
        assert profile.json_output is CompatibilityStatus.UNKNOWN
        assert profile.usage_tokens is CompatibilityStatus.UNKNOWN


class TestCompatibilityDecisionDefaults:
    """CompatibilityDecision 默认列表为空."""

    def test_defaults(self) -> None:
        dec = CompatibilityDecision(status=CompatibilityStatus.NATIVE)
        assert dec.status is CompatibilityStatus.NATIVE
        assert dec.simulation_actions == []
        assert dec.unsupported_semantics == []


# ═══════════════════════════════════════════════════════════════
# 3. 自定义值完整构造
# ═══════════════════════════════════════════════════════════════


class TestCanonicalThinkingFullConstruction:
    """CanonicalThinking 全字段赋值."""

    def test_full_fields(self) -> None:
        t = CanonicalThinking(
            enabled=True, budget_tokens=1024, effort="high", source_field="thinking",
        )
        assert t.enabled is True
        assert t.budget_tokens == 1024
        assert t.effort == "high"
        assert t.source_field == "thinking"


class TestCanonicalToolCallFullConstruction:
    """CanonicalToolCall 全字段赋值."""

    def test_full_fields(self) -> None:
        tc = CanonicalToolCall(
            tool_id="tc_2",
            name="write_file",
            arguments={"path": "/tmp/a.txt", "content": "hello"},
            provider_tool_id="prov_tc_2",
            provider_kind="tool",
        )
        assert tc.arguments == {"path": "/tmp/a.txt", "content": "hello"}
        assert tc.provider_tool_id == "prov_tc_2"
        assert tc.provider_kind == "tool"


class TestCanonicalRequestFullConstruction:
    """CanonicalRequest 完整构造 (含嵌套 thinking 与 messages)."""

    def test_full_request(self) -> None:
        req = CanonicalRequest(
            session_key="sk_1",
            trace_id="tr_1",
            request_id="req_1",
            model="claude-sonnet-4-20250514",
            messages=[
                CanonicalMessagePart(
                    type=CanonicalPartType.TEXT, role="user", text="hello",
                ),
                CanonicalMessagePart(
                    type=CanonicalPartType.TOOL_USE,
                    role="assistant",
                    tool_call=CanonicalToolCall(tool_id="tc_a", name="read_file"),
                ),
            ],
            thinking=CanonicalThinking(enabled=True, budget_tokens=512),
            metadata={"user_id": "u_123"},
            tool_names=["read_file", "write_file"],
            supports_json_output=True,
        )
        assert req.session_key == "sk_1"
        assert len(req.messages) == 2
        assert req.thinking.enabled is True
        assert req.supports_json_output is True


# ═══════════════════════════════════════════════════════════════
# 4. Frozen 不可变性检查
# ═══════════════════════════════════════════════════════════════


class TestFrozenImmutability:
    """验证 frozen=True 的 dataclass 赋值时抛出 FrozenInstanceError."""

    @pytest.mark.parametrize(
        "cls, kwargs, attr, new_val",
        [
            (CanonicalThinking, {}, "enabled", True),
            (CanonicalToolCall, {"tool_id": "x", "name": "y"}, "name", "z"),
            (
                CanonicalMessagePart,
                {"type": CanonicalPartType.TEXT, "role": "user"},
                "text",
                "changed",
            ),
            (CompatibilityProfile, {}, "thinking", CompatibilityStatus.NATIVE),
            (
                CompatibilityDecision,
                {"status": CompatibilityStatus.UNKNOWN},
                "simulation_actions",
                ["x"],
            ),
        ],
        ids=[
            "CanonicalThinking",
            "CanonicalToolCall",
            "CanonicalMessagePart",
            "CompatibilityProfile",
            "CompatibilityDecision",
        ],
    )
    def test_frozen_raises_on_assignment(
        self,
        cls: type,
        kwargs: dict,
        attr: str,
        new_val: object,
    ) -> None:
        obj = cls(**kwargs)
        with pytest.raises(dataclasses.FrozenInstanceError):
            setattr(obj, attr, new_val)  # type: ignore[arg-type]

    def test_canonical_request_is_frozen(self) -> None:
        req = CanonicalRequest(
            session_key="a", trace_id="b", request_id="c", model="d",
            messages=[], thinking=CanonicalThinking(), metadata={}, tool_names=[],
            supports_json_output=False,
        )
        with pytest.raises(dataclasses.FrozenInstanceError):
            req.model = "e"  # type: ignore[misc]


# ═══════════════════════════════════════════════════════════════
# 5. CompatibilityTrace.to_dict() 输出结构
# ═══════════════════════════════════════════════════════════════


class TestCompatibilityTraceToDict:
    """CompatibilityTrace.to_dict() 返回结构与内容."""

    def test_to_dict_contains_all_fields(self) -> None:
        trace = CompatibilityTrace(
            trace_id="tr_1",
            vendor="copilot",
            session_key="sk_1",
            provider_protocol="openai",
            compat_mode="simulate",
            simulation_actions=["wrap_thinking"],
            unsupported_semantics=["extended_thinking"],
            session_state_hits=3,
            request_adaptations=["inject_system_prompt"],
            generated_at_unix=1700000000,
        )
        d = trace.to_dict()

        # 验证顶层 key 集合完备
        expected_keys = {
            "trace_id",
            "vendor",
            "session_key",
            "provider_protocol",
            "compat_mode",
            "simulation_actions",
            "unsupported_semantics",
            "session_state_hits",
            "request_adaptations",
            "generated_at_unix",
        }
        assert set(d.keys()) == expected_keys

    def test_to_dict_values_match(self) -> None:
        trace = CompatibilityTrace(
            trace_id="tr_2",
            vendor="zhipu",
            session_key="sk_2",
            provider_protocol="glm",
            compat_mode="native",
        )
        d = trace.to_dict()
        assert d["trace_id"] == "tr_2"
        assert d["vendor"] == "zhipu"
        assert d["simulation_actions"] == []
        assert d["session_state_hits"] == 0
        assert isinstance(d["generated_at_unix"], int)

    def test_to_dict_returns_independent_copy(self) -> None:
        """to_dict() 返回的 dict 不应与内部状态共享可变引用."""
        trace = CompatibilityTrace(
            trace_id="tr_3", vendor="x", session_key="y",
            provider_protocol="z", compat_mode="m",
            simulation_actions=["a"],
        )
        d = trace.to_dict()
        d["simulation_actions"].append("b")
        # 原对象不受影响
        assert trace.simulation_actions == ["a"]


# ═══════════════════════════════════════════════════════════════
# 6. 嵌套类型关系
# ═══════════════════════════════════════════════════════════════


class TestNestedTypeRelationships:
    """验证 CanonicalMessagePart 内嵌 CanonicalPartType / CanonicalToolCall,
    以及 CanonicalRequest 内嵌 CanonicalMessagePart / CanonicalThinking."""

    def test_message_part_with_tool_call(self) -> None:
        tc = CanonicalToolCall(tool_id="tc_n1", name="search", arguments={"q": "test"})
        part = CanonicalMessagePart(
            type=CanonicalPartType.TOOL_USE,
            role="assistant",
            tool_call=tc,
        )
        assert part.type is CanonicalPartType.TOOL_USE
        assert part.tool_call is not None
        assert part.tool_call.name == "search"
        assert part.tool_call.arguments == {"q": "test"}

    def test_message_part_with_tool_result(self) -> None:
        part = CanonicalMessagePart(
            type=CanonicalPartType.TOOL_RESULT,
            role="user",
            text='{"result": "ok"}',
            tool_result_id="tc_n1",
        )
        assert part.type is CanonicalPartType.TOOL_RESULT
        assert part.tool_result_id == "tc_n1"

    def test_request_contains_nested_thinking_and_messages(self) -> None:
        thinking = CanonicalThinking(enabled=True, budget_tokens=2048)
        messages = [
            CanonicalMessagePart(
                type=CanonicalPartType.TEXT, role="user", text="explain recursion",
            ),
            CanonicalMessagePart(
                type=CanonicalPartType.THINKING, role="assistant", text="let me think...",
            ),
        ]
        req = CanonicalRequest(
            session_key="sk_nested",
            trace_id="tr_nested",
            request_id="req_nested",
            model="claude-opus-4",
            messages=messages,
            thinking=thinking,
            metadata={},
            tool_names=[],
            supports_json_output=False,
        )
        # 验证嵌套对象的同一性 (identity)
        assert req.thinking is thinking
        assert req.messages[0] is messages[0]
        assert req.messages[1].type is CanonicalPartType.THINKING

    def test_compatibility_profile_with_mixed_statuses(self) -> None:
        """CompatibilityProfile 各维度可独立设置不同状态."""
        profile = CompatibilityProfile(
            thinking=CompatibilityStatus.NATIVE,
            tool_calling=CompatibilityStatus.SIMULATED,
            images=CompatibilityStatus.UNSAFE,
        )
        assert profile.thinking is CompatibilityStatus.NATIVE
        assert profile.tool_calling is CompatibilityStatus.SIMULATED
        assert profile.images is CompatibilityStatus.UNSAFE
        # 未显式设置的维度保持默认 UNKNOWN
        assert profile.mcp_tools is CompatibilityStatus.UNKNOWN

    def test_compatibility_decision_with_details(self) -> None:
        dec = CompatibilityDecision(
            status=CompatibilityStatus.SIMULATED,
            simulation_actions=["wrap_tool_use", "flatten_thinking"],
            unsupported_semantics=["streaming_tool_results"],
        )
        assert len(dec.simulation_actions) == 2
        assert "streaming_tool_results" in dec.unsupported_semantics


# ═══════════════════════════════════════════════════════════════
# 7. 边界情况: 空列表 / 空字典 / 默认枚举
# ═══════════════════════════════════════════════════════════════


class TestEdgeCases:
    """空集合、默认枚举值等边界场景."""

    def test_canonical_request_empty_messages(self) -> None:
        req = CanonicalRequest(
            session_key="sk_e", trace_id="tr_e", request_id="req_e", model="m",
            messages=[], thinking=CanonicalThinking(), metadata={}, tool_names=[],
            supports_json_output=False,
        )
        assert req.messages == []
        assert req.tool_names == []
        assert req.metadata == {}

    def test_canonical_tool_call_empty_arguments(self) -> None:
        tc = CanonicalToolCall(tool_id="x", name="noop")
        assert tc.arguments == {}

    def test_compatibility_trace_default_lists_are_empty(self) -> None:
        trace = CompatibilityTrace(
            trace_id="t", vendor="b", session_key="s",
            provider_protocol="p", compat_mode="c",
        )
        assert trace.simulation_actions == []
        assert trace.unsupported_semantics == []
        assert trace.request_adaptations == []

    def test_compat_session_record_default_dicts_are_empty(self) -> None:
        record = CompatSessionRecord(session_key="sk_edge")
        assert record.trace_id == ""
        assert record.tool_call_map == {}
        assert record.thought_signature_map == {}
        assert record.provider_state == {}
        assert record.state_version == 1
        assert record.updated_at_unix == 0

    def test_compat_session_record_is_mutable(self) -> None:
        """CompatSessionRecord 为非 frozen dataclass, 允许属性赋值."""
        record = CompatSessionRecord(session_key="sk_mut")
        record.state_version = 5
        record.updated_at_unix = 999
        assert record.state_version == 5
        assert record.updated_at_unix == 999

    def test_canonical_message_part_raw_block_preserved(self) -> None:
        raw = {"type": "text", "text": "original", "extra": 42}
        part = CanonicalMessagePart(
            type=CanonicalPartType.UNKNOWN, role="assistant", raw_block=raw,
        )
        assert part.raw_block == raw
        assert part.raw_block["extra"] == 42

    def test_canonical_thinking_partial_override(self) -> None:
        """仅覆盖部分字段, 其余保持默认."""
        t = CanonicalThinking(budget_tokens=4096)
        assert t.enabled is False  # 默认
        assert t.budget_tokens == 4096  # 覆盖
        assert t.effort is None  # 默认
