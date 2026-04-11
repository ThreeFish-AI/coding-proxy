"""TokenLogger 查询扩展单元测试."""

import aiosqlite
import pytest
import pytest_asyncio

from coding.proxy.logging.db import TokenLogger


@pytest_asyncio.fixture
async def logger(tmp_path):
    tl = TokenLogger(tmp_path / "test.db")
    await tl.init()
    yield tl
    await tl.close()


@pytest.mark.asyncio
async def test_query_window_total_empty(logger):
    total = await logger.query_window_total(5.0)
    assert total == 0


@pytest.mark.asyncio
async def test_log_evidence_and_query_by_request_id(logger):
    await logger.log_evidence(
        vendor="copilot",
        request_id="req_cache_1",
        model_served="claude-sonnet-4",
        evidence_kind="data_usage",
        raw_usage_json='{"cache_read_input_tokens":42,"prompt_tokens":100}',
        parsed_input_tokens=100,
        parsed_output_tokens=20,
        parsed_cache_read_tokens=42,
        cache_signal_present=True,
        source_field_map_json='{"cache_read_tokens":"cache_read_input_tokens"}',
    )

    rows = await logger.query_evidence("req_cache_1")
    assert len(rows) == 1
    assert rows[0]["vendor"] == "copilot"
    assert rows[0]["evidence_kind"] == "data_usage"
    assert rows[0]["parsed_cache_read_tokens"] == 42
    assert rows[0]["cache_signal_present"] == 1


@pytest.mark.asyncio
async def test_query_window_total_sums_correctly(logger):
    await logger.log(
        vendor="anthropic",
        model_requested="claude-sonnet-4",
        model_served="claude-sonnet-4",
        input_tokens=100,
        output_tokens=50,
        success=True,
    )
    await logger.log(
        vendor="anthropic",
        model_requested="claude-sonnet-4",
        model_served="claude-sonnet-4",
        input_tokens=200,
        output_tokens=80,
        success=True,
    )
    total = await logger.query_window_total(5.0)
    assert total == 430  # (100+50) + (200+80)


@pytest.mark.asyncio
async def test_query_window_total_filters_vendor(logger):
    await logger.log(
        vendor="anthropic",
        model_requested="claude-sonnet-4",
        model_served="claude-sonnet-4",
        input_tokens=100,
        output_tokens=50,
        success=True,
    )
    await logger.log(
        vendor="zhipu",
        model_requested="claude-sonnet-4",
        model_served="glm-5.1",
        input_tokens=200,
        output_tokens=80,
        success=True,
    )
    total = await logger.query_window_total(5.0, vendor="anthropic")
    assert total == 150  # 仅 anthropic


@pytest.mark.asyncio
async def test_query_window_total_excludes_failures(logger):
    await logger.log(
        vendor="anthropic",
        model_requested="claude-sonnet-4",
        model_served="claude-sonnet-4",
        input_tokens=100,
        output_tokens=50,
        success=True,
    )
    await logger.log(
        vendor="anthropic",
        model_requested="claude-sonnet-4",
        model_served="claude-sonnet-4",
        input_tokens=500,
        output_tokens=0,
        success=False,
    )
    total = await logger.query_window_total(5.0)
    assert total == 150  # 失败请求不计入


@pytest.mark.asyncio
async def test_query_daily_groups_by_model(logger):
    """query_daily 应按 model_requested 和 model_served 分组."""
    await logger.log(
        vendor="anthropic",
        model_requested="claude-sonnet-4",
        model_served="claude-sonnet-4",
        input_tokens=100,
        output_tokens=50,
    )
    await logger.log(
        vendor="anthropic",
        model_requested="claude-opus-4",
        model_served="claude-opus-4",
        input_tokens=200,
        output_tokens=80,
    )
    await logger.log(
        vendor="anthropic",
        model_requested="claude-sonnet-4",
        model_served="claude-sonnet-4",
        input_tokens=150,
        output_tokens=60,
    )
    rows = await logger.query_daily(days=7)
    assert len(rows) == 2
    models = {r["model_requested"] for r in rows}
    assert models == {"claude-sonnet-4", "claude-opus-4"}
    # 验证 sonnet 聚合正确
    sonnet = next(r for r in rows if r["model_requested"] == "claude-sonnet-4")
    assert sonnet["total_requests"] == 2
    assert sonnet["total_input"] == 250
    assert sonnet["total_output"] == 110


