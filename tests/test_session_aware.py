"""Session-Aware Architecture 测试.

覆盖矩阵:
1. 新装库 schema 包含 session_key 列与索引;
2. 旧库增量迁移幂等 (重复 init 不抛错);
3. log() 写入 session_key 可回读;
4. query_recent_sessions() 聚合/排序/过滤正确性;
5. query_session_profile() 单会话查询;
6. SessionPolicyResolver 精确匹配/通配匹配/无匹配;
7. _resolve_effective_tiers 策略 tier 重排逻辑.
"""

from __future__ import annotations

import aiosqlite
import pytest
import pytest_asyncio

from coding.proxy.config.session_policy import (
    SessionPoliciesConfig,
    SessionPolicy,
    SessionPolicyMatch,
)
from coding.proxy.logging.db import TokenLogger
from coding.proxy.routing.session_policy import SessionPolicyResolver

# ── Fixture ──────────────────────────────────────────────────


@pytest_asyncio.fixture
async def logger(tmp_path):
    tl = TokenLogger(tmp_path / "test.db")
    await tl.init()
    yield tl
    await tl.close()


# ── 1. 新装库 schema ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_fresh_schema_contains_session_key(logger):
    cursor = await logger._db.execute("PRAGMA table_info(usage_log)")
    rows = await cursor.fetchall()
    columns = {row["name"] for row in rows}
    assert "session_key" in columns


@pytest.mark.asyncio
async def test_fresh_schema_contains_session_key_index(logger):
    cursor = await logger._db.execute(
        "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='usage_log'"
    )
    rows = await cursor.fetchall()
    names = {row["name"] for row in rows}
    assert "idx_usage_session_key" in names


# ── 2. 旧库迁移幂等 ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_migration_from_legacy_db_adds_session_key(tmp_path):
    db_path = tmp_path / "legacy.db"
    async with aiosqlite.connect(str(db_path)) as db:
        await db.executescript(
            """
            CREATE TABLE usage_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
                vendor TEXT NOT NULL,
                model_requested TEXT NOT NULL,
                model_served TEXT NOT NULL,
                input_tokens INTEGER DEFAULT 0,
                output_tokens INTEGER DEFAULT 0,
                cache_creation_tokens INTEGER DEFAULT 0,
                cache_read_tokens INTEGER DEFAULT 0,
                duration_ms INTEGER DEFAULT 0,
                success BOOLEAN NOT NULL DEFAULT 1,
                failover BOOLEAN NOT NULL DEFAULT 0,
                failover_from TEXT DEFAULT NULL,
                request_id TEXT DEFAULT '',
                client_category TEXT NOT NULL DEFAULT 'cc',
                operation TEXT NOT NULL DEFAULT '',
                endpoint TEXT NOT NULL DEFAULT '',
                extra_usage_json TEXT NOT NULL DEFAULT '{}'
            );
            """
        )
        await db.execute(
            "INSERT INTO usage_log (vendor, model_requested, model_served) "
            "VALUES ('anthropic','claude-sonnet','claude-sonnet')"
        )
        await db.commit()

    tl = TokenLogger(db_path)
    await tl.init()
    try:
        cursor = await tl._db.execute("PRAGMA table_info(usage_log)")
        rows = await cursor.fetchall()
        columns = {row["name"] for row in rows}
        assert "session_key" in columns

        cursor = await tl._db.execute(
            "SELECT session_key FROM usage_log WHERE vendor='anthropic'"
        )
        row = await cursor.fetchone()
        assert row["session_key"] == ""
    finally:
        await tl.close()

    tl2 = TokenLogger(db_path)
    await tl2.init()
    await tl2.close()


# ── 3. log() 写入 session_key ───────────────────────────────


@pytest.mark.asyncio
async def test_log_persists_session_key(logger):
    await logger.log(
        vendor="anthropic",
        model_requested="claude-sonnet",
        model_served="claude-sonnet",
        input_tokens=100,
        output_tokens=50,
        session_key="test-session-123",
    )
    cursor = await logger._db.execute(
        "SELECT session_key FROM usage_log WHERE vendor='anthropic'"
    )
    row = await cursor.fetchone()
    assert row["session_key"] == "test-session-123"


