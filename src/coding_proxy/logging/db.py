"""Token 用量 SQLite 日志."""

from __future__ import annotations

import aiosqlite
from pathlib import Path

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
        await self._db.commit()

    async def log(self, backend: str, model_requested: str, model_served: str,
                  input_tokens: int = 0, output_tokens: int = 0,
                  cache_creation_tokens: int = 0, cache_read_tokens: int = 0,
                  duration_ms: int = 0, success: bool = True,
                  failover: bool = False, request_id: str = "") -> None:
        if not self._db:
            return
        await self._db.execute(
            """INSERT INTO usage_log
               (backend, model_requested, model_served,
                input_tokens, output_tokens,
                cache_creation_tokens, cache_read_tokens,
                duration_ms, success, failover, request_id)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (backend, model_requested, model_served,
             input_tokens, output_tokens,
             cache_creation_tokens, cache_read_tokens,
             duration_ms, success, failover, request_id))
        await self._db.commit()

    async def query_daily(self, days: int = 7, backend: str | None = None) -> list[dict]:
        if not self._db:
            return []
        sql = """SELECT date(ts) AS date, backend,
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
        sql += " GROUP BY date(ts), backend ORDER BY date(ts) DESC, backend"
        cursor = await self._db.execute(sql, params)
        rows = await cursor.fetchall()
        return [dict(row) for row in rows]

    async def close(self) -> None:
        if self._db:
            await self._db.close()
            self._db = None
