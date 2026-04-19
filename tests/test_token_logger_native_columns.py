"""TokenLogger 原生 API 新列与幂等迁移测试.

覆盖矩阵:
1. 新装库直接包含 client_category / operation / endpoint / extra_usage_json 四列;
2. 旧库增量迁移幂等 (重复 init 不抛错);
3. log() 写入新字段可回读;
4. query_usage() 按 client_category / operation / endpoint 过滤;
5. 历史行默认 client_category='cc', operation='', endpoint='', extra_usage_json='{}';
6. _PERIOD_SQL 的 GROUP BY 附加 client_category/operation 后, 同 vendor 不同 op 分行聚合.
"""

from __future__ import annotations

import aiosqlite
import pytest
import pytest_asyncio

from coding.proxy.logging.db import TimePeriod, TokenLogger


@pytest_asyncio.fixture
async def logger(tmp_path):
    tl = TokenLogger(tmp_path / "test.db")
    await tl.init()
    yield tl
    await tl.close()


# ── 1. 新装库 schema ────────────────────────────────────────


@pytest.mark.asyncio
async def test_fresh_schema_contains_native_columns(logger):
    cursor = await logger._db.execute("PRAGMA table_info(usage_log)")
    rows = await cursor.fetchall()
    columns = {row["name"] for row in rows}
    assert {
        "client_category",
        "operation",
        "endpoint",
        "extra_usage_json",
    }.issubset(columns)


@pytest.mark.asyncio
async def test_fresh_schema_contains_new_indexes(logger):
    cursor = await logger._db.execute(
        "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='usage_log'"
    )
    rows = await cursor.fetchall()
    names = {row["name"] for row in rows}
    assert "idx_usage_client_category" in names
    assert "idx_usage_operation" in names