@pytest.mark.asyncio
async def test_log_default_session_key_empty(logger):
    await logger.log(
        vendor="anthropic",
        model_requested="claude-sonnet",
        model_served="claude-sonnet",
        input_tokens=50,
        output_tokens=10,
    )
    cursor = await logger._db.execute("SELECT session_key FROM usage_log")
    row = await cursor.fetchone()
    assert row["session_key"] == ""


# ── 4. query_recent_sessions ────────────────────────────────


@pytest.mark.asyncio
async def test_query_recent_sessions_basic(logger):
    for i in range(3):
        await logger.log(
            vendor="anthropic",
            model_requested="claude-sonnet",
            model_served="claude-sonnet",
            input_tokens=100 * (i + 1),
            output_tokens=50 * (i + 1),
            session_key="session-alpha",
            duration_ms=100 + i * 50,
        )
    await logger.log(
        vendor="copilot",
        model_requested="claude-sonnet",
        model_served="gpt-4o",
        input_tokens=200,
        output_tokens=80,
        session_key="session-beta",
        duration_ms=150,
    )
    await logger.log(
        vendor="zhipu",
        model_requested="claude-sonnet",
        model_served="glm-5v-turbo",
        input_tokens=50,
        output_tokens=20,
        session_key="",  # 空 key，应被排除
    )

    sessions = await logger.query_recent_sessions(limit=10, hours=1)
    assert len(sessions) == 2

    alpha = next(s for s in sessions if s["session_key"] == "session-alpha")
    assert alpha["total_requests"] == 3
    assert alpha["total_tokens"] == (100 + 200 + 300) + (50 + 100 + 150)
    assert alpha["total_input"] == 100 + 200 + 300
    assert alpha["total_output"] == 50 + 100 + 150
    assert "claude-sonnet" in alpha["models"]
    assert "anthropic" in alpha["vendors"]
    assert alpha["success_rate"] == 100.0
    assert "cc" in alpha["client_categories"]


@pytest.mark.asyncio
async def test_query_recent_sessions_excludes_empty_key(logger):
    await logger.log(
        vendor="anthropic",
        model_requested="claude-sonnet",
        model_served="claude-sonnet",
        input_tokens=100,
        session_key="",
    )
    await logger.log(
        vendor="anthropic",
        model_requested="claude-sonnet",
        model_served="claude-sonnet",
        input_tokens=100,
        session_key="valid-session",
    )
    sessions = await logger.query_recent_sessions(limit=10, hours=1)
    assert len(sessions) == 1
    assert sessions[0]["session_key"] == "valid-session"


@pytest.mark.asyncio
async def test_query_recent_sessions_limit(logger):
    for i in range(5):
        await logger.log(
            vendor="anthropic",
            model_requested="claude-sonnet",
            model_served="claude-sonnet",
            input_tokens=100,
            session_key=f"session-{i}",
        )
    sessions = await logger.query_recent_sessions(limit=3, hours=1)
    assert len(sessions) == 3


@pytest.mark.asyncio
async def test_query_recent_sessions_success_rate(logger):
    await logger.log(
        vendor="anthropic",
        model_requested="m",
        model_served="m",
        session_key="s1",
        success=True,
    )
    await logger.log(
        vendor="anthropic",
        model_requested="m",
        model_served="m",
        session_key="s1",
        success=False,
    )
    await logger.log(
        vendor="anthropic",
        model_requested="m",
        model_served="m",
        session_key="s1",
        success=True,
    )
    sessions = await logger.query_recent_sessions(limit=10, hours=1)
    assert len(sessions) == 1
    assert abs(sessions[0]["success_rate"] - (2 / 3 * 100)) < 0.01


# ── 5. query_session_profile ────────────────────────────────


@pytest.mark.asyncio
async def test_query_session_profile_found(logger):
    await logger.log(
        vendor="anthropic",
        model_requested="m",
        model_served="m",
        input_tokens=100,
        output_tokens=50,
        session_key="profile-test",
    )
    profile = await logger.query_session_profile("profile-test")
    assert profile is not None
    assert profile["session_key"] == "profile-test"
    assert profile["total_requests"] == 1


@pytest.mark.asyncio
async def test_query_session_profile_not_found(logger):
    profile = await logger.query_session_profile("nonexistent")
    assert profile is None


# ── 6. SessionPolicyResolver ────────────────────────────────


