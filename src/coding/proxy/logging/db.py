"""Token 用量 SQLite 日志."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

import aiosqlite
from pathlib import Path
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)


def _local_tz() -> ZoneInfo:
    """获取系统本地时区，失败降级 UTC."""
    try:
        return datetime.now().astimezone().tzinfo  # type: ignore[return-value]
    except Exception:
        logger.warning("无法获取系统本地时区，降级使用 UTC")
        return timezone.utc


def _days_start_utc_iso(days: int) -> str:
    """
    计算本地时区下「往前推 days-1 天的那天 00:00:00」对应的 UTC ISO 字符串.

    语义: days=1 → 今天 00:00 local → 转 UTC
          days=7 → 6 天前 00:00 local → 转 UTC
    """
    tz = _local_tz()
    start_date = datetime.now(tz).date() - timedelta(days=max(1, days) - 1)
    start_dt = datetime(start_date.year, start_date.month, start_date.day, tzinfo=tz)
    return start_dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%f+00:00")


def _hours_ago_utc_iso(hours: float) -> str:
    """计算 hours 小时前的 UTC ISO 字符串（用于滚动窗口）."""
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
    return cutoff.strftime("%Y-%m-%dT%H:%M:%f+00:00")


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
        return datetime.fromisoformat(
            ts_str.replace("Z", "+00:00")
        ).astimezone(tz).strftime("%Y-%m-%d")
    except (ValueError, TypeError, AttributeError):
        # 非 ISO 格式（如旧数据 'now'）降级为字符串截取前 10 位
        if isinstance(ts_str, str) and len(ts_str) >= 10:
            return ts_str[:10]
        return ""


_CREATE_TABLE = """
CREATE TABLE IF NOT EXISTS usage_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    backend TEXT NOT NULL,
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
CREATE INDEX IF NOT EXISTS idx_usage_ts ON usage_log(ts);
CREATE INDEX IF NOT EXISTS idx_usage_backend ON usage_log(backend);
CREATE TABLE IF NOT EXISTS usage_evidence (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    backend TEXT NOT NULL,
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
CREATE INDEX IF NOT EXISTS idx_usage_evidence_request_id ON usage_evidence(request_id);
CREATE INDEX IF NOT EXISTS idx_usage_evidence_backend ON usage_evidence(backend);
"""


class TokenLogger:
    def __init__(self, db_path: Path) -> None:
        self._db_path = db_path
        self._db: aiosqlite.Connection | None = None

    async def init(self) -> None:
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._db = await aiosqlite.connect(str(self._db_path))
        self._db.row_factory = aiosqlite.Row
        await self._db.execute("PRAGMA journal_mode=WAL")
        await self._db.executescript(_CREATE_TABLE)
        # 注册时区感知的日期函数：将 UTC 时间戳转为本地日期
        await self._db.create_function("local_date", 1, _local_date_udf)
        await self._migrate_add_failover_from()
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

    async def log(self, backend: str, model_requested: str, model_served: str,
                  input_tokens: int = 0, output_tokens: int = 0,
                  cache_creation_tokens: int = 0, cache_read_tokens: int = 0,
                  duration_ms: int = 0, success: bool = True,
                  failover: bool = False, failover_from: str | None = None,
                  request_id: str = "") -> None:
        if not self._db:
            return
        await self._db.execute(
            """INSERT INTO usage_log
               (backend, model_requested, model_served,
                input_tokens, output_tokens,
                cache_creation_tokens, cache_read_tokens,
                duration_ms, success, failover, failover_from, request_id)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (backend, model_requested, model_served,
             input_tokens, output_tokens,
             cache_creation_tokens, cache_read_tokens,
             duration_ms, success, failover, failover_from, request_id))
        await self._db.commit()

    async def log_evidence(
        self,
        *,
        backend: str,
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
               (backend, request_id, model_served, evidence_kind, raw_usage_json,
                parsed_input_tokens, parsed_output_tokens,
                parsed_cache_creation_tokens, parsed_cache_read_tokens,
                cache_signal_present, source_field_map_json)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                backend,
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
            """SELECT backend, request_id, model_served, evidence_kind, raw_usage_json,
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

    async def query_daily(self, days: int = 7, backend: str | None = None,
                          model: str | None = None) -> list[dict]:
        if not self._db:
            return []
        days = max(1, days)
        start_iso = _days_start_utc_iso(days)
        sql = """SELECT local_date(ts) AS date, backend, model_requested, model_served,
                   COUNT(*) AS total_requests,
                   SUM(input_tokens) AS total_input,
                   SUM(output_tokens) AS total_output,
                   SUM(cache_creation_tokens) AS total_cache_creation,
                   SUM(cache_read_tokens) AS total_cache_read,
                   SUM(CASE WHEN failover THEN 1 ELSE 0 END) AS total_failovers,
                   AVG(duration_ms) AS avg_duration_ms
               FROM usage_log WHERE ts >= ?"""
        params: list = [start_iso]
        if backend:
            sql += " AND backend = ?"
            params.append(backend)
        if model:
            sql += " AND model_requested = ?"
            params.append(model)
        sql += (" GROUP BY local_date(ts), backend, model_requested, model_served"
                " ORDER BY local_date(ts) DESC, backend, model_requested, model_served")
        cursor = await self._db.execute(sql, params)
        rows = await cursor.fetchall()
        return [dict(row) for row in rows]

    async def query_failover_stats(self, days: int = 7, include_model_info: bool = False) -> list[dict]:
        """
        按 failover_from → backend 聚合故障转移次数.

        Args:
            days: 查询天数
            include_model_info: 是否在聚合中包含模型信息
                              - False: 按 (failover_from, backend) 聚合 (默认,向后兼容)
                              - True: 按 (failover_from, backend, model_requested, model_served) 聚合
        """
        if not self._db:
            return []
        days = max(1, days)
        start_iso = _days_start_utc_iso(days)

        if include_model_info:
            sql = """SELECT failover_from, backend, model_requested, model_served,
                       COUNT(*) AS count
                   FROM usage_log
                   WHERE failover = 1 AND ts >= ?
                   GROUP BY failover_from, backend, model_requested, model_served
                   ORDER BY count DESC"""
        else:
            # 保持原有的聚合逻辑确保向后兼容
            sql = """SELECT failover_from, backend,
                       COUNT(*) AS count
                   FROM usage_log
                   WHERE failover = 1 AND ts >= ?
                   GROUP BY failover_from, backend
                   ORDER BY count DESC"""

        cursor = await self._db.execute(sql, [start_iso])
        rows = await cursor.fetchall()
        return [dict(row) for row in rows]

    async def query_window_total(
        self, window_hours: float, backend: str = "anthropic",
    ) -> int:
        """查询滚动时间窗口内指定后端的 token 总用量."""
        if not self._db:
            return 0
        cutoff_iso = _hours_ago_utc_iso(window_hours)
        cursor = await self._db.execute(
            """SELECT COALESCE(SUM(input_tokens + output_tokens), 0) AS total
               FROM usage_log
               WHERE backend = ? AND success = 1
                 AND ts >= ?""",
            (backend, cutoff_iso),
        )
        row = await cursor.fetchone()
        return row["total"] if row else 0

    async def close(self) -> None:
        if self._db:
            await self._db.close()
            self._db = None