@pytest.mark.asyncio
async def test_query_daily_model_filter(logger):
    """query_daily 的 model 参数应正确过滤."""
    await logger.log(
        vendor="anthropic",
        model_requested="claude-sonnet-4",
        model_served="claude-sonnet-4",
        input_tokens=100,
        output_tokens=50,
    )
    await logger.log(
        vendor="anthropic",
        model_requested="claude-opus-4",
        model_served="claude-opus-4",
        input_tokens=200,
        output_tokens=80,
    )
    rows = await logger.query_daily(days=7, model="claude-opus-4")
    assert len(rows) == 1
    assert rows[0]["model_requested"] == "claude-opus-4"
    assert rows[0]["total_requests"] == 1


@pytest.mark.asyncio
async def test_query_daily_shows_model_mapping(logger):
    """故障转移场景：model_requested 与 model_served 不同时应分别展示."""
    await logger.log(
        vendor="zhipu",
        model_requested="claude-sonnet-4",
        model_served="glm-5.1",
        input_tokens=300,
        output_tokens=100,
        failover=True,
        failover_from="anthropic",
    )
    await logger.log(
        vendor="anthropic",
        model_requested="claude-sonnet-4",
        model_served="claude-sonnet-4",
        input_tokens=100,
        output_tokens=50,
    )
    rows = await logger.query_daily(days=7)
    assert len(rows) == 2
    zhipu_row = next(r for r in rows if r["vendor"] == "zhipu")
    assert zhipu_row["model_requested"] == "claude-sonnet-4"
    assert zhipu_row["model_served"] == "glm-5.1"
    assert zhipu_row["total_failovers"] == 1


@pytest.mark.asyncio
async def test_log_with_failover_from(logger):
    """log() 接受 failover_from 参数并正确写入数据库."""
    await logger.log(
        vendor="zhipu",
        model_requested="claude-sonnet-4",
        model_served="glm-5.1",
        input_tokens=100,
        output_tokens=50,
        failover=True,
        failover_from="anthropic",
    )
    rows = await logger.query_daily(days=7)
    assert len(rows) == 1
    assert rows[0]["total_failovers"] == 1
    # failover_from 通过 query_failover_stats 验证
    stats = await logger.query_failover_stats(days=7)
    assert len(stats) == 1
    assert stats[0]["failover_from"] == "anthropic"


@pytest.mark.asyncio
async def test_log_without_failover_from(logger):
    """不传 failover_from 时默认为 None."""
    await logger.log(
        vendor="anthropic",
        model_requested="claude-sonnet-4",
        model_served="claude-sonnet-4",
        input_tokens=100,
        output_tokens=50,
    )
    rows = await logger.query_daily(days=7)
    assert len(rows) == 1
    stats = await logger.query_failover_stats(days=7)
    assert len(stats) == 0


@pytest.mark.asyncio
async def test_query_daily_merges_failover_rows(logger):
    """query_daily no longer groups by failover_from, rows merge."""
    await logger.log(
        vendor="zhipu",
        model_requested="claude-sonnet-4",
        model_served="claude-sonnet-4",
        input_tokens=100,
        failover=True,
        failover_from="anthropic",
    )
    await logger.log(
        vendor="zhipu",
        model_requested="claude-sonnet-4",
        model_served="claude-sonnet-4",
        input_tokens=200,
    )
    rows = await logger.query_daily(days=7)
    assert len(rows) == 1
    assert rows[0]["total_requests"] == 2
    assert rows[0]["total_input"] == 300
    assert rows[0]["total_failovers"] == 1


@pytest.mark.asyncio
async def test_migration_adds_failover_from_column(tmp_path):
    """旧数据库（无 failover_from 列）迁移后新列存在."""

    db_path = tmp_path / "old.db"
    # 创建旧表（不含 failover_from，使用 vendor 列名）
    db = await aiosqlite.connect(str(db_path))
    await db.execute("""CREATE TABLE usage_log (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ts TEXT NOT NULL DEFAULT 'now',
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
        request_id TEXT DEFAULT ''
    )""")
    # 插入一条旧数据
    await db.execute(
        "INSERT INTO usage_log (vendor, model_requested, model_served) VALUES (?, ?, ?)",
        ("anthropic", "claude-sonnet-4", "claude-sonnet-4"),
    )
    await db.commit()
    await db.close()

    # 用 TokenLogger 重新 init（触发迁移）
    tl = TokenLogger(db_path)
    await tl.init()
    rows = await tl.query_daily(days=7)
    assert len(rows) == 1
    await tl.close()


