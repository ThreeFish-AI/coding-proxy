"""CLI：config 子命令、start 引导、usage --db、离线命令只读回退."""

from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import typer
import yaml
from typer.testing import CliRunner

from coding.proxy.cli import app, auth_commands, config_commands
from coding.proxy.config.home import (
    admin_token_path,
    config_db_path,
    legacy_home_yaml_path,
)
from coding.proxy.config.resolver import resolve_config
from coding.proxy.config.store import ConfigStore

runner = CliRunner()
PLANTED = "sk-PLANTED-cli-secret-0123456789"


def _ok(args: list[str], **kw):
    result = runner.invoke(app, args, **kw)
    assert result.exit_code == 0, result.output
    return result


def _zhipu(cfg):
    return next(v for v in cfg.vendors if v.vendor == "zhipu")


def _start(*args: str):
    with (
        patch("uvicorn.run"),
        patch("coding.proxy.server.app.create_app", return_value=MagicMock()),
        patch("coding.proxy.cli._auto_login_if_needed", new_callable=AsyncMock),
        patch("coding.proxy.cli.banner.print_banner"),
    ):
        return runner.invoke(app, ["start", *args])


def _flat(output: str) -> str:
    """rich 按终端宽度折行；断言前去掉换行."""
    return output.replace("\n", "")


def test_all_typer_apps_hide_locals():
    for typer_app in (app, auth_commands.app, config_commands.app):
        assert isinstance(typer_app, typer.Typer)
        assert typer_app.pretty_exceptions_show_locals is False


def test_config_path():
    output = _ok(["config", "path"]).output.replace("\n", "")
    assert "config.db" in output and "master.key" in output


def test_config_set_and_reset():
    result = _ok(["config", "set", "vendors.zhipu.timeout_ms", "1234"])
    assert "已保存" in result.output
    assert _zhipu(resolve_config().config).timeout_ms == 1234

    result = _ok(["config", "set", "server.host", "0.0.0.0"])
    assert "需重启" in result.output
    assert resolve_config().config.server.host == "0.0.0.0"

    _ok(["config", "reset", "server"])
    assert resolve_config().config.server.host == "127.0.0.1"


def test_config_set_rejects_unknown_path():
    result = runner.invoke(app, ["config", "set", "vendors.zhipu.bogus", "1"])
    assert result.exit_code == 1
    assert "未知配置路径" in result.output


def test_set_secret_stdin_never_echoed():
    result = _ok(
        ["config", "set-secret", "vendors.zhipu.api_key", "--stdin"],
        input=PLANTED + "\n",
    )
    assert PLANTED not in result.output
    assert _zhipu(resolve_config().config).api_key == PLANTED
    assert PLANTED.encode() not in config_db_path().read_bytes()

    export = _ok(["config", "export"]).stdout
    assert PLANTED not in export
    zhipu = next(v for v in yaml.safe_load(export)["vendors"] if v["vendor"] == "zhipu")
    assert zhipu["api_key"] == "<redacted>"


def test_set_secret_env_and_option_validation(monkeypatch):
    monkeypatch.setenv("MY_KIMI", "kimi-env")
    _ok(["config", "set-secret", "vendors.kimi.api_key", "--env", "MY_KIMI"])
    kimi = next(v for v in resolve_config().config.vendors if v.vendor == "kimi")
    assert kimi.api_key == "kimi-env"
    result = runner.invoke(
        app, ["config", "set-secret", "vendors.kimi.api_key", "--clear", "--stdin"]
    )
    assert result.exit_code == 2


def test_export_to_file_and_import_dry_run():
    _ok(["config", "set", "vendors.zhipu.timeout_ms", "77"])
    out = Path.cwd().parent / "exported.yaml"
    _ok(["config", "export", "--out", str(out)])
    if os.name == "posix":
        assert out.stat().st_mode & 0o777 == 0o600
    revision = ConfigStore(config_db_path()).snapshot().revision("vendors")
    result = _ok(["config", "import", str(out), "--dry-run"])
    assert "dry-run" in result.output and "vendors" in result.output
    assert ConfigStore(config_db_path()).snapshot().revision("vendors") == revision


def test_import_dry_run_does_not_create_store():
    src = Path.cwd().parent / "legacy.yaml"
    src.write_text("server:\n  port: 4444\n")
    result = _ok(["config", "import", str(src), "--dry-run"])
    assert "server" in result.output
    assert not config_db_path().exists()
    _ok(["config", "import", str(src)])
    assert resolve_config().config.server.port == 4444


def test_config_token():
    token = _ok(["config", "token"]).stdout.strip()
    assert token == admin_token_path().read_text().strip()
    assert _ok(["config", "token"]).stdout.strip() == token


def test_config_token_regenerates_empty_file():
    admin_token_path().parent.mkdir(parents=True)
    admin_token_path().write_bytes(b"")
    token = _ok(["config", "token"]).stdout.strip()
    assert len(token) >= 32
    assert admin_token_path().read_text().strip() == token