# ── 2. 旧库迁移幂等 ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_migration_from_legacy_db_idempotent(tmp_path):
    """模拟老版 DB: usage_log 无新四列 + 含历史行, 迁移后应补齐列并回填默认值."""
    db_path = tmp_path / "legacy.db"
    # 手动创建不含新列的老版 schema
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
                request_id TEXT DEFAULT ''
            );
            CREATE TABLE usage_evidence (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
                vendor TEXT NOT NULL,
                request_id TEXT DEFAULT '',
                model_served TEXT NOT NULL DEFAULT '',
                evidence_kind TEXT NOT NULL,
                raw_usage_json TEXT NOT NULL DEFAULT '{}',
                parsed_input_tokens INTEGER DEFAULT 0,
                parsed_output_tokens INTEGER DEFAULT 0,
                parsed_cache_creation_tokens INTEGER DEFAULT 0,
                parsed_cache_read_tokens INTEGER DEFAULT 0,
                cache_signal_present BOOLEAN NOT NULL DEFAULT 0,
                source_field_map_json TEXT NOT NULL DEFAULT '{}'
            );
            """
        )
        # 插入一条历史行（老字段覆盖完整）
        await db.execute(
            """INSERT INTO usage_log
               (vendor, model_requested, model_served, input_tokens, output_tokens)
               VALUES ('anthropic','claude-sonnet','claude-sonnet',100,50)"""
        )
        await db.commit()

    # 首次 init 触发迁移
    tl = TokenLogger(db_path)
    await tl.init()
    try:
        cursor = await tl._db.execute("PRAGMA table_info(usage_log)")
        rows = await cursor.fetchall()
        columns = {row["name"] for row in rows}
        for new_col in (
            "client_category",
            "operation",
            "endpoint",
            "extra_usage_json",
        ):
            assert new_col in columns, f"missing column after migration: {new_col}"

        # 历史行应自动得到默认值
        cursor = await tl._db.execute(
            "SELECT client_category, operation, endpoint, extra_usage_json "
            "FROM usage_log WHERE vendor='anthropic'"
        )
        row = await cursor.fetchone()
        assert row["client_category"] == "cc"
        assert row["operation"] == ""
        assert row["endpoint"] == ""
        assert row["extra_usage_json"] == "{}"
    finally:
        await tl.close()

    # 二次 init 应完全幂等, 不抛异常
    tl2 = TokenLogger(db_path)
    await tl2.init()
    await tl2.close()


# ── 3. log() 写入新字段 ─────────────────────────────────────


@pytest.mark.asyncio
async def test_log_persists_native_fields(logger):
    await logger.log(
        vendor="openai",
        model_requested="gpt-4o-mini",
        model_served="gpt-4o-mini",
        input_tokens=120,
        output_tokens=80,
        client_category="api",
        operation="chat",
        endpoint="/v1/chat/completions",
        extra_usage_json='{"reasoning_tokens":64}',
    )
    cursor = await logger._db.execute(
        "SELECT client_category, operation, endpoint, extra_usage_json "
        "FROM usage_log WHERE vendor='openai'"
    )
    row = await cursor.fetchone()
    assert row["client_category"] == "api"
    assert row["operation"] == "chat"
    assert row["endpoint"] == "/v1/chat/completions"
    assert row["extra_usage_json"] == '{"reasoning_tokens":64}'


@pytest.mark.asyncio
async def test_log_defaults_remain_cc(logger):
    """既有 caller 不传新字段 → client_category 默认为 'cc', 零回归."""
    await logger.log(
        vendor="anthropic",
        model_requested="claude-haiku",
        model_served="claude-haiku",
        input_tokens=50,
        output_tokens=10,
    )
    cursor = await logger._db.execute(
        "SELECT client_category, operation, endpoint, extra_usage_json FROM usage_log"
    )
    row = await cursor.fetchone()
    assert row["client_category"] == "cc"
    assert row["operation"] == ""
    assert row["endpoint"] == ""
    assert row["extra_usage_json"] == "{}"


# ── 4. query_usage() 过滤 ───────────────────────────────────


@pytest.mark.asyncio
async def test_query_usage_filter_by_client_category(logger):
    # 写入 cc 与 api 各一条
    await logger.log(
        vendor="anthropic",
        model_requested="claude-sonnet",
        model_served="claude-sonnet",
        input_tokens=100,
        output_tokens=50,
    )
    await logger.log(
        vendor="openai",
        model_requested="gpt-4o",
        model_served="gpt-4o",
        input_tokens=200,
        output_tokens=80,
        client_category="api",
        operation="chat",
        endpoint="/v1/chat/completions",
    )

    api_rows = await logger.query_usage(
        period=TimePeriod.TOTAL, count=0, client_category="api"
    )
    assert len(api_rows) == 1
    assert api_rows[0]["vendor"] == "openai"
    assert api_rows[0]["client_category"] == "api"
    assert api_rows[0]["operation"] == "chat"

    cc_rows = await logger.query_usage(
        period=TimePeriod.TOTAL, count=0, client_category="cc"
    )
    assert len(cc_rows) == 1
    assert cc_rows[0]["vendor"] == "anthropic"


@pytest.mark.asyncio
async def test_query_usage_filter_by_operation_and_endpoint(logger):
    await logger.log(
        vendor="openai",
        model_requested="gpt-4o",
        model_served="gpt-4o",
        input_tokens=100,
        output_tokens=50,
        client_category="api",
        operation="chat",
        endpoint="/v1/chat/completions",
    )
    await logger.log(
        vendor="openai",
        model_requested="text-embedding-3-small",
        model_served="text-embedding-3-small",
        input_tokens=200,
        output_tokens=0,
        client_category="api",
        operation="embedding",
        endpoint="/v1/embeddings",
    )

    chat_rows = await logger.query_usage(
        period=TimePeriod.TOTAL, count=0, operation="chat"
    )
    assert len(chat_rows) == 1
    assert chat_rows[0]["operation"] == "chat"

    embed_rows = await logger.query_usage(
        period=TimePeriod.TOTAL, count=0, endpoint="/v1/embeddings"
    )
    assert len(embed_rows) == 1
    assert embed_rows[0]["operation"] == "embedding"


@pytest.mark.asyncio
async def test_query_usage_list_filter_multi_operations(logger):
    for op, ep in (
        ("chat", "/v1/chat/completions"),
        ("embedding", "/v1/embeddings"),
        ("moderation", "/v1/moderations"),
    ):
        await logger.log(
            vendor="openai",
            model_requested="x",
            model_served="x",
            input_tokens=10,
            output_tokens=5,
            client_category="api",
            operation=op,
            endpoint=ep,
        )

    rows = await logger.query_usage(
        period=TimePeriod.TOTAL,
        count=0,
        operation=["chat", "embedding"],
    )
    ops = {r["operation"] for r in rows}
    assert ops == {"chat", "embedding"}


# ── 5. GROUP BY 附带 client_category/operation ──────────────


@pytest.mark.asyncio
async def test_group_by_separates_categories(logger):
    """同 vendor / 同 model 但不同 client_category 或 operation 应分行聚合."""
    for cat, op in (("cc", ""), ("api", "chat"), ("api", "embedding")):
        await logger.log(
            vendor="openai",
            model_requested="m",
            model_served="m",
            input_tokens=10,
            output_tokens=5,
            client_category=cat,
            operation=op,
        )

    rows = await logger.query_usage(period=TimePeriod.TOTAL, count=0)
    # 预期三行: (cc,''), (api,chat), (api,embedding)
    key_set = {(r["client_category"], r["operation"]) for r in rows}
    assert key_set == {("cc", ""), ("api", "chat"), ("api", "embedding")}
    for row in rows:
        assert row["total_requests"] == 1  # 未被错误聚合


# ── 6. 聚合字段回传 ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_query_usage_returns_client_category_and_operation(logger):
    await logger.log(
        vendor="gemini",
        model_requested="gemini-2.0-flash",
        model_served="gemini-2.0-flash",
        input_tokens=100,
        output_tokens=40,
        client_category="api",
        operation="generate_content",
        endpoint="/v1beta/models/gemini-2.0-flash:generateContent",
    )
    rows = await logger.query_usage(period=TimePeriod.TOTAL, count=0)
    assert len(rows) == 1
    assert rows[0]["client_category"] == "api"
    assert rows[0]["operation"] == "generate_content"