def _make_policy(name, keys=None, category=None, tiers=None):
    return SessionPolicy(
        name=name,
        match=SessionPolicyMatch(session_keys=keys or [], client_category=category),
        tiers=tiers or [],
    )


def test_resolve_by_session_key():
    p1 = _make_policy("vip", keys=["key-1", "key-2"], tiers=["anthropic"])
    p2 = _make_policy("cc-default", category="cc", tiers=["copilot"])
    resolver = SessionPolicyResolver([p1, p2])

    assert resolver.resolve("key-1") is p1
    assert resolver.resolve("key-2") is p1
    assert resolver.resolve("unknown-key", "cc") is p2
    assert resolver.resolve("unknown-key", "api") is None


def test_resolve_key_priority_over_category():
    p1 = _make_policy("cc-default", category="cc", tiers=["copilot"])
    p2 = _make_policy("vip", keys=["vip-key"], tiers=["anthropic"])
    resolver = SessionPolicyResolver([p1, p2])

    result = resolver.resolve("vip-key", "cc")
    assert result is p2  # 精确 key 匹配优先


def test_resolve_no_match():
    resolver = SessionPolicyResolver([])
    assert resolver.resolve("any-key") is None


def test_resolve_first_match_wins():
    p1 = _make_policy("first", keys=["dup-key"], tiers=["anthropic"])
    p2 = _make_policy("second", keys=["dup-key"], tiers=["zhipu"])
    resolver = SessionPolicyResolver([p1, p2])

    assert resolver.resolve("dup-key") is p1


def test_empty_resolver():
    resolver = SessionPolicyResolver()
    assert resolver.resolve("any") is None


# ── 7. _resolve_effective_tiers (via executor) ──────────────


def test_resolve_effective_tiers_with_policy():
    from coding.proxy.routing.executor import _RouteExecutor
    from coding.proxy.routing.tier import VendorTier
    from coding.proxy.vendors.base import BaseVendor

    class FakeVendor(BaseVendor):
        def __init__(self, name):
            self._name = name

        def get_name(self):
            return self._name

        async def _prepare_request(self, body, headers):
            return body, headers

        async def send_message_stream(self, body, headers):
            yield b"", ""

        async def send_message(self, body, headers):
            return None

        def supports_request(self, caps):
            return True, []

        def map_model(self, model):
            return model

    tiers = [
        VendorTier(vendor=FakeVendor("zhipu")),
        VendorTier(vendor=FakeVendor("anthropic")),
        VendorTier(vendor=FakeVendor("copilot")),
    ]

    policy = _make_policy("vip", keys=["vip-key"], tiers=["anthropic", "copilot"])
    resolver = SessionPolicyResolver([policy])

    executor = _RouteExecutor(
        router=None,
        tiers=tiers,
        usage_recorder=None,
        session_manager=None,
        session_policy_resolver=resolver,
    )

    effective = executor._resolve_effective_tiers("vip-key")
    names = [t.name for t in effective]
    assert names == ["anthropic", "copilot", "zhipu"]


def test_resolve_effective_tiers_no_policy():
    from coding.proxy.routing.executor import _RouteExecutor
    from coding.proxy.routing.tier import VendorTier
    from coding.proxy.vendors.base import BaseVendor

    class FakeVendor(BaseVendor):
        def __init__(self, name):
            self._name = name

        def get_name(self):
            return self._name

        async def _prepare_request(self, body, headers):
            return body, headers

        async def send_message_stream(self, body, headers):
            yield b"", ""

        async def send_message(self, body, headers):
            return None

        def supports_request(self, caps):
            return True, []

        def map_model(self, model):
            return model

    tiers = [
        VendorTier(vendor=FakeVendor("zhipu")),
        VendorTier(vendor=FakeVendor("anthropic")),
    ]

    executor = _RouteExecutor(
        router=None,
        tiers=tiers,
        usage_recorder=None,
        session_manager=None,
    )

    effective = executor._resolve_effective_tiers("unknown-key")
    assert effective is tiers  # 返回同一列表引用


# ── 8. SessionPoliciesConfig 集成 ───────────────────────────


def test_config_default_empty():
    config = SessionPoliciesConfig()
    assert config.policies == []