def test_commands_recover_from_hot_journal():
    """写入方崩溃遗留 hot journal 时，start / config 命令照常工作（不报“文件损坏”）."""
    _ok(["config", "set", "server.port", "4000"])
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
    subprocess.run([sys.executable, "-c", child, str(config_db_path())], check=True)
    assert config_db_path().with_name("config.db-journal").exists()
    result = _start()
    assert result.exit_code == 0, result.output
    assert "4000" in _ok(["config", "export"]).stdout


def test_write_commands_require_migration_when_yaml_pending():
    legacy_home_yaml_path().parent.mkdir(parents=True)
    legacy_home_yaml_path().write_text("server:\n  port: 1\n")
    for args in (["config", "set", "server.port", "2"], ["config", "export"]):
        result = runner.invoke(app, args)
        assert result.exit_code == 1
        assert "尚未迁移" in result.output
    assert not config_db_path().exists()


class TestUsage:
    @pytest.fixture
    def usage_mocks(self):
        with (
            patch("coding.proxy.cli.TokenLogger") as token_logger,
            patch("coding.proxy.cli.show_usage", new_callable=AsyncMock),
        ):
            token_logger.return_value = AsyncMock()
            yield token_logger

    def test_db_option_is_honoured(self, usage_mocks):
        db = Path.cwd().parent / "custom-usage.db"
        _ok(["usage", "--db", str(db)])
        usage_mocks.assert_called_once_with(db)

    def test_default_db_from_resolved_config(self, usage_mocks):
        _ok(["usage"])
        usage_mocks.assert_called_once_with(resolve_config().config.db_path)

    def test_pre_migration_reads_yaml_readonly(self, usage_mocks):
        legacy_home_yaml_path().parent.mkdir(parents=True)
        db = Path.cwd().parent / "from-yaml.db"
        legacy_home_yaml_path().write_text(f'database:\n  path: "{db}"\n')
        result = _ok(["usage"])
        usage_mocks.assert_called_once_with(db)
        assert "尚未迁移" in result.output
        assert not config_db_path().exists()


class TestStart:
    def test_start_boots_store_and_applies_cli_overrides(self):
        legacy_home_yaml_path().parent.mkdir(parents=True)
        legacy_home_yaml_path().write_text("server:\n  port: 4100\n")
        captured: dict = {}

        def _fake_run(app_obj, **kwargs):
            captured.update(kwargs)

        with (
            patch("uvicorn.run", side_effect=_fake_run),
            patch("coding.proxy.server.app.create_app", return_value=MagicMock()),
            patch("coding.proxy.cli._auto_login_if_needed", new_callable=AsyncMock),
            patch("coding.proxy.cli.banner.print_banner"),
        ):
            result = runner.invoke(app, ["start", "-p", "4200"])
        assert result.exit_code == 0, result.output
        assert captured["port"] == 4200
        assert resolve_config().config.server.port == 4100
        assert config_db_path().exists()

    def test_start_refuses_stale_yaml(self):
        with (
            patch("uvicorn.run") as run,
            patch("coding.proxy.cli._auto_login_if_needed", new_callable=AsyncMock),
        ):
            Path("config.yaml").write_text("server:\n  port: 1\n")
            result = runner.invoke(app, ["start"])
        assert result.exit_code == 2
        assert "config import" in result.output
        run.assert_not_called()

    def test_start_ignores_third_party_cwd_yaml(self):
        Path("config.yaml").write_text("foo: bar\nlint: {rules: []}\n")
        for _ in range(2):
            result = _start()
            assert result.exit_code == 0, result.output


class TestBrokenStore:
    @pytest.fixture(params=["newer_schema", "garbage"])
    def broken(self, request) -> Path:
        path = config_db_path()
        path.parent.mkdir(parents=True)
        if request.param == "newer_schema":
            conn = sqlite3.connect(path)
            conn.execute("PRAGMA user_version = 99")
            conn.close()
        else:
            path.write_bytes(b"not a database at all" * 100)
        return path

    @pytest.mark.parametrize(
        ("args", "code"),
        [
            (["start"], 2),
            (["usage"], 1),
            (["auth", "status"], 1),
            (["config", "export"], 1),
            (["config", "set", "server.port", "1"], 1),
        ],
    )
    def test_one_line_error_without_traceback(self, broken, args, code):
        with patch("coding.proxy.cli.TokenLogger"):
            result = _start() if args == ["start"] else runner.invoke(app, args)
        assert result.exit_code == code, result.output
        assert isinstance(result.exception, SystemExit)
        assert "config.db" in _flat(result.output)
        assert "CODING_PROXY_HOME" in _flat(result.output)


def test_reset_vendors_reports_pinned_secret(monkeypatch):
    monkeypatch.setenv("ZHIPU_API_KEY", "zhipu-env-value")
    source = Path.cwd().parent / "zhipu.yaml"
    source.write_text(
        "vendors:\n  - vendor: anthropic\n  - vendor: zhipu\n    timeout_ms: 1\n"
        "tiers: [zhipu, anthropic]\n"
    )
    _ok(["config", "import", str(source)])
    output = _flat(_ok(["config", "reset", "vendors"]).output)
    assert "vendors.zhipu.api_key" in output and "--default-env" in output
    assert _zhipu(resolve_config().config).api_key == ""
