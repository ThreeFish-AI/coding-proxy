"""Token 用量 SQLite 日志."""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from pathlib import Path
from zoneinfo import ZoneInfo

import aiosqlite

logger = logging.getLogger(__name__)


# ── 时间维度枚举 ──────────────────────────────────────────────


class TimePeriod(StrEnum):
    """用量查询时间维度."""

    DAY = "day"
    WEEK = "week"
    MONTH = "month"
    TOTAL = "total"


# ── 时区工具函数 ──────────────────────────────────────────────


def _local_tz() -> ZoneInfo:
    """获取系统本地时区，失败降级 UTC."""
    try:
        return datetime.now().astimezone().tzinfo  # type: ignore[return-value]
    except Exception:
        logger.warning("无法获取系统本地时区，降级使用 UTC")
        return UTC


def _days_start_utc_iso(days: int) -> str:
    """
    计算本地时区下「往前推 days-1 天的那天 00:00:00」对应的 UTC ISO 字符串.

    语义: days=1 → 今天 00:00 local → 转 UTC
          days=7 → 6 天前 00:00 local → 转 UTC
    """
    tz = _local_tz()
    start_date = datetime.now(tz).date() - timedelta(days=max(1, days) - 1)
    start_dt = datetime(start_date.year, start_date.month, start_date.day, tzinfo=tz)
    return start_dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%f+00:00")


def _hours_ago_utc_iso(hours: float) -> str:
    """计算 hours 小时前的 UTC ISO 字符串（用于滚动窗口）."""
    cutoff = datetime.now(UTC) - timedelta(hours=hours)
    return cutoff.strftime("%Y-%m-%dT%H:%M:%f+00:00")


def _weeks_start_utc_iso(weeks: int) -> str:
    """计算本地时区下 weeks 周前的周一 00:00 对应的 UTC ISO 字符串."""
    tz = _local_tz()
    now = datetime.now(tz)
    monday = now.date() - timedelta(days=now.weekday(), weeks=max(1, weeks) - 1)
    start_dt = datetime(monday.year, monday.month, monday.day, tzinfo=tz)
    return start_dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%f+00:00")


def _months_start_utc_iso(months: int) -> str:
    """计算本地时区下 months 个月前的 1 日 00:00 对应的 UTC ISO 字符串."""
    tz = _local_tz()
    now = datetime.now(tz)
    y, m = now.year, now.month
    m -= max(1, months) - 1
    while m <= 0:
        m += 12
        y -= 1
    start_dt = datetime(y, m, 1, tzinfo=tz)
    return start_dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%f+00:00")


def _period_start_iso(period: TimePeriod, count: int) -> str | None:
    """根据时间维度和数量计算起始 UTC ISO 字符串.

    Returns:
        ISO 字符串，或 ``None``（``count == 0`` 或 TOTAL 维度时不限时间范围）。
    """
    if count == 0:
        return None  # count=0 语义：不限时间
    if period is TimePeriod.DAY:
        return _days_start_utc_iso(count)
    if period is TimePeriod.WEEK:
        return _weeks_start_utc_iso(count)
    if period is TimePeriod.MONTH:
        return _months_start_utc_iso(count)
    return None  # TOTAL: 全量查询


# ── SQLite UDF ────────────────────────────────────────────────


def _local_date_udf(ts_str: str) -> str:
    """
    SQLite UDF：将 UTC ISO 时间戳转为本地日期字符串.

    设计要点：
    - 动态调用 _local_tz()（非闭包捕获），使 unittest.mock.patch 可注入测试时区
    - 容错处理非 ISO 格式（如旧迁移数据 ts='now'），降级为字符串截取前 10 位
    - 永不抛异常（SQLite UDF 异常会导致整个查询失败）
    """
    try:
        tz = _local_tz()
        return (
            datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
            .astimezone(tz)
            .strftime("%Y-%m-%d")
        )
    except (ValueError, TypeError, AttributeError):
        # 非 ISO 格式（如旧数据 'now'）降级为字符串截取前 10 位
        if isinstance(ts_str, str) and len(ts_str) >= 10:
            return ts_str[:10]
        return ""


