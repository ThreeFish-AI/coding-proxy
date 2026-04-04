"""路由会话管理器 — 封装兼容性会话的创建、上下文应用与持久化."""

from __future__ import annotations

from typing import Any

from ..compat.canonical import (
    CompatibilityStatus,
    CompatibilityTrace,
)
from ..compat.session_store import CompatSessionRecord, CompatSessionStore
from .tier import VendorTier


class RouteSessionManager:
    """管理单次路由请求的兼容性会话生命周期."""

    def __init__(self, compat_session_store: CompatSessionStore | None = None) -> None:
        self._store = compat_session_store

    async def get_or_create_record(self, session_key: str, trace_id: str) -> CompatSessionRecord | None:
        if self._store is None:
            return None
        record = await self._store.get(session_key)
        if record is not None:
            return record
        return CompatSessionRecord(session_key=session_key, trace_id=trace_id)

    def apply_compat_context(
        self,
        *,
        tier: VendorTier,
        canonical_request: Any,
        decision: Any,
        session_record: CompatSessionRecord | None,
    ) -> None:
        provider_protocol = {
            "copilot": "openai_chat_completions",
            "antigravity": "gemini_generate_content",
            "zhipu": "anthropic_messages",
            "anthropic": "anthropic_messages",
        }.get(tier.name, "unknown")
        compat_trace = CompatibilityTrace(
            trace_id=canonical_request.trace_id, vendor=tier.name,
            session_key=canonical_request.session_key, provider_protocol=provider_protocol,
            compat_mode=decision.status.value, simulation_actions=list(decision.simulation_actions),
            unsupported_semantics=list(decision.unsupported_semantics),
            session_state_hits=1 if session_record else 0, request_adaptations=[],
        )
        tier.vendor.set_compat_context(trace=compat_trace, session_record=session_record)

    async def persist_session(self, trace: CompatibilityTrace | None, session_record: CompatSessionRecord | None) -> None:
        if self._store is None or trace is None or session_record is None:
            return
        provider_states = dict(session_record.provider_state)
        provider_states[trace.vendor] = {
            "compat_mode": trace.compat_mode, "simulation_actions": trace.simulation_actions,
            "unsupported_semantics": trace.unsupported_semantics, "trace_id": trace.trace_id,
        }
        session_record.trace_id = trace.trace_id
        session_record.provider_state = provider_states
        await self._store.upsert(session_record)
