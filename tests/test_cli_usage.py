"""CLI usage 命令参数测试 — 验证 -v/--vendor 及 -w/-m/-t 时间维度参数行为."""

import re
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from typer.testing import CliRunner

from coding.proxy.cli import app
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


def _kwargs(mock_show):
    """提取 show_usage 的关键字参数."""
    return mock_show.call_args.kwargs


# ── A 组：help 输出验证 ──────────────────────────────────────


class TestUsageHelpOutput:
    """usage --help 应展示所有时间维度选项."""

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

    def test_help_shows_model_long_only(self):
        """--model 应仅保留长选项（-m 已让渡给 --month）."""
        result = runner.invoke(app, ["usage", "--help"])
        assert result.exit_code == 0
        clean = re.sub(r"\x1b\[[0-9;]*m", "", result.output)
        assert "--model" in clean


# ── B 组：参数接受与传递 ─────────────────────────────────────


class TestVendorParameterAcceptance:
    """验证 -v / --vendor 参数被正确解析并传递至下游函数."""

    def test_short_vendor_flag(self, _isolate_cli_deps):
        mock_show = _isolate_cli_deps
        result = runner.invoke(app, ["usage", "-v", "anthropic"])
        assert result.exit_code == 0
        mock_show.assert_awaited_once()
        assert _kwargs(mock_show)["vendor"] == "anthropic"

    def test_long_vendor_flag(self, _isolate_cli_deps):
        mock_show = _isolate_cli_deps
        result = runner.invoke(app, ["usage", "--vendor", "zhipu"])
        assert result.exit_code == 0
        mock_show.assert_awaited_once()
        assert _kwargs(mock_show)["vendor"] == "zhipu"

    def test_default_no_vendor_filter(self, _isolate_cli_deps):
        mock_show = _isolate_cli_deps
        result = runner.invoke(app, ["usage"])
        assert result.exit_code == 0
        mock_show.assert_awaited_once()
        assert _kwargs(mock_show)["vendor"] is None


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
        kw = _kwargs(mock_show)
        assert kw["period"] == TimePeriod.DAY
        assert kw["count"] == 30
        assert kw["vendor"] == "copilot"
        assert kw["model"] == "claude-*"


# ── E 组：时间维度快捷选项 ───────────────────────────────────


class TestTimeDimensionFlags:
    """验证 -w/-m/-t 时间维度快捷选项的解析与传递."""

    def test_default_is_day_7(self, _isolate_cli_deps):
        """不传任何时间维度参数时，默认 DAY + count=7."""
        mock_show = _isolate_cli_deps
        result = runner.invoke(app, ["usage"])
        assert result.exit_code == 0
        kw = _kwargs(mock_show)
        assert kw["period"] == TimePeriod.DAY
        assert kw["count"] == 7

    def test_custom_days(self, _isolate_cli_deps):
        mock_show = _isolate_cli_deps
        result = runner.invoke(app, ["usage", "-d", "14"])
        assert result.exit_code == 0
        kw = _kwargs(mock_show)
        assert kw["period"] == TimePeriod.DAY
        assert kw["count"] == 14

    def test_week_flag(self, _isolate_cli_deps):
        mock_show = _isolate_cli_deps
        result = runner.invoke(app, ["usage", "-w", "1"])
        assert result.exit_code == 0
        kw = _kwargs(mock_show)
        assert kw["period"] == TimePeriod.WEEK
        assert kw["count"] == 1

    def test_week_flag_count(self, _isolate_cli_deps):
        """``-w 3`` 应查询最近 3 周."""
        mock_show = _isolate_cli_deps
        result = runner.invoke(app, ["usage", "-w", "3"])
        assert result.exit_code == 0
        kw = _kwargs(mock_show)
        assert kw["period"] == TimePeriod.WEEK
        assert kw["count"] == 3

    def test_month_flag(self, _isolate_cli_deps):
        mock_show = _isolate_cli_deps
        result = runner.invoke(app, ["usage", "-m", "1"])
        assert result.exit_code == 0
        kw = _kwargs(mock_show)
        assert kw["period"] == TimePeriod.MONTH
        assert kw["count"] == 1

    def test_month_flag_count(self, _isolate_cli_deps):
        """``-m 2`` 应查询最近 2 月."""
        mock_show = _isolate_cli_deps
        result = runner.invoke(app, ["usage", "-m", "2"])
        assert result.exit_code == 0
        kw = _kwargs(mock_show)
        assert kw["period"] == TimePeriod.MONTH
        assert kw["count"] == 2

    def test_total_flag(self, _isolate_cli_deps):
        mock_show = _isolate_cli_deps
        result = runner.invoke(app, ["usage", "-t"])
        assert result.exit_code == 0
        kw = _kwargs(mock_show)
        assert kw["period"] == TimePeriod.TOTAL
        assert kw["count"] == 1

    def test_total_flag_long_form(self, _isolate_cli_deps):
        mock_show = _isolate_cli_deps
        result = runner.invoke(app, ["usage", "--total"])
        assert result.exit_code == 0
        kw = _kwargs(mock_show)
        assert kw["period"] == TimePeriod.TOTAL

    def test_week_with_vendor(self, _isolate_cli_deps):
        """时间维度可与 --vendor 组合使用."""
        mock_show = _isolate_cli_deps
        result = runner.invoke(app, ["usage", "-w", "1", "-v", "anthropic"])
        assert result.exit_code == 0
        kw = _kwargs(mock_show)
        assert kw["period"] == TimePeriod.WEEK
        assert kw["vendor"] == "anthropic"

    def test_total_overrides_days(self, _isolate_cli_deps):
        """-t 优先级高于 -d."""
        mock_show = _isolate_cli_deps
        result = runner.invoke(app, ["usage", "-d", "30", "-t"])
        assert result.exit_code == 0
        kw = _kwargs(mock_show)
        assert kw["period"] == TimePeriod.TOTAL

    def test_month_overrides_week(self, _isolate_cli_deps):
        """-m 优先级高于 -w."""
        mock_show = _isolate_cli_deps
        result = runner.invoke(app, ["usage", "-w", "1", "-m", "1"])
        assert result.exit_code == 0
        kw = _kwargs(mock_show)
        assert kw["period"] == TimePeriod.MONTH