@pytest.mark.asyncio
async def test_query_failover_stats(logger):
    """query_failover_stats 按来源→目标聚合故障转移次数."""
    await logger.log(
        vendor="zhipu",
        model_requested="claude-sonnet-4",
        model_served="glm-5.1",
        failover=True,
        failover_from="anthropic",
    )
    await logger.log(
        vendor="zhipu",
        model_requested="claude-opus-4",
        model_served="glm-5.1",
        failover=True,
        failover_from="anthropic",
    )
    await logger.log(
        vendor="zhipu",
        model_requested="claude-sonnet-4",
        model_served="claude-sonnet-4",
        failover=True,
        failover_from="copilot",
    )
    await logger.log(
        vendor="anthropic",
        model_requested="claude-sonnet-4",
        model_served="claude-sonnet-4",
        # 非 failover，不应出现在统计中
    )
    stats = await logger.query_failover_stats(days=7)
    assert len(stats) == 2
    anthropic_to_zhipu = next(s for s in stats if s["failover_from"] == "anthropic")
    assert anthropic_to_zhipu["vendor"] == "zhipu"
    assert anthropic_to_zhipu["count"] == 2
    copilot_to_zhipu = next(s for s in stats if s["failover_from"] == "copilot")
    assert copilot_to_zhipu["count"] == 1


# ---------------------------------------------------------------------------
# 天数边界与时区修正测试
# ---------------------------------------------------------------------------

from datetime import UTC, datetime, timedelta
from unittest.mock import patch
from zoneinfo import ZoneInfo

_SHANGHAI = ZoneInfo("Asia/Shanghai")


@pytest.mark.asyncio
async def test_query_daily_days_one_shows_one_day(logger):
    """-d 1 应仅返回今天（本地日期）的数据，不含昨天."""
    now_local = datetime.now(_SHANGHAI)
    today_start_utc = datetime(
        now_local.year,
        now_local.month,
        now_local.day,
        tzinfo=_SHANGHAI,
    ).astimezone(UTC)
    yesterday_start_utc = today_start_utc - timedelta(days=1)

    # 插入一条"今天"的记录
    await logger._db.execute(
        """INSERT INTO usage_log (ts, vendor, model_requested, model_served,
                                  input_tokens, output_tokens)
           VALUES (?, 'anthropic', 'claude-sonnet-4', 'claude-sonnet-4', 100, 50)""",
        (today_start_utc.strftime("%Y-%m-%dT%H:%M:%fZ"),),
    )
    # 插入一条"昨天"的记录
    await logger._db.execute(
        """INSERT INTO usage_log (ts, vendor, model_requested, model_served,
                                  input_tokens, output_tokens)
           VALUES (?, 'anthropic', 'claude-opus-4', 'claude-opus-4', 200, 80)""",
        (yesterday_start_utc.strftime("%Y-%m-%dT%H:%M:%fZ"),),
    )
    await logger._db.commit()

    with patch("coding.proxy.logging.db._local_tz", return_value=_SHANGHAI):
        rows = await logger.query_daily(days=1)

    # days=1 只应返回今天的数据
    assert len(rows) == 1
    assert rows[0]["model_requested"] == "claude-sonnet-4"


@pytest.mark.asyncio
async def test_query_daily_days_boundary_exact(logger):
    """-d N 的范围应精确包含 N 个自然日."""
    now_local = datetime.now(_SHANGHAI)
    today_start = datetime(
        now_local.year, now_local.month, now_local.day, tzinfo=_SHANGHAI
    )

    # 插入今天、昨天、前天共 3 条数据
    for day_offset in range(3):
        dt = (today_start - timedelta(days=day_offset)).astimezone(UTC)
        await logger._db.execute(
            """INSERT INTO usage_log (ts, vendor, model_requested, model_served,
                                      input_tokens, output_tokens)
               VALUES (?, 'anthropic', 'm', 'm', 100, 50)""",
            (dt.strftime("%Y-%m-%dT%H:%M:%fZ"),),
        )
    await logger._db.commit()

    with patch("coding.proxy.logging.db._local_tz", return_value=_SHANGHAI):
        rows_2 = await logger.query_daily(days=2)
        rows_3 = await logger.query_daily(days=3)

    assert len(rows_2) == 2  # 今天 + 昨天
    assert len(rows_3) == 3  # 今天 + 昨天 + 前天