def _local_week_udf(ts_str: str) -> str:
    """SQLite UDF：将 UTC ISO 时间戳转为本地 ISO 周标识 (YYYY-WNN)."""
    try:
        tz = _local_tz()
        return (
            datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
            .astimezone(tz)
            .strftime("%G-W%V")
        )
    except (ValueError, TypeError, AttributeError):
        return ""


def _local_month_udf(ts_str: str) -> str:
    """SQLite UDF：将 UTC ISO 时间戳转为本地年月标识 (YYYY-MM)."""
    try:
        tz = _local_tz()
        return (
            datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
            .astimezone(tz)
            .strftime("%Y-%m")
        )
    except (ValueError, TypeError, AttributeError):
        if isinstance(ts_str, str) and len(ts_str) >= 7:
            return ts_str[:7]
        return ""


# ── DDL ───────────────────────────────────────────────────────

_CREATE_TABLES = """
CREATE TABLE IF NOT EXISTS usage_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
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
CREATE TABLE IF NOT EXISTS usage_evidence (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
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

_CREATE_INDEXES = """
CREATE INDEX IF NOT EXISTS idx_usage_ts ON usage_log(ts);
CREATE INDEX IF NOT EXISTS idx_usage_vendor ON usage_log(vendor);
CREATE INDEX IF NOT EXISTS idx_usage_evidence_request_id ON usage_evidence(request_id);
CREATE INDEX IF NOT EXISTS idx_usage_evidence_vendor ON usage_evidence(vendor);
"""

# ── 时间维度 → SQL 片段映射 ───────────────────────────────────

_PERIOD_SQL: dict[TimePeriod, tuple[str, str, str]] = {
    # (date_expr, group_by, order_by_expr)
    TimePeriod.DAY: (
        "local_date(ts) AS date",
        "local_date(ts), vendor, model_served",
        "local_date(ts) DESC, vendor, model_served",
    ),
    TimePeriod.WEEK: (
        "local_week(ts) AS date",
        "local_week(ts), vendor, model_served",
        "local_week(ts) DESC, vendor, model_served",
    ),
    TimePeriod.MONTH: (
        "local_month(ts) AS date",
        "local_month(ts), vendor, model_served",
        "local_month(ts) DESC, vendor, model_served",
    ),
    TimePeriod.TOTAL: (
        "NULL AS date",
        "vendor, model_served",
        "vendor, model_served",
    ),
}


# ── TokenLogger ───────────────────────────────────────────────


class TokenLogger:
    def __init__(self, db_path: Path) -> None:
        self._db_path = db_path
        self._db: aiosqlite.Connection | None = None

    async def init(self) -> None:
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._db = await aiosqlite.connect(str(self._db_path))
        self._db.row_factory = aiosqlite.Row
        await self._db.execute("PRAGMA journal_mode=WAL")
        await self._db.executescript(_CREATE_TABLES)
        # 迁移必须在建索引之前执行，确保 vendor 列已存在
        await self._migrate_rename_backend_to_vendor()
        await self._migrate_add_failover_from()
        await self._db.executescript(_CREATE_INDEXES)
        # 注册时区感知的日期函数：将 UTC 时间戳转为本地时间维度
        await self._db.create_function("local_date", 1, _local_date_udf)
        await self._db.create_function("local_week", 1, _local_week_udf)
        await self._db.create_function("local_month", 1, _local_month_udf)
        await self._db.commit()

    async def _migrate_add_failover_from(self) -> None:
        """幂等迁移：为已有数据库添加 failover_from 列."""
        if not self._db:
            return
        cursor = await self._db.execute("PRAGMA table_info(usage_log)")
        columns = {row["name"] for row in await cursor.fetchall()}
        if "failover_from" not in columns:
            await self._db.execute(
                "ALTER TABLE usage_log ADD COLUMN failover_from TEXT DEFAULT NULL"
            )
            logger.info("Migration: added failover_from column to usage_log")

    async def _migrate_rename_backend_to_vendor(self) -> None:
        """幂等迁移：重命名 backend 列为 vendor."""
        if not self._db:
            return
        for table in ("usage_log", "usage_evidence"):
            cursor = await self._db.execute(f"PRAGMA table_info({table})")
            columns = {row["name"] for row in await cursor.fetchall()}
            if "backend" in columns and "vendor" not in columns:
                await self._db.execute(
                    f"ALTER TABLE {table} RENAME COLUMN backend TO vendor"
                )
                logger.info(
                    "Migration: renamed 'backend' column to 'vendor' in %s", table
                )

    async def log(
        self,
        vendor: str,
        model_requested: str,
        model_served: str,
        input_tokens: int = 0,
        output_tokens: int = 0,
        cache_creation_tokens: int = 0,
        cache_read_tokens: int = 0,
        duration_ms: int = 0,
        success: bool = True,
        failover: bool = False,
        failover_from: str | None = None,
        request_id: str = "",
    ) -> None:
        if not self._db:
            return
        await self._db.execute(
            """INSERT INTO usage_log
               (vendor, model_requested, model_served,
                input_tokens, output_tokens,
                cache_creation_tokens, cache_read_tokens,
                duration_ms, success, failover, failover_from, request_id)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                vendor,
                model_requested,
                model_served,
                input_tokens,
                output_tokens,
                cache_creation_tokens,
                cache_read_tokens,
                duration_ms,
                success,
                failover,
                failover_from,
                request_id,
            ),
        )
        await self._db.commit()

    async def log_evidence(
        self,
        *,
        vendor: str,
        request_id: str = "",
        model_served: str = "",
        evidence_kind: str,
        raw_usage_json: str,
        parsed_input_tokens: int = 0,
        parsed_output_tokens: int = 0,
        parsed_cache_creation_tokens: int = 0,
        parsed_cache_read_tokens: int = 0,
        cache_signal_present: bool = False,
        source_field_map_json: str = "{}",
    ) -> None:
        if not self._db:
            return
        await self._db.execute(
            """INSERT INTO usage_evidence
               (vendor, request_id, model_served, evidence_kind, raw_usage_json,
                parsed_input_tokens, parsed_output_tokens,
                parsed_cache_creation_tokens, parsed_cache_read_tokens,
                cache_signal_present, source_field_map_json)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                vendor,
                request_id,
                model_served,
                evidence_kind,
                raw_usage_json,
                parsed_input_tokens,
                parsed_output_tokens,
                parsed_cache_creation_tokens,
                parsed_cache_read_tokens,
                cache_signal_present,
                source_field_map_json,
            ),
        )
        await self._db.commit()

    async def query_evidence(self, request_id: str) -> list[dict]:
        if not self._db:
            return []
        cursor = await self._db.execute(
            """SELECT vendor, request_id, model_served, evidence_kind, raw_usage_json,
                      parsed_input_tokens, parsed_output_tokens,
                      parsed_cache_creation_tokens, parsed_cache_read_tokens,
                      cache_signal_present, source_field_map_json
               FROM usage_evidence
               WHERE request_id = ?
               ORDER BY id ASC""",
            (request_id,),
        )
        rows = await cursor.fetchall()
        return [dict(row) for row in rows]

    # ── 核心查询方法 ──────────────────────────────────────────

    async def query_usage(
        self,
        *,
        period: TimePeriod = TimePeriod.DAY,
        count: int = 7,
        vendor: str | list[str] | None = None,
        model: str | list[str] | None = None,
    ) -> list[dict]:
        """按指定时间维度聚合 Token 使用统计.

        Args:
            period: 时间维度（日/周/月/全量）。
            count: ``period`` 的数量。仅用于计算起始时间边界，
                   ``TOTAL`` 维度下忽略此参数。
            vendor: 过滤供应商，支持单个字符串或字符串列表（多 vendor 过滤）。
            model: 过滤实际服务模型（model_served），支持单个字符串或字符串列表。
        """
        if not self._db:
            return []

        date_expr, group_clause, order_clause = _PERIOD_SQL[period]

        sql = f"""SELECT {date_expr}, vendor,
                   GROUP_CONCAT(DISTINCT model_requested) AS model_requested,
                   model_served,
                   COUNT(*) AS total_requests,
                   SUM(input_tokens) AS total_input,
                   SUM(output_tokens) AS total_output,
                   SUM(cache_creation_tokens) AS total_cache_creation,
                   SUM(cache_read_tokens) AS total_cache_read,
                   SUM(CASE WHEN failover THEN 1 ELSE 0 END) AS total_failovers,
                   AVG(duration_ms) AS avg_duration_ms
               FROM usage_log WHERE 1=1"""

        params: list = []

        start_iso = _period_start_iso(period, count)
        if start_iso is not None:
            sql += " AND ts >= ?"
            params.append(start_iso)

        if vendor:
            vendors = [vendor] if isinstance(vendor, str) else vendor
            placeholders = ",".join("?" * len(vendors))
            sql += f" AND vendor IN ({placeholders})"
            params.extend(vendors)
        if model:
            models = [model] if isinstance(model, str) else model
            placeholders = ",".join("?" * len(models))
            sql += f" AND model_served IN ({placeholders})"
            params.extend(models)

        sql += f" GROUP BY {group_clause} ORDER BY {order_clause}"

        cursor = await self._db.execute(sql, params)
        rows = await cursor.fetchall()
        return [dict(row) for row in rows]

    async def query_daily(
        self,
        days: int | None = 7,
        vendor: str | None = None,
        model: str | None = None,
    ) -> list[dict]:
        """按日聚合 Token 使用统计.

        Args:
            days: 查询天数。``None`` 表示不限时间（全量查询）。
            vendor: 过滤供应商。
            model: 过滤请求模型。
        """
        # days=None → count=0 → _period_start_iso 返回 None → 不限时间
        count = 0 if days is None else days
        return await self.query_usage(
            period=TimePeriod.DAY, count=count, vendor=vendor, model=model
        )

    async def query_failover_stats(
        self, days: int | None = 7, include_model_info: bool = False
    ) -> list[dict]:
        """按 failover_from → vendor 聚合故障转移次数.

        Args:
            days: 查询天数。``None`` 表示不限时间（全量查询）。
            include_model_info: 是否在聚合中包含模型信息
                              - False: 按 (failover_from, vendor) 聚合 (默认,向后兼容)
                              - True: 按 (failover_from, vendor, model_requested, model_served) 聚合
        """
        if not self._db:
            return []

        time_clause = ""
        params: list = []
        if days is not None:
            days = max(1, days)
            start_iso = _days_start_utc_iso(days)
            time_clause = " AND ts >= ?"
            params.append(start_iso)

        if include_model_info:
            sql = f"""SELECT failover_from, vendor, model_requested, model_served,
                       COUNT(*) AS count
                   FROM usage_log
                   WHERE failover = 1{time_clause}
                   GROUP BY failover_from, vendor, model_requested, model_served
                   ORDER BY count DESC"""
        else:
            sql = f"""SELECT failover_from, vendor,
                       COUNT(*) AS count
                   FROM usage_log
                   WHERE failover = 1{time_clause}
                   GROUP BY failover_from, vendor
                   ORDER BY count DESC"""

        cursor = await self._db.execute(sql, params)
        rows = await cursor.fetchall()
        return [dict(row) for row in rows]

    async def query_window_total(
        self,
        window_hours: float,
        vendor: str = "anthropic",
    ) -> int:
        """查询滚动时间窗口内指定供应商的 token 总用量."""
        if not self._db:
            return 0
        cutoff_iso = _hours_ago_utc_iso(window_hours)
        cursor = await self._db.execute(
            """SELECT COALESCE(SUM(input_tokens + output_tokens), 0) AS total
               FROM usage_log
               WHERE vendor = ? AND success = 1
                 AND ts >= ?""",
            (vendor, cutoff_iso),
        )
        row = await cursor.fetchone()
        return row["total"] if row else 0

    async def close(self) -> None:
        if self._db:
            await self._db.close()
            self._db = None
