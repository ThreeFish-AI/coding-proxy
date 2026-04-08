"""CLI usage 命令参数测试 — 验证 -v/--vendor 及 -w/-m/-t 时间维度参数行为."""

import re
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from typer.testing import CliRunner

from coding.proxy.cli import app

runner = CliRunner()


@pytest.fixture(autouse=True)
def _isolate_cli_deps():
    """统一隔离 usage 命令的所有外部依赖，避免副作用."""
    cfg_mock = MagicMock()
    cfg_mock.db_path = Path("/tmp/test_cli_usage.db")
    cfg_mock.pricing = {}

    tl_mock = AsyncMock()

    with (
        patch("coding.proxy.cli.load_config", return_value=cfg_mock),
        patch("coding.proxy.cli.TokenLogger", return_value=tl_mock),
        patch("coding.proxy.cli.show_usage", new_callable=AsyncMock) as mock_show,
    ):
        yield mock_show


# ── A 组：help 输出验证 ──────────────────────────────────────


class TestUsageHelpOutput:
    """usage --help 应展示 -v/--vendor 和 -w/-m/-t 时间维度选项."""

    def test_help_shows_vendor_flag(self):
        result = runner.invoke(app, ["usage", "--help"])
        assert result.exit_code == 0
        clean = re.sub(r"\x1b\[[0-9;]*m", "", result.output)
        assert "--vendor" in clean
        assert "-v" in clean

    def test_help_shows_time_dimension_flags(self):
        result = runner.invoke(app, ["usage", "--help"])
        assert result.exit_code == 0
        clean = re.sub(r"\x1b\[[0-9;]*m", "", result.output)
        assert "--week" in clean
        assert "-w" in clean
        assert "--month" in clean
        assert "-m" in clean
        assert "--total" in clean
        assert "-t" in clean

    def test_help_no_backend_flag(self):
        result = runner.invoke(app, ["usage", "--help"])
        assert result.exit_code == 0
        assert "--backend" not in result.output
        assert "-b" not in result.output or "no such option" in result.output.lower()


# ── B 组：参数接受与传递 ─────────────────────────────────────


class TestVendorParameterAcceptance:
    """验证 -v / --vendor 参数被正确解析并传递至下游函数."""

    def test_short_vendor_flag(self, _isolate_cli_deps):
        mock_show = _isolate_cli_deps
        result = runner.invoke(app, ["usage", "-v", "anthropic"])
        assert result.exit_code == 0
        mock_show.assert_awaited_once()
        assert mock_show.call_args.args[2] == "anthropic"

    def test_long_vendor_flag(self, _isolate_cli_deps):
        mock_show = _isolate_cli_deps
        result = runner.invoke(app, ["usage", "--vendor", "zhipu"])
        assert result.exit_code == 0
        mock_show.assert_awaited_once()
        assert mock_show.call_args.args[2] == "zhipu"

    def test_default_no_vendor_filter(self, _isolate_cli_deps):
        mock_show = _isolate_cli_deps
        result = runner.invoke(app, ["usage"])
        assert result.exit_code == 0
        mock_show.assert_awaited_once()
        assert mock_show.call_args.args[2] is None


# ── C 组：旧参数拒绝 ─────────────────────────────────────────


class TestOldBackendFlagRejected:
    """旧的 -b / --backend 参数应被 Typer 拒绝（不再保留）."""

    def test_reject_short_backend_flag(self):
        result = runner.invoke(app, ["usage", "-b", "anthropic"])
        assert result.exit_code != 0

    def test_reject_long_backend_flag(self):
        result = runner.invoke(app, ["usage", "--backend", "anthropic"])
        assert result.exit_code != 0


# ── D 组：组合参数 ───────────────────────────────────────────


class TestCombinedParameters:
    """验证 vendor 与 days、model 等参数的组合使用."""

    def test_vendor_with_days_and_model(self, _isolate_cli_deps):
        """--model 仅保留长选项（-m 已让渡给 --month）."""
        mock_show = _isolate_cli_deps
        result = runner.invoke(
            app, ["usage", "-d", "30", "-v", "copilot", "--model", "claude-*"]
        )
        assert result.exit_code == 0
        call_args = mock_show.call_args.args
        # positional: (logger, days, vendor, model, pricing_table)
        assert call_args[1] == 30  # days
        assert call_args[2] == "copilot"  # vendor
        assert call_args[3] == "claude-*"  # model


# ── E 组：时间维度快捷选项 ───────────────────────────────────


class TestTimeDimensionFlags:
    """验证 -w (week) / -m (month) / -t (total) 时间维度快捷选项."""

    def test_week_flag_resolves(self, _isolate_cli_deps):
        mock_show = _isolate_cli_deps
        result = runner.invoke(app, ["usage", "-w"])
        assert result.exit_code == 0
        mock_show.assert_awaited_once()
        resolved_days = mock_show.call_args.args[1]
        # 本周一至今，至少 1 天，最多 7 天
        assert 1 <= resolved_days <= 7

    def test_month_flag_resolves(self, _isolate_cli_deps):
        mock_show = _isolate_cli_deps
        result = runner.invoke(app, ["usage", "-m"])
        assert result.exit_code == 0
        mock_show.assert_awaited_once()
        resolved_days = mock_show.call_args.args[1]
        # 本月 1 日至今，至少 1 天，最多 31 天
        assert 1 <= resolved_days <= 31

    def test_total_flag_resolves(self, _isolate_cli_deps):
        """-t 应解析为 None（全量查询，不限时间）."""
        mock_show = _isolate_cli_deps
        result = runner.invoke(app, ["usage", "-t"])
        assert result.exit_code == 0
        mock_show.assert_awaited_once()
        assert mock_show.call_args.args[1] is None

    def test_total_flag_long_form(self, _isolate_cli_deps):
        """--total 长选项同样应解析为 None."""
        mock_show = _isolate_cli_deps
        result = runner.invoke(app, ["usage", "--total"])
        assert result.exit_code == 0
        mock_show.assert_awaited_once()
        assert mock_show.call_args.args[1] is None

    def test_week_flag_with_vendor(self, _isolate_cli_deps):
        """时间维度可与 --vendor 组合使用."""
        mock_show = _isolate_cli_deps
        result = runner.invoke(app, ["usage", "-w", "-v", "anthropic"])
        assert result.exit_code == 0
        mock_show.assert_awaited_once()
        assert mock_show.call_args.args[2] == "anthropic"
        assert 1 <= mock_show.call_args.args[1] <= 7
