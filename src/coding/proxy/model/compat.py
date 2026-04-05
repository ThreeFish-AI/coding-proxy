"""兼容层抽象类型 — 供应商无关的 Claude/Anthropic 语义模型.

从 :mod:`coding.proxy.compat.canonical` 和
:mod:`coding.proxy.compat.session_store` 正交提取纯声明式类型定义。
构建逻辑（如 ``build_canonical_request()``）和持久化管理器（如
``CompatSessionStore``）保留在原模块。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

# ═══════════════════════════════════════════════════════════════
# 消息部分类型体系
# ═══════════════════════════════════════════════════════════════


class CanonicalPartType(StrEnum):
    """规范消息部分的类型枚举."""

    TEXT = "text"
    THINKING = "thinking"
    IMAGE = "image"
    TOOL_USE = "tool_use"
    TOOL_RESULT = "tool_result"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class CanonicalThinking:
    """思考（extended thinking）能力参数."""

    enabled: bool = False
    budget_tokens: int | None = None
    effort: str | None = None
    source_field: str | None = None


@dataclass(frozen=True)
class CanonicalToolCall:
    """工具调用记录."""

    tool_id: str
    name: str
    arguments: dict[str, Any] = field(default_factory=dict)
    provider_tool_id: str | None = None
    provider_kind: str = "function"


@dataclass(frozen=True)
class CanonicalMessagePart:
    """规范化的消息内容块."""

    type: CanonicalPartType
    role: str
    text: str = ""
    tool_call: CanonicalToolCall | None = None
    tool_result_id: str | None = None
    raw_block: dict[str, Any] | None = None


@dataclass(frozen=True)
class CanonicalRequest:
    """规范化的完整请求抽象."""

    session_key: str
    trace_id: str
    request_id: str
    model: str
    messages: list[CanonicalMessagePart]
    thinking: CanonicalThinking
    metadata: dict[str, Any]
    tool_names: list[str]
    supports_json_output: bool


# ═══════════════════════════════════════════════════════════════
# 兼容性评估类型体系
# ═══════════════════════════════════════════════════════════════


class CompatibilityStatus(StrEnum):
    """供应商对某语义特性的兼容状态."""

    NATIVE = "native"
    SIMULATED = "simulated"
    UNSAFE = "unsafe"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class CompatibilityProfile:
    """供应商各维度的兼容性画像."""

    thinking: CompatibilityStatus = CompatibilityStatus.UNKNOWN
    tool_calling: CompatibilityStatus = CompatibilityStatus.UNKNOWN
    tool_streaming: CompatibilityStatus = CompatibilityStatus.UNKNOWN
    mcp_tools: CompatibilityStatus = CompatibilityStatus.UNKNOWN
    images: CompatibilityStatus = CompatibilityStatus.UNKNOWN
    metadata: CompatibilityStatus = CompatibilityStatus.UNKNOWN
    json_output: CompatibilityStatus = CompatibilityStatus.UNKNOWN
    usage_tokens: CompatibilityStatus = CompatibilityStatus.UNKNOWN


@dataclass(frozen=True)
class CompatibilityDecision:
    """单次请求的兼容性决策结果."""

    status: CompatibilityStatus
    simulation_actions: list[str] = field(default_factory=list)
    unsupported_semantics: list[str] = field(default_factory=list)


@dataclass
class CompatibilityTrace:
    """兼容性处理链路追踪记录."""

    trace_id: str
    vendor: str
    session_key: str
    provider_protocol: str
    compat_mode: str
    simulation_actions: list[str] = field(default_factory=list)
    unsupported_semantics: list[str] = field(default_factory=list)
    session_state_hits: int = 0
    request_adaptations: list[str] = field(default_factory=list)
    generated_at_unix: int = field(
        default_factory=lambda: int(__import__("time").time())
    )

    def to_dict(self) -> dict[str, Any]:
        from dataclasses import asdict

        return asdict(self)


# ═══════════════════════════════════════════════════════════════
# 会话状态记录
# ═══════════════════════════════════════════════════════════════


@dataclass
class CompatSessionRecord:
    """兼容层会话持久化记录."""

    session_key: str
    trace_id: str = ""
    tool_call_map: dict[str, str] = field(default_factory=dict)
    thought_signature_map: dict[str, str] = field(default_factory=dict)
    provider_state: dict[str, Any] = field(default_factory=dict)
    state_version: int = 1
    updated_at_unix: int = 0
