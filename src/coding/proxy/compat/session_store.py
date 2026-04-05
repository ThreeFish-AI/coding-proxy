"""兼容层会话状态持久化.

数据类型 ``CompatSessionRecord`` 已迁移至 :mod:`coding.proxy.model.compat`。
本文件保留 ``CompatSessionStore`` 持久化管理器，类型通过 re-export 提供。

.. deprecated::
    未来版本将移除类型 re-export，请直接从 :mod:`coding.proxy.model.compat` 导入。
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

import aiosqlite

# noqa: F401
from ..model.compat import CompatSessionRecord

_CREATE_TABLE = """
CREATE TABLE IF NOT EXISTS compat_session (
    session_key TEXT PRIMARY KEY,
    trace_id TEXT NOT NULL DEFAULT '',
    tool_call_map_json TEXT NOT NULL DEFAULT '{}',
    thought_signature_map_json TEXT NOT NULL DEFAULT '{}',
    provider_state_json TEXT NOT NULL DEFAULT '{}',
    state_version INTEGER NOT NULL DEFAULT 1,
    updated_at_unix INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_compat_session_updated_at ON compat_session(updated_at_unix);
"""


class CompatSessionStore:
    """兼容层会话状态 SQLite 持久化存储."""

    def __init__(self, db_path: Path, ttl_seconds: int = 86400) -> None:
        self._db_path = db_path
        self._ttl_seconds = ttl_seconds
        self._db: aiosqlite.Connection | None = None

    async def init(self) -> None:
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._db = await aiosqlite.connect(str(self._db_path))
        self._db.row_factory = aiosqlite.Row
        await self._db.execute("PRAGMA journal_mode=WAL")
        await self._db.executescript(_CREATE_TABLE)
        await self._purge_expired()
        await self._db.commit()

    async def get(self, session_key: str) -> CompatSessionRecord | None:
        if not self._db:
            return None
        cursor = await self._db.execute(
            """SELECT session_key, trace_id, tool_call_map_json, thought_signature_map_json,
                      provider_state_json, state_version, updated_at_unix
               FROM compat_session WHERE session_key = ?""",
            (session_key,),
        )
        row = await cursor.fetchone()
        if row is None:
            return None
        record = CompatSessionRecord(
            session_key=row["session_key"],
            trace_id=row["trace_id"],
            tool_call_map=_loads_dict(row["tool_call_map_json"]),
            thought_signature_map=_loads_dict(row["thought_signature_map_json"]),
            provider_state=_loads_dict(row["provider_state_json"]),
            state_version=row["state_version"],
            updated_at_unix=row["updated_at_unix"],
        )
        if self._is_expired(record.updated_at_unix):
            await self.delete(session_key)
            return None
        return record

    async def upsert(self, record: CompatSessionRecord) -> None:
        if not self._db:
            return
        updated_at = int(time.time())
        await self._db.execute(
            """INSERT INTO compat_session (
                   session_key, trace_id, tool_call_map_json, thought_signature_map_json,
                   provider_state_json, state_version, updated_at_unix
               ) VALUES (?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(session_key) DO UPDATE SET
                   trace_id=excluded.trace_id,
                   tool_call_map_json=excluded.tool_call_map_json,
                   thought_signature_map_json=excluded.thought_signature_map_json,
                   provider_state_json=excluded.provider_state_json,
                   state_version=excluded.state_version,
                   updated_at_unix=excluded.updated_at_unix""",
            (
                record.session_key,
                record.trace_id,
                json.dumps(record.tool_call_map, ensure_ascii=False, sort_keys=True),
                json.dumps(
                    record.thought_signature_map, ensure_ascii=False, sort_keys=True
                ),
                json.dumps(record.provider_state, ensure_ascii=False, sort_keys=True),
                record.state_version,
                updated_at,
            ),
        )
        await self._db.commit()

    async def delete(self, session_key: str) -> None:
        if not self._db:
            return
        await self._db.execute(
            "DELETE FROM compat_session WHERE session_key = ?", (session_key,)
        )
        await self._db.commit()

    async def close(self) -> None:
        if self._db:
            await self._db.close()
            self._db = None

    async def _purge_expired(self) -> None:
        if not self._db:
            return
        threshold = int(time.time()) - self._ttl_seconds
        await self._db.execute(
            "DELETE FROM compat_session WHERE updated_at_unix > 0 AND updated_at_unix < ?",
            (threshold,),
        )

    def _is_expired(self, updated_at_unix: int) -> bool:
        return (
            updated_at_unix > 0
            and (int(time.time()) - updated_at_unix) > self._ttl_seconds
        )


def _loads_dict(raw: str) -> dict[str, Any]:
    try:
        value = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return {}
    return value if isinstance(value, dict) else {}
