"""Token 用量 SQLite 日志."""

from __future__ import annotations

import logging

import aiosqlite
from pathlib import Path

logger = logging.getLogger(__name__)

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

    async def query_daily(self, days: int = 7, backend: str | None = None,
                          model: str | None = None) -> list[dict]:
        if not self._db:
            return []
        sql = """SELECT date(ts) AS date, backend, model_requested, model_served,
                   COUNT(*) AS total_requests,
                   SUM(input_tokens) AS total_input,
                   SUM(output_tokens) AS total_output,
                   SUM(CASE WHEN failover THEN 1 ELSE 0 END) AS total_failovers,
                   AVG(duration_ms) AS avg_duration_ms
               FROM usage_log WHERE ts >= datetime('now', ? || ' days')"""
        params: list = [f"-{days}"]
        if backend:
            sql += " AND backend = ?"
            params.append(backend)
        if model:
            sql += " AND model_requested = ?"
            params.append(model)
        sql += (" GROUP BY date(ts), backend, model_requested, model_served"
                " ORDER BY date(ts) DESC, backend, model_requested, model_served")
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

        if include_model_info:
            sql = """SELECT failover_from, backend, model_requested, model_served,
                       COUNT(*) AS count
                   FROM usage_log
                   WHERE failover = 1 AND ts >= datetime('now', ? || ' days')
                   GROUP BY failover_from, backend, model_requested, model_served
                   ORDER BY count DESC"""
        else:
            # 保持原有的聚合逻辑确保向后兼容
            sql = """SELECT failover_from, backend,
                       COUNT(*) AS count
                   FROM usage_log
                   WHERE failover = 1 AND ts >= datetime('now', ? || ' days')
                   GROUP BY failover_from, backend
                   ORDER BY count DESC"""

        cursor = await self._db.execute(sql, [f"-{days}"])
        rows = await cursor.fetchall()
        return [dict(row) for row in rows]

    async def query_window_total(
        self, window_hours: float, backend: str = "anthropic",
    ) -> int:
        """查询滚动时间窗口内指定后端的 token 总用量."""
        if not self._db:
            return 0
        cursor = await self._db.execute(
            """SELECT COALESCE(SUM(input_tokens + output_tokens), 0) AS total
               FROM usage_log
               WHERE backend = ? AND success = 1
                 AND ts >= datetime('now', ? || ' hours')""",
            (backend, f"-{window_hours}"),
        )
        row = await cursor.fetchone()
        return row["total"] if row else 0

    async def close(self) -> None:
        if self._db:
            await self._db.close()
            self._db = None
