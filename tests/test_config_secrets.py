"""密钥：Fernet 主密钥、三态解析、物化规则."""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from cryptography.fernet import Fernet

from coding.proxy.config.home import config_db_path, master_key_path, proxy_home
from coding.proxy.config.importer import ensure_store_ready
from coding.proxy.config.resolver import resolve_config
from coding.proxy.config.secrets import (
    ENV_MASTER_KEY,
    MasterKeyMismatch,
    SecretCipher,
    secret_hint,
)
from coding.proxy.config.service import ConfigService
from coding.proxy.config.store import ConfigStore, SecretRecord

PLAINTEXT = "sk-test-0123456789abcdef-PLANT"


def _store() -> ConfigStore:
    ensure_store_ready()
    return ConfigStore(config_db_path())


def _put_stored(store: ConfigStore, path: str, plaintext: str) -> SecretCipher:
    cipher = SecretCipher(proxy_home())
    with store.transaction() as txn:
        cipher.ensure_for_write(txn)
        txn.put_secret(path, cipher.make_record(plaintext))
    return cipher


def _vendor(cfg, vendor_type: str):
    return next(v for v in cfg.vendors if v.vendor == vendor_type)


class TestCipher:
    def test_roundtrip_and_key_check_canary(self):
        store = _store()
        cipher = _put_stored(store, "vendors.zhipu.api_key", PLAINTEXT)
        meta = store.snapshot().meta
        assert meta["key_check"] and meta["key_id"] == cipher.key_id
        if os.name == "posix":
            assert master_key_path().stat().st_mode & 0o777 == 0o600
        reopened = SecretCipher(proxy_home())
        reopened.open(meta)
        record = store.snapshot().secrets["vendors.zhipu.api_key"]
        assert reopened.decrypt(record.ciphertext) == PLAINTEXT
        assert PLAINTEXT.encode() not in config_db_path().read_bytes()

    def test_mismatch_never_regenerates(self, monkeypatch):
        store = _store()
        _put_stored(store, "vendors.zhipu.api_key", PLAINTEXT)
        key_before = master_key_path().read_bytes()
        monkeypatch.setenv(ENV_MASTER_KEY, Fernet.generate_key().decode())
        cipher = SecretCipher(proxy_home())
        with pytest.raises(MasterKeyMismatch) as exc_info:
            with store.transaction() as txn:
                cipher.ensure_for_write(txn)
        message = str(exc_info.value)
        assert ENV_MASTER_KEY in message and "key_id=" in message
        assert os.environ[ENV_MASTER_KEY] not in message
        assert master_key_path().read_bytes() == key_before
        with pytest.raises(MasterKeyMismatch):
            resolve_config()

    def test_missing_key_file_is_not_recreated(self):
        store = _store()
        _put_stored(store, "vendors.zhipu.api_key", PLAINTEXT)
        master_key_path().unlink()
        with pytest.raises(MasterKeyMismatch, match="缺少主密钥"):
            with store.transaction() as txn:
                SecretCipher(proxy_home()).ensure_for_write(txn)
        assert not master_key_path().exists()

    def test_env_key_overrides_file(self, monkeypatch):
        key = Fernet.generate_key()
        monkeypatch.setenv(ENV_MASTER_KEY, key.decode())
        store = _store()
        cipher = _put_stored(store, "vendors.kimi.api_key", PLAINTEXT)
        assert not master_key_path().exists()
        assert store.snapshot().meta["key_id"] == cipher.key_id
        cfg = resolve_config().config
        assert _vendor(cfg, "kimi").api_key == PLAINTEXT

    def test_decrypt_false_needs_no_key(self):
        store = _store()
        _put_stored(store, "vendors.zhipu.api_key", PLAINTEXT)
        master_key_path().unlink()
        resolved = resolve_config(decrypt=False)
        assert _vendor(resolved.config, "zhipu").api_key == ""
        state = resolved.secret_states["vendors.zhipu.api_key"]
        assert state["kind"] == "stored" and state["resolved"] is False
        assert state["hint"] == "••••LANT"