def test_config_parse():
    config = SessionPoliciesConfig(
        policies=[
            {
                "name": "vip",
                "match": {"session_keys": ["key-1"]},
                "tiers": ["anthropic", "copilot"],
            }
        ]
    )
    assert len(config.policies) == 1
    assert config.policies[0].name == "vip"
    assert config.policies[0].match.session_keys == ["key-1"]
    assert config.policies[0].tiers == ["anthropic", "copilot"]


# ── 9. SessionPolicyResolver 运行时可变性 ────────────────────────


def test_runtime_upsert_and_resolve():
    resolver = SessionPolicyResolver()
    assert resolver.resolve("my-session") is None

    resolver.upsert("my-session", ["anthropic", "copilot"])
    policy = resolver.resolve("my-session")
    assert policy is not None
    assert policy.tiers == ["anthropic", "copilot"]
    assert policy.name.startswith("runtime:")


def test_runtime_upsert_overwrites():
    resolver = SessionPolicyResolver()
    resolver.upsert("my-session", ["anthropic"])
    resolver.upsert("my-session", ["copilot", "zhipu"])
    policy = resolver.resolve("my-session")
    assert policy.tiers == ["copilot", "zhipu"]


def test_runtime_remove():
    resolver = SessionPolicyResolver()
    resolver.upsert("my-session", ["anthropic"])
    assert resolver.remove("my-session") is True
    assert resolver.resolve("my-session") is None
    assert resolver.remove("my-session") is False


def test_runtime_remove_does_not_affect_config_policy():
    p = _make_policy("config-policy", keys=["config-key"], tiers=["anthropic"])
    resolver = SessionPolicyResolver([p])
    # Cannot remove config-driven policy via runtime API
    assert resolver.remove("config-key") is False
    assert resolver.resolve("config-key") is p


def test_runtime_upsert_overrides_config_policy():
    p = _make_policy("config-policy", keys=["shared-key"], tiers=["anthropic"])
    resolver = SessionPolicyResolver([p])
    resolver.upsert("shared-key", ["copilot"])
    # Runtime binding takes precedence (replaces in key_index)
    policy = resolver.resolve("shared-key")
    assert policy.tiers == ["copilot"]
    assert policy.name.startswith("runtime:")


def test_list_runtime_bindings():
    resolver = SessionPolicyResolver()
    p = _make_policy("config-policy", keys=["config-key"], tiers=["anthropic"])
    resolver = SessionPolicyResolver([p])
    resolver.upsert("runtime-1", ["copilot"])
    resolver.upsert("runtime-2", ["zhipu", "anthropic"])

    bindings = resolver.list_runtime_bindings()
    assert len(bindings) == 2
    keys = {b["session_key"] for b in bindings}
    assert keys == {"runtime-1", "runtime-2"}
    # Config-driven policy should not appear
    assert "config-key" not in keys


def test_runtime_upsert_integrates_with_executor():
    from coding.proxy.routing.executor import _RouteExecutor
    from coding.proxy.routing.tier import VendorTier
    from coding.proxy.vendors.base import BaseVendor

    class FakeVendor(BaseVendor):
        def __init__(self, name):
            self._name = name

        def get_name(self):
            return self._name

        async def _prepare_request(self, body, headers):
            return body, headers

        async def send_message_stream(self, body, headers):
            yield b"", ""

        async def send_message(self, body, headers):
            return None

        def supports_request(self, caps):
            return True, []

        def map_model(self, model):
            return model

    tiers = [
        VendorTier(vendor=FakeVendor("zhipu")),
        VendorTier(vendor=FakeVendor("anthropic")),
        VendorTier(vendor=FakeVendor("copilot")),
    ]

    resolver = SessionPolicyResolver()
    executor = _RouteExecutor(
        router=None,
        tiers=tiers,
        usage_recorder=None,
        session_manager=None,
        session_policy_resolver=resolver,
    )

    # Before binding: default order
    assert [t.name for t in executor._resolve_effective_tiers("test")] == [
        "zhipu",
        "anthropic",
        "copilot",
    ]

    # After binding: anthropic first, copilot second, zhipu last
    resolver.upsert("test", ["anthropic", "copilot"])
    assert [t.name for t in executor._resolve_effective_tiers("test")] == [
        "anthropic",
        "copilot",
        "zhipu",
    ]

    # After unbind: back to default
    resolver.remove("test")
    assert [t.name for t in executor._resolve_effective_tiers("test")] == [
        "zhipu",
        "anthropic",
        "copilot",
    ]
