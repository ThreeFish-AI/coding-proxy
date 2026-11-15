"""配置存储 ``$CODING_PROXY_HOME/config.db``（stdlib sqlite3）.

- ``config_section``：行存在 = 该 section 被覆盖；删除行 = 恢复默认；``data`` 为 JSON
  （可为字面 ``null``，例如 ``tiers: null``）。
- ``secret``：密钥按路径独立存放（stored 密文 / env 变量名 / empty）。
- ``meta``：import_marker、key_check、key_id、secrets_revision、``rev:<section>``。

per-section revision 单调递增；删除行后 revision 由 ``meta['rev:<section>']`` 延续，
因此 reset 同样会推进 revision。只读访问不会创建文件。
"""

from __future__ import annotations

import json
import os
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 1
SECRET_KINDS = ("stored", "env", "empty")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS config_section (
    section TEXT PRIMARY KEY,
    data TEXT NOT NULL,
    revision INTEGER NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS secret (
    path TEXT PRIMARY KEY,
    kind TEXT NOT NULL CHECK (kind IN ('stored', 'env', 'empty')),
    ciphertext BLOB,
    env_var TEXT,
    hint TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


_RECOVERY_HINT = "请从备份恢复该文件，或为该实例设置独立的 CODING_PROXY_HOME"


class RevisionConflict(Exception):
    """乐观并发冲突：期望 revision 与存储不一致."""

    def __init__(self, sections: list[str]) -> None:
        self.sections = sorted(sections)
        super().__init__(f"revision conflict: {', '.join(self.sections)}")


class ConfigStoreError(RuntimeError):
    """config.db 不可用（schema 过新 / 文件损坏 / 被占用）；消息只含路径与原因."""


class StoreSchemaError(ConfigStoreError):
    """config.db schema 版本不受支持."""

    def __init__(self, path: Path, version: int) -> None:
        super().__init__(
            f"{path} 的 schema v{version} 高于当前版本支持的 v{SCHEMA_VERSION}"
            "（可能由更新版本的 coding-proxy 写入）。请升级 coding-proxy，"
            "或为该实例设置独立的 CODING_PROXY_HOME"
        )


class StoreCorruptError(ConfigStoreError):
    """config.db 不是有效的 SQLite 数据库（文件损坏）."""

    def __init__(self, path: Path, reason: str) -> None:
        super().__init__(f"无法读取配置存储 {path}（{reason}）。{_RECOVERY_HINT}")


class StoreAccessError(ConfigStoreError):
    """config.db 暂时无法访问（权限 / 磁盘 / 只读文件系统等），文件本身未必损坏."""

    def __init__(self, path: Path, reason: str) -> None:
        super().__init__(
            f"无法访问配置存储 {path}（{reason}）。请检查该文件及所在目录的权限与"
            "磁盘状态；不要删除该文件，其中包含配置覆盖与加密密钥"
        )


class StoreBusyError(ConfigStoreError):
    """config.db 被其它写入方长时间占用（可重试）."""

    def __init__(self, path: Path) -> None:
        super().__init__(f"配置存储 {path} 正被其它进程写入，请稍后重试")


_CORRUPT_CODES = frozenset({sqlite3.SQLITE_CORRUPT, sqlite3.SQLITE_NOTADB})


def _primary_code(exc: sqlite3.Error) -> int | None:
    code = getattr(exc, "sqlite_errorcode", None)
    return code & 0xFF if isinstance(code, int) else None


@contextmanager
def _sqlite_errors(path: Path) -> Iterator[None]:
    """把 sqlite 打开 / 加锁阶段的异常映射为 :class:`ConfigStoreError`.

    只有 SQLITE_CORRUPT / SQLITE_NOTADB 视为文件损坏；其余（权限、I/O、只读）为
    :class:`StoreAccessError`，避免误导用户删除仍然完好的 config.db。
    """
    try:
        yield
    except sqlite3.OperationalError as exc:
        text = str(exc).lower()
        if "locked" in text or "busy" in text:
            raise StoreBusyError(path) from None
        if _primary_code(exc) in _CORRUPT_CODES:
            raise StoreCorruptError(path, str(exc)) from None
        raise StoreAccessError(path, str(exc)) from None
    except sqlite3.DatabaseError as exc:
        raise StoreCorruptError(path, str(exc)) from None


@dataclass(frozen=True)
class SecretRecord:
    """密钥记录；``ciphertext`` 不参与 repr，避免进入日志."""

    kind: str
    ciphertext: bytes | None = field(default=None, repr=False)
    env_var: str | None = None
    hint: str = ""

    def __post_init__(self) -> None:
        if self.kind not in SECRET_KINDS:
            raise ValueError(f"unknown secret kind: {self.kind}")


@dataclass
class StoreSnapshot:
    sections: dict[str, Any] = field(default_factory=dict)
    revisions: dict[str, int] = field(default_factory=dict)
    secrets: dict[str, SecretRecord] = field(default_factory=dict)
    meta: dict[str, str] = field(default_factory=dict)

    def revision(self, section: str) -> int:
        return self.revisions.get(section, 0)


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _read_snapshot(conn: sqlite3.Connection, path: Path) -> StoreSnapshot:
    snap = StoreSnapshot()
    for key, value in conn.execute("SELECT key, value FROM meta"):
        snap.meta[key] = value
        if key.startswith("rev:"):
            snap.revisions[key[4:]] = int(value)
    for section, data, revision in conn.execute(
        "SELECT section, data, revision FROM config_section"
    ):
        try:
            snap.sections[section] = json.loads(data)
        except ValueError:
            # 行内 data 非 JSON（外部改动 / 文件损坏）：走损坏分支而非裸 traceback
            raise StoreCorruptError(
                path, f"section {section} 的数据不是有效 JSON"
            ) from None
        snap.revisions[section] = max(int(revision), snap.revisions.get(section, 0))
    for path, kind, ciphertext, env_var, hint in conn.execute(
        "SELECT path, kind, ciphertext, env_var, hint FROM secret"
    ):
        snap.secrets[path] = SecretRecord(
            kind=kind,
            ciphertext=bytes(ciphertext) if ciphertext is not None else None,
            env_var=env_var,
            hint=hint or "",
        )
    return snap


class StoreTxn:
    """写事务句柄（由 :meth:`ConfigStore.transaction` 提供）."""

    def __init__(self, conn: sqlite3.Connection, snapshot: StoreSnapshot) -> None:
        self._conn = conn
        self.snapshot = snapshot
        self._secrets_touched = False

    def _bump(self, section: str) -> int:
        rev = self.snapshot.revision(section) + 1
        self._conn.execute(
            "INSERT INTO meta(key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (f"rev:{section}", str(rev)),
        )
        self.snapshot.revisions[section] = rev
        return rev

    def put_section(self, section: str, data: Any) -> int:
        """写入 section 覆盖（``data`` 可为 None → JSON null），返回新 revision."""
        payload = json.dumps(data, ensure_ascii=False, allow_nan=False)
        rev = self._bump(section)
        self._conn.execute(
            "INSERT INTO config_section(section, data, revision, updated_at) "
            "VALUES (?, ?, ?, ?) ON CONFLICT(section) DO UPDATE SET "
            "data = excluded.data, revision = excluded.revision, "
            "updated_at = excluded.updated_at",
            (section, payload, rev, _now()),
        )
        self.snapshot.sections[section] = json.loads(payload)
        return rev

    def delete_section(self, section: str) -> int:
        """删除 section 覆盖（恢复默认），revision 照常推进."""
        rev = self._bump(section)
        self._conn.execute("DELETE FROM config_section WHERE section = ?", (section,))
        self.snapshot.sections.pop(section, None)
        return rev

    def put_secret(self, path: str, record: SecretRecord | None) -> None:
        """写入 / 删除（``record=None``）密钥记录."""
        if record is None:
            self._conn.execute("DELETE FROM secret WHERE path = ?", (path,))
            self.snapshot.secrets.pop(path, None)
        else:
            self._conn.execute(
                "INSERT INTO secret(path, kind, ciphertext, env_var, hint, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT(path) DO UPDATE SET "
                "kind = excluded.kind, ciphertext = excluded.ciphertext, "
                "env_var = excluded.env_var, hint = excluded.hint, "
                "updated_at = excluded.updated_at",
                (
                    path,
                    record.kind,
                    record.ciphertext,
                    record.env_var,
                    record.hint,
                    _now(),
                ),
            )
            self.snapshot.secrets[path] = record
        if not self._secrets_touched:
            self._secrets_touched = True
            rev = int(self.snapshot.meta.get("secrets_revision", "0")) + 1
            self.set_meta("secrets_revision", str(rev))

    def set_meta(self, key: str, value: str) -> None:
        self._conn.execute(
            "INSERT INTO meta(key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )
        self.snapshot.meta[key] = value


class ConfigStore:
    """``config.db`` 访问层."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)

    def exists(self) -> bool:
        return self.path.is_file()

    # ── 连接 ──────────────────────────────────────────────────

    @staticmethod
    def _configure(conn: sqlite3.Connection) -> None:
        conn.execute("PRAGMA busy_timeout = 2000")
        conn.execute("PRAGMA secure_delete = ON")

    def _connect_rw(self) -> sqlite3.Connection:
        if not self.path.exists():
            self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            try:
                fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                os.close(fd)
            except FileExistsError:
                pass
        conn = sqlite3.connect(self.path, isolation_level=None)
        try:
            self._configure(conn)
            conn.execute("PRAGMA journal_mode = DELETE")
            self._ensure_schema(conn)
        except BaseException:
            conn.close()
            raise
        return conn

    def _connect_existing(self, mode: str) -> sqlite3.Connection:
        """打开已存在的文件（``mode=ro|rw``；URI 模式下文件缺失不会被创建）."""
        uri = f"{self.path.resolve().as_uri()}?mode={mode}"
        conn = sqlite3.connect(uri, uri=True, isolation_level=None)
        try:
            self._configure(conn)
        except BaseException:
            conn.close()
            raise
        return conn

    def _ensure_schema(self, conn: sqlite3.Connection) -> None:
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        if version > SCHEMA_VERSION:
            raise StoreSchemaError(self.path, version)
        if version < SCHEMA_VERSION:
            conn.executescript(_SCHEMA)
            conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")

    # ── 读写 ──────────────────────────────────────────────────

    def initialize(self) -> None:
        """创建（0600）并建表；已存在时只校验 schema."""
        with _sqlite_errors(self.path):
            self._connect_rw().close()

    def snapshot(self) -> StoreSnapshot:
        """只读快照；文件不存在时返回空快照（不创建文件）.

        写入方中途崩溃会遗留 hot journal，只读连接无法回滚（SQLITE_READONLY_ROLLBACK）；
        此时改用读写连接打开既有文件，由 SQLite 完成回滚后再读取。
        """
        if not self.exists():
            return StoreSnapshot()
        with _sqlite_errors(self.path):
            try:
                return self._read(self._connect_existing("ro"))
            except sqlite3.OperationalError as exc:
                if _primary_code(exc) != sqlite3.SQLITE_READONLY:
                    raise
            return self._read(self._connect_existing("rw"))

    def _read(self, conn: sqlite3.Connection) -> StoreSnapshot:
        try:
            version = conn.execute("PRAGMA user_version").fetchone()[0]
            if version == 0:
                return StoreSnapshot()
            if version > SCHEMA_VERSION:
                raise StoreSchemaError(self.path, version)
            return _read_snapshot(conn, self.path)
        finally:
            conn.close()

    @contextmanager
    def transaction(self, expected: dict[str, int] | None = None) -> Iterator[StoreTxn]:
        """``BEGIN IMMEDIATE`` 写事务；``expected`` 与当前 revision 不符时抛冲突."""
        with _sqlite_errors(self.path):
            conn = self._connect_rw()
        try:
            with _sqlite_errors(self.path):
                conn.execute("BEGIN IMMEDIATE")
            try:
                snap = _read_snapshot(conn, self.path)
                conflicts = [
                    s for s, rev in (expected or {}).items() if snap.revision(s) != rev
                ]
                if conflicts:
                    raise RevisionConflict(conflicts)
                yield StoreTxn(conn, snap)
            except BaseException:
                conn.execute("ROLLBACK")
                raise
            with _sqlite_errors(self.path):
                conn.execute("COMMIT")
        finally:
            conn.close()


__all__ = [
    "SCHEMA_VERSION",
    "ConfigStore",
    "StoreTxn",
    "StoreSnapshot",
    "SecretRecord",
    "RevisionConflict",
    "ConfigStoreError",
    "StoreSchemaError",
    "StoreCorruptError",
    "StoreAccessError",
    "StoreBusyError",
]
