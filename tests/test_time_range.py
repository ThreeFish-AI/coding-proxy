"""stats 模块纯函数单元测试 — _build_title / _period_to_days / _week_date_range."""

import re
from datetime import datetime, timedelta

import pytest

from coding.proxy.logging.db import TimePeriod
from coding.proxy.logging.stats import (
    _build_title,
    _period_to_days,
    _week_date_range,
)

# ── _week_date_range ─────────────────────────────────────────


class TestWeekDateRange:
    """_week_date_range 计算正确的周一～周日范围."""

    def test_this_week_contains_today(self):
        """count=1 应包含今天."""
        today = datetime.now().date()
        range_str = _week_date_range(1)
        # 解析起止日期
        parts = range_str.split(" ～ ")
        start = datetime.strptime(parts[0], "%Y-%m-%d").date()
        end = datetime.strptime(parts[1], "%Y-%m-%d").date()
        assert start <= today <= end

    def test_this_week_starts_on_monday(self):
        """count=1 的起始日期应为周一."""
        range_str = _week_date_range(1)
        start_str = range_str.split(" ～ ")[0]
        start = datetime.strptime(start_str, "%Y-%m-%d").date()
        assert start.weekday() == 0  # Monday

    def test_this_week_ends_on_sunday(self):
        """count=1 的结束日期应为周日."""
        range_str = _week_date_range(1)
        end_str = range_str.split(" ～ ")[1]
        end = datetime.strptime(end_str, "%Y-%m-%d").date()
        assert end.weekday() == 6  # Sunday

    def test_last_week(self):
        """count=2 应为上周."""
        today = datetime.now().date()
        this_monday = today - timedelta(days=today.weekday())
        last_monday = this_monday - timedelta(weeks=1)
        last_sunday = last_monday + timedelta(days=6)

        range_str = _week_date_range(2)
        parts = range_str.split(" ～ ")
        start = datetime.strptime(parts[0], "%Y-%m-%d").date()
        end = datetime.strptime(parts[1], "%Y-%m-%d").date()
        assert start == last_monday
        assert end == last_sunday

    def test_span_is_seven_days(self):
        """周范围应始终为 7 天."""
        for count in (1, 2, 5):
            range_str = _week_date_range(count)
            parts = range_str.split(" ～ ")
            start = datetime.strptime(parts[0], "%Y-%m-%d").date()
            end = datetime.strptime(parts[1], "%Y-%m-%d").date()
            assert (end - start).days == 6


# ── _build_title ──────────────────────────────────────────────


class TestBuildTitle:
    """_build_title 根据时间维度生成语义化标题."""

    def test_day_title(self):
        assert _build_title(TimePeriod.DAY, 7) == "Token 使用统计（最近 7 日）"

    def test_day_title_single(self):
        assert _build_title(TimePeriod.DAY, 1) == "Token 使用统计（最近 1 日）"

    def test_month_title(self):
        assert _build_title(TimePeriod.MONTH, 3) == "Token 使用统计（最近 3 月）"

    def test_total_title(self):
        assert _build_title(TimePeriod.TOTAL, 1) == "Token 使用统计（全部）"

    def test_week_title_contains_date_range(self):
        """WEEK 维度标题应包含具体日期范围."""
        title = _build_title(TimePeriod.WEEK, 1)
        # 格式: Token 使用统计（最近 1 周：YYYY-MM-DD ～ YYYY-MM-DD）
        assert title.startswith("Token 使用统计（最近 1 周：")
        assert title.endswith("）")
        assert re.search(r"\d{4}-\d{2}-\d{2} ～ \d{4}-\d{2}-\d{2}", title)

    def test_week_title_multi_count(self):
        """WEEK 维度 count>1 也应包含日期范围."""
        title = _build_title(TimePeriod.WEEK, 4)
        assert title.startswith("Token 使用统计（最近 4 周：")
        assert re.search(r"\d{4}-\d{2}-\d{2} ～ \d{4}-\d{2}-\d{2}", title)


# ── _period_to_days ───────────────────────────────────────────


class TestPeriodToDays:
    """_period_to_days 将 TimePeriod 近似转换为天数."""

    def test_day(self):
        assert _period_to_days(TimePeriod.DAY, 7) == 7

    def test_day_clamps_to_one(self):
        assert _period_to_days(TimePeriod.DAY, 0) == 1

    def test_week(self):
        assert _period_to_days(TimePeriod.WEEK, 2) == 14

    def test_month(self):
        # 粗略近似：31 * count
        assert _period_to_days(TimePeriod.MONTH, 3) == 93

    def test_total_returns_none(self):
        assert _period_to_days(TimePeriod.TOTAL, 1) is None