@pytest.mark.asyncio
async def test_query_daily_groups_by_local_date(logger):
    """UTC 时间 16:30 (= UTC+8 次日 00:30) 应归入本地次日."""
    # 动态计算：取「今天本地日期」的前一天作为基准，
    # 构造该日本地 00:30（= UTC 前日 16:30）的时间戳，
    # 确保 days=7 窗口必然包含此数据。
    now_local = datetime.now(_SHANGHAI)
    target_local_date = now_local.date() - timedelta(days=1)  # 昨天
    target_local_dt = datetime(
        target_local_date.year, target_local_date.month,
        target_local_date.day, 0, 30, 0, tzinfo=_SHANGHAI,
    )
    utc_ts = target_local_dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")

    await logger._db.execute(
        """INSERT INTO usage_log (ts, vendor, model_requested, model_served,
                                  input_tokens, output_tokens)
           VALUES (?, 'anthropic', 'claude-sonnet-4', 'claude-sonnet-4', 100, 50)""",
        (utc_ts,),
    )
    await logger._db.commit()

    with patch("coding.proxy.logging.db._local_tz", return_value=_SHANGHAI):
        rows = await logger.query_daily(days=7)

    assert len(rows) == 1
    # 在 UTC+8 下，UTC 16:30 是次日 00:30，应显示为 target_local_date
    assert rows[0]["date"] == target_local_date.strftime("%Y-%m-%d")


@pytest.mark.asyncio
async def test_query_window_total_uses_utc_baseline(logger):
    """滚动窗口应基于 UTC 时间计算，避免本地时区偏移."""
    # 插入一条 2 小时前的记录
    cutoff = datetime.now(UTC) - timedelta(hours=2)
    await logger._db.execute(
        """INSERT INTO usage_log (ts, vendor, model_requested, model_served,
                                  input_tokens, output_tokens, success)
           VALUES (?, 'anthropic', 'claude-sonnet-4', 'claude-sonnet-4', 100, 50, 1)""",
        (cutoff.strftime("%Y-%m-%dT%H:%M:%fZ"),),
    )
    await logger._db.commit()

    total = await logger.query_window_total(window_hours=3.0, vendor="anthropic")
    assert total == 150  # 100 + 50

    # 1 小时窗口应不包含这条记录
    total_narrow = await logger.query_window_total(window_hours=1.0, vendor="anthropic")
    assert total_narrow == 0


@pytest.mark.asyncio
async def test_query_failover_stats_day_boundary(logger):
    """故障转移统计应遵循与 query_daily 相同的天数边界."""
    now_local = datetime.now(_SHANGHAI)
    today_start = datetime(
        now_local.year, now_local.month, now_local.day, tzinfo=_SHANGHAI
    )
    yesterday_start_utc = (today_start - timedelta(days=1)).astimezone(UTC)

    # 昨天的 failover 记录
    await logger._db.execute(
        """INSERT INTO usage_log (ts, vendor, model_requested, model_served,
                                  failover, failover_from)
           VALUES (?, 'zhipu', 's', 'g', 1, 'anthropic')""",
        (yesterday_start_utc.strftime("%Y-%m-%dT%H:%M:%fZ"),),
    )
    await logger._db.commit()

    with patch("coding.proxy.logging.db._local_tz", return_value=_SHANGHAI):
        # days=1 不应包含昨天的 failover
        stats_1 = await logger.query_failover_stats(days=1)
        assert len(stats_1) == 0

        # days=2 应包含昨天的 failover
        stats_2 = await logger.query_failover_stats(days=2)
        assert len(stats_2) == 1
        assert stats_2[0]["failover_from"] == "anthropic"


@pytest.mark.asyncio
async def test_query_daily_clamps_zero_days(logger):
    """days=0 应被提升为 1（等价于查今天）."""
    await logger.log(
        vendor="anthropic",
        model_requested="claude-sonnet-4",
        model_served="claude-sonnet-4",
        input_tokens=100,
        output_tokens=50,
    )
    # days=0 不应报错，行为等同 days=1
    rows = await logger.query_daily(days=0)
    assert len(rows) >= 1