class TestResolution:
    def test_three_states_and_defaults(self, monkeypatch):
        monkeypatch.setenv("MY_KIMI_KEY", "kimi-from-env")
        monkeypatch.setenv("ZHIPU_API_KEY", "zhipu-default-env")
        store = _store()
        _put_stored(store, "vendors.minimax.api_key", PLAINTEXT)
        with store.transaction() as txn:
            txn.put_secret(
                "vendors.kimi.api_key", SecretRecord("env", env_var="MY_KIMI_KEY")
            )
            txn.put_secret("vendors.doubao.api_key", SecretRecord("empty"))
            txn.put_section("vendors", {"xiaomi": {"enabled": True}})
        resolved = resolve_config()
        cfg = resolved.config
        assert _vendor(cfg, "minimax").api_key == PLAINTEXT
        assert _vendor(cfg, "kimi").api_key == "kimi-from-env"
        assert _vendor(cfg, "doubao").api_key == ""
        # 无记录 + 默认 vendor → 包内默认 ${ZHIPU_API_KEY}
        assert _vendor(cfg, "zhipu").api_key == "zhipu-default-env"
        # 无记录 + 已覆盖 vendor → Pydantic 默认 ""
        assert _vendor(cfg, "xiaomi").api_key == ""

        states = resolved.secret_states
        assert states["vendors.minimax.api_key"]["kind"] == "stored"
        assert states["vendors.minimax.api_key"]["resolved"] is True
        assert states["vendors.kimi.api_key"] == {
            "kind": "env",
            "env_var": "MY_KIMI_KEY",
            "resolved": True,
            "hint": "",
            "default_env": "KIMI_API_KEY",
            "recorded": True,
        }
        assert states["vendors.doubao.api_key"]["kind"] == "empty"
        assert states["vendors.zhipu.api_key"]["kind"] == "env"
        assert states["vendors.zhipu.api_key"]["env_var"] == "ZHIPU_API_KEY"
        assert states["vendors.xiaomi.api_key"]["kind"] == "empty"
        assert states["auth.google_client_secret"]["kind"] == "default"
        # recorded = 存在 stored/env 记录：empty 记录与 default/默认 ${VAR} 推导为 False
        assert states["vendors.minimax.api_key"]["recorded"] is True
        assert states["vendors.kimi.api_key"]["recorded"] is True
        assert states["vendors.doubao.api_key"]["recorded"] is False
        assert states["vendors.zhipu.api_key"]["recorded"] is False
        assert states["vendors.xiaomi.api_key"]["recorded"] is False
        assert states["auth.google_client_secret"]["recorded"] is False

    def test_unset_env_resolves_empty(self):
        _store()
        resolved = resolve_config()
        assert _vendor(resolved.config, "zhipu").api_key == ""
        assert resolved.secret_states["vendors.zhipu.api_key"]["resolved"] is False

    def test_hint_rule(self):
        assert secret_hint("short-secret") == ""
        assert secret_hint("x" * 15 + "WXYZ") == "WXYZ"

    def test_auth_google_client_secret_record(self, monkeypatch):
        monkeypatch.setenv("G_SECRET", "from-env")
        store = _store()
        with store.transaction() as txn:
            txn.put_secret(
                "auth.google_client_secret", SecretRecord("env", env_var="G_SECRET")
            )
        assert resolve_config().config.auth.google_client_secret == "from-env"


class TestMaterialisation:
    def test_new_vendor_override_copies_default_env_source(self, monkeypatch):
        monkeypatch.setenv("ZHIPU_API_KEY", "zhipu-env-value")
        monkeypatch.setenv("GOOG_REFRESH_TOKEN", "goog-refresh")
        store = _store()
        service = ConfigService()
        vendors = service.view()["values"]["vendors"]
        for vendor in vendors:
            if vendor["vendor"] in ("zhipu", "antigravity"):
                vendor["timeout_ms"] = 1234
        result = service.apply_changes(
            {"revisions": {"vendors": 0}, "sections": {"vendors": vendors}},
            actor="dashboard",
            allow_bootstrap=False,
            allow_env_any=False,
        )
        secrets = store.snapshot().secrets
        assert secrets["vendors.zhipu.api_key"] == SecretRecord(
            "env", env_var="ZHIPU_API_KEY"
        )
        assert (
            secrets["vendors.antigravity.refresh_token"].env_var == "GOOG_REFRESH_TOKEN"
        )
        assert "vendors.antigravity.client_id" in secrets
        assert _vendor(result.config, "zhipu").api_key == "zhipu-env-value"
        assert _vendor(result.config, "zhipu").timeout_ms == 1234
        assert resolve_config().config == result.config


def test_master_key_file_path_is_under_home():
    assert master_key_path() == Path(proxy_home()) / "master.key"
