"""config.db 存储层 + home 私有文件工具测试."""

from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from coding.proxy.config.home import (
    admin_token_path,
    create_private_file,
    ensure_home,
    is_valid_admin_token,
    load_or_create_admin_token,
    proxy_home,
    write_private_atomic,
)
from coding.proxy.config.store import (
    SCHEMA_VERSION,
    ConfigStore,
    ConfigStoreError,
    RevisionConflict,
    SecretRecord,
    StoreAccessError,
    StoreBusyError,
    StoreCorruptError,
    StoreSchemaError,
)

posix_only = pytest.mark.skipif(os.name != "posix", reason="POSIX 权限语义")


@pytest.fixture
def store(tmp_path: Path) -> ConfigStore:
    return ConfigStore(tmp_path / "cfg" / "config.db")


class TestCreation:
    @posix_only
    def test_file_created_0600_with_schema(self, store: ConfigStore):
        store.initialize()
        assert store.path.stat().st_mode & 0o777 == 0o600
        conn = sqlite3.connect(store.path)
        try:
            assert conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
            tables = {
                r[0]
                for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
        finally:
            conn.close()
        assert {"config_section", "secret", "meta"} <= tables

    def test_snapshot_does_not_create_file(self, store: ConfigStore):
        snap = store.snapshot()
        assert snap.sections == {} and snap.revisions == {}
        assert not store.path.exists()
        assert not store.path.parent.exists()

    def test_secure_delete_and_journal_mode(self, store: ConfigStore):
        conn = store._connect_rw()
        try:
            assert conn.execute("PRAGMA secure_delete").fetchone()[0] == 1
            assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "delete"
            assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == 2000
        finally:
            conn.close()

    def test_newer_schema_rejected(self, store: ConfigStore):
        store.initialize()
        conn = sqlite3.connect(store.path)
        conn.execute("PRAGMA user_version = 99")
        conn.close()
        with pytest.raises(Exception, match="schema"):
            store.snapshot()


class TestRevisions:
    def test_put_bumps_revision_and_roundtrips_json(self, store: ConfigStore):
        with store.transaction({"tiers": 0}) as txn:
            assert txn.put_section("tiers", None) == 1
        snap = store.snapshot()
        assert "tiers" in snap.sections and snap.sections["tiers"] is None
        assert snap.revision("tiers") == 1
        with store.transaction({"tiers": 1}) as txn:
            assert txn.put_section("tiers", ["zhipu"]) == 2
        assert store.snapshot().sections["tiers"] == ["zhipu"]

    def test_reset_still_bumps_revision(self, store: ConfigStore):
        with store.transaction() as txn:
            txn.put_section("failover", {"status_codes": [500]})
        with store.transaction({"failover": 1}) as txn:
            assert txn.delete_section("failover") == 2
        snap = store.snapshot()
        assert "failover" not in snap.sections
        assert snap.revision("failover") == 2
        with store.transaction({"failover": 2}) as txn:
            assert txn.put_section("failover", {}) == 3

    def test_never_overridden_section_is_revision_zero(self, store: ConfigStore):
        store.initialize()
        assert store.snapshot().revision("pricing") == 0

    def test_revision_conflict_rolls_back(self, store: ConfigStore):
        with store.transaction() as txn:
            txn.put_section("vendors", {"zhipu": {"enabled": False}})
        with pytest.raises(RevisionConflict) as exc_info:
            with store.transaction({"vendors": 0, "tiers": 0}) as txn:
                txn.put_section("tiers", ["zhipu"])
        assert exc_info.value.sections == ["vendors"]
        snap = store.snapshot()
        assert "tiers" not in snap.sections
        assert snap.revision("vendors") == 1

    def test_exception_inside_transaction_rolls_back(self, store: ConfigStore):
        store.initialize()
        with pytest.raises(RuntimeError):
            with store.transaction() as txn:
                txn.put_section("tiers", ["zhipu"])
                raise RuntimeError("boom")
        assert store.snapshot().sections == {}


class TestSecretsAndMeta:
    def test_secret_records_roundtrip_and_delete(self, store: ConfigStore):
        with store.transaction() as txn:
            txn.put_secret("vendors.zhipu.api_key", SecretRecord("env", env_var="X"))
            txn.put_secret(
                "vendors.kimi.api_key", SecretRecord("stored", ciphertext=b"c", hint="")
            )
            txn.set_meta("k", "v")
        snap = store.snapshot()
        assert snap.secrets["vendors.zhipu.api_key"].env_var == "X"
        assert snap.secrets["vendors.kimi.api_key"].ciphertext == b"c"
        assert snap.meta["k"] == "v"
        assert snap.meta["secrets_revision"] == "1"
        with store.transaction() as txn:
            txn.put_secret("vendors.kimi.api_key", None)
        snap = store.snapshot()
        assert "vendors.kimi.api_key" not in snap.secrets
        assert snap.meta["secrets_revision"] == "2"

    def test_record_repr_hides_ciphertext(self):
        record = SecretRecord("stored", ciphertext=b"top-secret-bytes")
        assert "top-secret-bytes" not in repr(record)

    def test_unknown_kind_rejected(self):
        with pytest.raises(ValueError):
            SecretRecord("plain")


class TestHomeHelpers:
    def test_proxy_home_uses_env(self, tmp_path: Path, monkeypatch):
        monkeypatch.setenv("CODING_PROXY_HOME", str(tmp_path / "x"))
        assert proxy_home() == (tmp_path / "x").resolve()

    @posix_only
    def test_create_private_file_is_exclusive_0600(self, tmp_path: Path):
        path = tmp_path / "secret.key"
        create_private_file(path, b"abc")
        assert path.stat().st_mode & 0o777 == 0o600
        with pytest.raises(FileExistsError):
            create_private_file(path, b"other")
        assert path.read_bytes() == b"abc"

    @posix_only
    def test_write_private_atomic_replaces(self, tmp_path: Path):
        path = tmp_path / "out.yaml"
        path.write_text("old")
        write_private_atomic(path, b"new")
        assert path.read_bytes() == b"new"
        assert path.stat().st_mode & 0o777 == 0o600
        assert [p.name for p in tmp_path.iterdir()] == ["out.yaml"]

    @posix_only
    def test_admin_token_created_once(self):
        token = load_or_create_admin_token()
        assert len(token) >= 40
        assert admin_token_path().stat().st_mode & 0o777 == 0o600
        assert load_or_create_admin_token() == token
        assert [p.name for p in proxy_home().iterdir()] == ["admin_token"]

    @posix_only
    @pytest.mark.parametrize("content", [b"", b"\n", b"short", b"x" * 31, b"\xff" * 40])
    def test_invalid_admin_token_is_regenerated(self, content: bytes):
        """空 / 截断 / 非 urlsafe 的 token 文件（崩溃或并发首启遗留）不得被当作有效 token."""
        ensure_home()
        admin_token_path().write_bytes(content)
        token = load_or_create_admin_token()
        assert is_valid_admin_token(token)
        assert admin_token_path().read_text().strip() == token
        assert admin_token_path().stat().st_mode & 0o777 == 0o600
        assert [p.name for p in proxy_home().iterdir()] == ["admin_token"]

    def test_admin_token_validity_rule(self):
        assert not is_valid_admin_token("")
        assert not is_valid_admin_token("a" * 31)
        assert not is_valid_admin_token("a" * 32 + "\n")
        assert is_valid_admin_token("a" * 32)


class TestIsolationGuard:
    def test_guard_blocks_real_home_access(self):
        from tests import conftest

        probe = os.path.join(conftest._REAL_ROOTS[0], "__guard_probe__")
        before = len(conftest._violations)
        # audit hook 在真正打开之前拦截
        with pytest.raises(PermissionError):
            open(probe, "rb")
        with pytest.raises(PermissionError):
            sqlite3.connect(probe)
        assert len(conftest._violations) == before + 2
        del conftest._violations[before:]


class TestStoreErrors:
    def _enter_txn(self, store: ConfigStore) -> None:
        with store.transaction():
            pass

    def test_newer_schema(self, store: ConfigStore):
        store.initialize()
        conn = sqlite3.connect(store.path)
        conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION + 1}")
        conn.close()
        for action in (
            store.snapshot,
            store.initialize,
            lambda: self._enter_txn(store),
        ):
            with pytest.raises(StoreSchemaError) as exc_info:
                action()
            assert str(store.path) in str(exc_info.value)
            assert "CODING_PROXY_HOME" in str(exc_info.value)

    def test_corrupt_file(self, store: ConfigStore):
        store.path.parent.mkdir(parents=True)
        store.path.write_bytes(b"not a database at all" * 100)
        for action in (store.snapshot, lambda: self._enter_txn(store)):
            with pytest.raises(StoreCorruptError) as exc_info:
                action()
            assert isinstance(exc_info.value, ConfigStoreError)
            assert str(store.path) in str(exc_info.value)

    def test_invalid_json_row_is_corrupt(self, store: ConfigStore):
        """config_section.data 被改成非法 JSON：映射为 StoreCorruptError 而非裸 traceback."""
        store.initialize()
        with store.transaction() as txn:
            txn.put_section("tiers", ["zhipu"])
        conn = sqlite3.connect(store.path)
        conn.execute("UPDATE config_section SET data = '{oops: 1}'")
        conn.commit()
        conn.close()
        for action in (store.snapshot, lambda: self._enter_txn(store)):
            with pytest.raises(StoreCorruptError) as exc_info:
                action()
            assert "tiers" in str(exc_info.value)
            assert "JSON" in str(exc_info.value)
            assert str(store.path) in str(exc_info.value)

    def test_hot_journal_is_recovered_by_snapshot(self, store: ConfigStore):
        """写入方在事务中崩溃遗留 hot journal：快照应完成回滚并返回提交前的数据."""
        with store.transaction() as txn:
            txn.put_section("tiers", ["zhipu"])
        child = (
            "import os, sqlite3, sys\n"
            "conn = sqlite3.connect(sys.argv[1], isolation_level=None)\n"
            "conn.execute('PRAGMA cache_size = 1')\n"
            "conn.execute('BEGIN IMMEDIATE')\n"
            "for i in range(3000):\n"
            "    conn.execute('INSERT OR REPLACE INTO meta(key, value) VALUES (?, ?)',"
            " (f'k{i}', 'x' * 200))\n"
            "os._exit(0)\n"
        )
        subprocess.run([sys.executable, "-c", child, str(store.path)], check=True)
        journal = store.path.with_name(store.path.name + "-journal")
        assert journal.exists()

        snap = store.snapshot()
        assert snap.sections == {"tiers": ["zhipu"]}
        assert not any(k.startswith("k") for k in snap.meta)
        assert not journal.exists()

    @posix_only
    def test_unreadable_store_is_access_error_not_corrupt(self, store: ConfigStore):
        store.initialize()
        store.path.chmod(0o000)
        try:
            if os.access(store.path, os.R_OK):
                pytest.skip("当前用户可绕过文件权限（如 root）")
            with pytest.raises(StoreAccessError) as exc_info:
                store.snapshot()
            assert not isinstance(exc_info.value, StoreCorruptError)
            assert "不要删除" in str(exc_info.value)
        finally:
            store.path.chmod(0o600)

    def test_locked_store_is_busy(self, store: ConfigStore, monkeypatch):
        store.initialize()
        monkeypatch.setattr(
            ConfigStore,
            "_configure",
            staticmethod(lambda conn: conn.execute("PRAGMA busy_timeout = 50")),
        )
        holder = sqlite3.connect(store.path, isolation_level=None)
        holder.execute("BEGIN IMMEDIATE")
        try:
            with pytest.raises(StoreBusyError):
                self._enter_txn(store)
        finally:
            holder.execute("ROLLBACK")
            holder.close()