# ── F 组：多 vendor 过滤 ─────────────────────────────────────


class TestMultiVendorFilter:
    """验证 -v 参数支持逗号分隔的多 vendor 过滤."""

    def test_single_vendor_remains_string(self, _isolate_cli_deps):
        """单个 vendor 应保持字符串类型（向后兼容）."""
        mock_show = _isolate_cli_deps
        result = runner.invoke(app, ["usage", "-v", "anthropic"])
        assert result.exit_code == 0
        assert _kwargs(mock_show)["vendor"] == "anthropic"

    def test_multi_vendor_parsed_as_list(self, _isolate_cli_deps):
        """'-v anthropic,zhipu' 应将 vendor 解析为 list 传递."""
        mock_show = _isolate_cli_deps
        result = runner.invoke(app, ["usage", "-v", "anthropic,zhipu"])
        assert result.exit_code == 0
        assert _kwargs(mock_show)["vendor"] == ["anthropic", "zhipu"]

    def test_multi_vendor_three_values(self, _isolate_cli_deps):
        """三个 vendor 同样解析为 list."""
        mock_show = _isolate_cli_deps
        result = runner.invoke(app, ["usage", "-v", "anthropic,zhipu,copilot"])
        assert result.exit_code == 0
        assert _kwargs(mock_show)["vendor"] == ["anthropic", "zhipu", "copilot"]

    def test_multi_vendor_with_spaces(self, _isolate_cli_deps):
        """逗号周围的空格应被正确去除."""
        mock_show = _isolate_cli_deps
        result = runner.invoke(app, ["usage", "-v", "anthropic , zhipu"])
        assert result.exit_code == 0
        assert _kwargs(mock_show)["vendor"] == ["anthropic", "zhipu"]

    def test_multi_vendor_combined_with_time_dim(self, _isolate_cli_deps):
        """多 vendor 过滤可与时间维度参数组合使用."""
        mock_show = _isolate_cli_deps
        result = runner.invoke(app, ["usage", "-w", "2", "-v", "anthropic,copilot"])
        assert result.exit_code == 0
        kw = _kwargs(mock_show)
        assert kw["period"] == TimePeriod.WEEK
        assert kw["count"] == 2
        assert kw["vendor"] == ["anthropic", "copilot"]

    def test_no_vendor_passes_none(self, _isolate_cli_deps):
        """不传 -v 时 vendor 应为 None（不过滤）."""
        mock_show = _isolate_cli_deps
        result = runner.invoke(app, ["usage"])
        assert result.exit_code == 0
        assert _kwargs(mock_show)["vendor"] is None
