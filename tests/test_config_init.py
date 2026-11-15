"""首次运行行为：不再复制默认配置到用户目录，路径默认值由 home 推导."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

import coding.proxy.config.loader as loader_module
from coding.proxy.auth.store import TokenStoreManager
from coding.proxy.config.home import (
    ensure_home,
    legacy_home_yaml_path,
    proxy_home,
)
from coding.proxy.config.loader import load_config
from coding.proxy.config.resolver import resolve_config
from coding.proxy.config.schema import ProxyConfig


class TestNoFirstRunCopy:
    def test_ensure_user_config_removed(self):
        assert not hasattr(loader_module, "_ensure_user_config")

    def test_load_config_does_not_create_home_yaml(self):
        cfg = load_config()
        assert cfg.server.port == 3392
        assert not legacy_home_yaml_path().exists()
        assert not proxy_home().exists()

    def test_resolve_config_has_no_side_effects(self):
        resolved = resolve_config()
        assert resolved.mode == "defaults"
        assert resolved.config == load_config(None)
        assert not proxy_home().exists()

    def test_explicit_path_is_never_rewritten(self, tmp_path: Path):
        cfg_file = tmp_path / "my-config.yaml"
        cfg_file.write_text("server:\n  port: 9999\n")
        before = cfg_file.read_bytes()
        cfg = load_config(cfg_file)
        assert cfg.server.port == 9999
        assert cfg_file.read_bytes() == before
        assert not proxy_home().exists()

    def test_cwd_config_still_read_by_legacy_loader(self):
        Path("config.yaml").write_text("server:\n  port: 7777\n")
        assert load_config().server.port == 7777
        assert not legacy_home_yaml_path().exists()


class TestHomeDerivedPaths:
    def test_paths_follow_coding_proxy_home(self, tmp_path: Path, monkeypatch):
        monkeypatch.setenv("CODING_PROXY_HOME", str(tmp_path / "profile"))
        cfg = ProxyConfig()
        home = (tmp_path / "profile").resolve()
        assert cfg.db_path == home / "usage.db"
        assert cfg.compat_state_path == home / "compat.db"
        assert Path(cfg.auth.token_store_path) == home / "tokens.json"
        assert TokenStoreManager()._path == home / "tokens.json"

    def test_unset_home_env_matches_old_expanded_literals(self, monkeypatch):
        monkeypatch.delenv("CODING_PROXY_HOME", raising=False)
        cfg = ProxyConfig()
        assert cfg.database.path == str(Path("~/.coding-proxy/usage.db").expanduser())
        assert cfg.database.compat_state_path == str(
            Path("~/.coding-proxy/compat.db").expanduser()
        )
        assert cfg.auth.token_store_path == str(
            Path("~/.coding-proxy/tokens.json").expanduser()
        )

    def test_defaults_are_evaluated_per_instance(self, tmp_path: Path, monkeypatch):
        monkeypatch.setenv("CODING_PROXY_HOME", str(tmp_path / "a"))
        first = ProxyConfig()
        monkeypatch.setenv("CODING_PROXY_HOME", str(tmp_path / "b"))
        second = ProxyConfig()
        assert first.db_path.parent.name == "a"
        assert second.db_path.parent.name == "b"

    def test_default_yaml_has_no_home_literals(self):
        path = loader_module._get_default_config_path()
        assert path is not None
        text = path.read_text(encoding="utf-8")
        assert 'path: "~/.coding-proxy' not in text
        assert 'token_store_path: "~/.coding-proxy' not in text

    @pytest.mark.skipif(os.name != "posix", reason="POSIX 权限语义")
    def test_ensure_home_is_private(self):
        home = ensure_home()
        assert home == proxy_home()
        assert home.stat().st_mode & 0o777 == 0o700
        home.chmod(0o755)
        ensure_home()
        assert home.stat().st_mode & 0o777 == 0o700
