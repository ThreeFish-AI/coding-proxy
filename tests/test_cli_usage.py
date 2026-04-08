"""CLI usage 命令参数测试 — 验证 -v/--vendor 及 -w/-m/-t 时间维度参数行为.

CLI _run_usage 现在通过关键字参数调用 show_usage(period=..., count=...)，
因此断言需检查 kwargs 而非 args。
"""

import re
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from typer.testing import CliRunner

from coding.proxy.cli import _resolve_period, app
from coding.proxy.logging.db import TimePeriod

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
        for flag in ("--week", "-w", "--month", "-m", "--total", "-t"):
            assert flag in clean, f"缺少标志 {flag}"

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
        assert mock_show.call_args.kwargs["vendor"] == "anthropic"

    def test_long_vendor_flag(self, _isolate_cli_deps):
        mock_show = _isolate_cli_deps
        result = runner.invoke(app, ["usage", "--vendor", "zhipu"])
        assert result.exit_code == 0
        mock_show.assert_awaited_once()
        assert mock_show.call_args.kwargs["vendor"] == "zhipu"

    def test_default_no_vendor_filter(self, _isolate_cli_deps):
        mock_show = _isolate_cli_deps
        result = runner.invoke(app, ["usage"])
        assert result.exit_code == 0
        mock_show.assert_awaited_once()
        assert mock_show.call_args.kwargs["vendor"] is None


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
        kwargs = mock_show.call_args.kwargs
        assert kwargs["period"] is TimePeriod.DAY
        assert kwargs["count"] == 30
        assert kwargs["vendor"] == "copilot"
        assert kwargs["model"] == "claude-*"


# ── E 组：时间维度快捷选项 ───────────────────────────────────


class TestTimeDimensionFlags:
    """验证 -w (week) / -m (month) / -t (total) 时间维度快捷选项."""

    def test_week_flag(self, _isolate_cli_deps):
        mock_show = _isolate_cli_deps
        result = runner.invoke(app, ["usage", "-w"])
        assert result.exit_code == 0
        mock_show.assert_awaited_once()
        assert mock_show.call_args.kwargs["period"] is TimePeriod.WEEK
        assert mock_show.call_args.kwargs["count"] == 4

    def test_month_flag(self, _isolate_cli_deps):
        mock_show = _isolate_cli_deps
        result = runner.invoke(app, ["usage", "-m"])
        assert result.exit_code == 0
        mock_show.assert_awaited_once()
        assert mock_show.call_args.kwargs["period"] is TimePeriod.MONTH
        assert mock_show.call_args.kwargs["count"] == 3

    def test_total_flag(self, _isolate_cli_deps):
        mock_show = _isolate_cli_deps
        result = runner.invoke(app, ["usage", "-t"])
        assert result.exit_code == 0
        mock_show.assert_awaited_once()
        assert mock_show.call_args.kwargs["period"] is TimePeriod.TOTAL
        assert mock_show.call_args.kwargs["count"] == 0

    def test_total_flag_long_form(self, _isolate_cli_deps):
        mock_show = _isolate_cli_deps
        result = runner.invoke(app, ["usage", "--total"])
        assert result.exit_code == 0
        assert mock_show.call_args.kwargs["period"] is TimePeriod.TOTAL

    def test_week_long_form(self, _isolate_cli_deps):
        mock_show = _isolate_cli_deps
        result = runner.invoke(app, ["usage", "--week"])
        assert result.exit_code == 0
        assert mock_show.call_args.kwargs["period"] is TimePeriod.WEEK

    def test_month_long_form(self, _isolate_cli_deps):
        mock_show = _isolate_cli_deps
        result = runner.invoke(app, ["usage", "--month"])
        assert result.exit_code == 0
        assert mock_show.call_args.kwargs["period"] is TimePeriod.MONTH

    def test_total_overrides_month(self, _isolate_cli_deps):
        """优先级: -t > -m."""
        mock_show = _isolate_cli_deps
        result = runner.invoke(app, ["usage", "-m", "-t"])
        assert result.exit_code == 0
        assert mock_show.call_args.kwargs["period"] is TimePeriod.TOTAL

    def test_month_overrides_week(self, _isolate_cli_deps):
        """优先级: -m > -w."""
        mock_show = _isolate_cli_deps
        result = runner.invoke(app, ["usage", "-w", "-m"])
        assert result.exit_code == 0
        assert mock_show.call_args.kwargs["period"] is TimePeriod.MONTH

    def test_total_overrides_days(self, _isolate_cli_deps):
        """优先级: -t > -d."""
        mock_show = _isolate_cli_deps
        result = runner.invoke(app, ["usage", "-d", "3", "-t"])
        assert result.exit_code == 0
        assert mock_show.call_args.kwargs["period"] is TimePeriod.TOTAL

    def test_default_days_without_flags(self, _isolate_cli_deps):
        """不传任何时间维度标志时，默认 -d 7."""
        mock_show = _isolate_cli_deps
        result = runner.invoke(app, ["usage"])
        assert result.exit_code == 0
        assert mock_show.call_args.kwargs["period"] is TimePeriod.DAY
        assert mock_show.call_args.kwargs["count"] == 7

    def test_week_with_vendor(self, _isolate_cli_deps):
        """时间维度可与 --vendor 组合使用."""
        mock_show = _isolate_cli_deps
        result = runner.invoke(app, ["usage", "-w", "-v", "anthropic"])
        assert result.exit_code == 0
        assert mock_show.call_args.kwargs["period"] is TimePeriod.WEEK
        assert mock_show.call_args.kwargs["vendor"] == "anthropic"

    def test_month_with_model(self, _isolate_cli_deps):
        """时间维度可与 --model 组合使用."""
        mock_show = _isolate_cli_deps
        result = runner.invoke(
            app, ["usage", "-m", "--model", "claude-sonnet-4"]
        )
        assert result.exit_code == 0
        assert mock_show.call_args.kwargs["period"] is TimePeriod.MONTH
        assert mock_show.call_args.kwargs["model"] == "claude-sonnet-4"


# ── F 组：_resolve_period 单元测试 ───────────────────────────


class TestResolvePeriod:
    """_resolve_period 纯函数的边界与优先级测试."""

    def test_all_false_returns_day(self):
        period, count = _resolve_period(14, False, False, False)
        assert period is TimePeriod.DAY
        assert count == 14

    def test_week_returns_week_period(self):
        period, count = _resolve_period(14, True, False, False)
        assert period is TimePeriod.WEEK
        assert count == 4

    def test_month_returns_month_period(self):
        period, count = _resolve_period(14, False, True, False)
        assert period is TimePeriod.MONTH
        assert count == 3

    def test_total_returns_total_period(self):
        period, count = _resolve_period(14, False, False, True)
        assert period is TimePeriod.TOTAL
        assert count == 0

    def test_total_over_month(self):
        period, count = _resolve_period(14, False, True, True)
        assert period is TimePeriod.TOTAL

    def test_month_over_week(self):
        period, count = _resolve_period(14, True, True, False)
        assert period is TimePeriod.MONTH

    def test_total_over_all(self):
        period, count = _resolve_period(14, True, True, True)
        assert period is TimePeriod.TOTAL