# ---------------------------------------------------------------------------
# 按 model_served 聚合测试（同日/同供应商/同实际模型 → 合并为一行）
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_query_daily_aggregates_by_model_served(logger):
    """核心场景：同日/同供应商/同 model_served 但不同 model_requested 应聚为一行."""
    await logger.log(
        vendor="anthropic",
        model_requested="claude-opus-4-6",
        model_served="claude-sonnet-4-6",
        input_tokens=100,
        output_tokens=50,
    )
    await logger.log(
        vendor="anthropic",
        model_requested="claude-sonnet-4-6",
        model_served="claude-sonnet-4-6",
        input_tokens=200,
        output_tokens=80,
    )
    rows = await logger.query_daily(days=7)
    # 同一 model_served → 聚为一行
    assert len(rows) == 1
    row = rows[0]
    # model_requested 应包含两个值，逗号分隔
    requested_models = set(row["model_requested"].split(","))
    assert requested_models == {"claude-opus-4-6", "claude-sonnet-4-6"}
    # Token 统计应正确求和
    assert row["total_requests"] == 2
    assert row["total_input"] == 300
    assert row["total_output"] == 130


@pytest.mark.asyncio
async def test_query_daily_single_model_no_comma(logger):
    """边界：仅一个 model_requested 时不应出现多余逗号."""
    await logger.log(
        vendor="anthropic",
        model_requested="claude-sonnet-4-6",
        model_served="claude-sonnet-4-6",
        input_tokens=100,
        output_tokens=50,
    )
    rows = await logger.query_daily(days=7)
    assert len(rows) == 1
    # 单值时应为纯字符串，不含逗号
    assert rows[0]["model_requested"] == "claude-sonnet-4-6"
    assert "," not in rows[0]["model_requested"]


@pytest.mark.asyncio
async def test_query_daily_distinct_deduplication(logger):
    """DISTINCT 应去重相同的 model_requested."""
    await logger.log(
        vendor="anthropic",
        model_requested="claude-sonnet-4-6",
        model_served="claude-sonnet-4-6",
        input_tokens=100,
        output_tokens=50,
    )
    await logger.log(
        vendor="anthropic",
        model_requested="claude-sonnet-4-6",  # 相同的 model_requested
        model_served="claude-sonnet-4-6",
        input_tokens=200,
        output_tokens=80,
    )
    rows = await logger.query_daily(days=7)
    assert len(rows) == 1
    # 去重后不应重复出现
    assert rows[0]["model_requested"] == "claude-sonnet-4-6"
    assert rows[0]["model_requested"].count(",") == 0


@pytest.mark.asyncio
async def test_query_daily_model_filter_still_works(logger):
    """--model 过滤按 model_served 执行，仅返回实际服务模型匹配的记录."""
    await logger.log(
        vendor="anthropic",
        model_requested="claude-opus-4-6",
        model_served="claude-opus-4-6",
        input_tokens=100,
        output_tokens=50,
    )
    await logger.log(
        vendor="anthropic",
        model_requested="claude-sonnet-4-6",
        model_served="claude-sonnet-4-6",
        input_tokens=200,
        output_tokens=80,
    )
    # 按 model_served 过滤，仅保留 model_served=opus 的记录
    rows = await logger.query_daily(days=7, model="claude-opus-4-6")
    assert len(rows) == 1
    assert rows[0]["total_requests"] == 1
    assert rows[0]["total_input"] == 100


@pytest.mark.asyncio
async def test_query_daily_different_vendors_not_merged(logger):
    """不同供应商即使 model_served 相同也不应合并."""
    await logger.log(
        vendor="anthropic",
        model_requested="claude-sonnet-4-6",
        model_served="claude-sonnet-4-6",
        input_tokens=100,
        output_tokens=50,
    )
    await logger.log(
        vendor="zhipu",
        model_requested="claude-sonnet-4-6",
        model_served="claude-sonnet-4-6",  # 同一 model_served
        input_tokens=200,
        output_tokens=80,
    )
    rows = await logger.query_daily(days=7)
    # 不同供应商，应为两行
    assert len(rows) == 2
    vendors = {r["vendor"] for r in rows}
    assert vendors == {"anthropic", "zhipu"}
