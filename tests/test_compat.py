from __future__ import annotations

from pathlib import Path

import pytest

from coding.proxy.compat.canonical import (
    CompatibilityStatus,
    build_canonical_request,
)
from coding.proxy.compat.session_store import CompatSessionRecord, CompatSessionStore
from coding.proxy.config.schema import ProxyConfig


def test_build_canonical_request_extracts_session_and_semantics():
    request = build_canonical_request(
        {
            "model": "claude-sonnet-4-20250514",
            "request_id": "req_123",
            "metadata": {"user_id": "user_123456"},
            "thinking": {"budget_tokens": 256, "effort": "medium"},
            "response_format": {"type": "json_object"},
            "tools": [{"name": "read_file"}],
            "messages": [
                {"role": "user", "content": [{"type": "text", "text": "hello"}]},
                {
                    "role": "assistant",
                    "content": [{"type": "tool_use", "id": "toolu_1", "name": "read_file", "input": {"path": "a"}}],
                },
            ],
        },
        {"x-session-id": "session_1"},
    )

    assert request.session_key == "session_1"
    assert request.request_id == "req_123"
    assert request.model == "claude-sonnet-4-20250514"
    assert request.thinking.enabled is True
    assert request.thinking.budget_tokens == 256
    assert request.supports_json_output is True
    assert request.tool_names == ["read_file"]
    assert len(request.messages) == 2


@pytest.mark.asyncio
async def test_compat_session_store_roundtrip(tmp_path: Path):
    store = CompatSessionStore(tmp_path / "compat.db", ttl_seconds=3600)
    await store.init()

    record = CompatSessionRecord(
        session_key="session_1",
        trace_id="trace_1",
        tool_call_map={"toolu_1": "call_1"},
        thought_signature_map={"sig_1": "provider_sig_1"},
        provider_state={"copilot": {"compat_mode": CompatibilityStatus.SIMULATED.value}},
    )
    await store.upsert(record)

    loaded = await store.get("session_1")

    assert loaded is not None
    assert loaded.trace_id == "trace_1"
    assert loaded.tool_call_map == {"toolu_1": "call_1"}
    assert loaded.thought_signature_map == {"sig_1": "provider_sig_1"}
    assert loaded.provider_state["copilot"]["compat_mode"] == CompatibilityStatus.SIMULATED.value

    await store.close()


def test_proxy_config_exposes_compat_state_path():
    cfg = ProxyConfig()
    assert str(cfg.compat_state_path).endswith(".coding-proxy/compat.db")
