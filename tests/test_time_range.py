"""stats 模块纯函数单元测试 — _build_title / _period_to_days."""

import pytest

from coding.proxy.logging.db import TimePeriod
from coding.proxy.logging.stats import _build_title, _period_to_days

# ── _build_title ──────────────────────────────────────────────


class TestBuildTitle:
    """_build_title 根据时间维度生成语义化标题."""

    @pytest.mark.parametrize(
        ("period", "count", "expected"),
        [
            (TimePeriod.DAY, 7, "Token 使用统计（最近 7 日）"),
            (TimePeriod.DAY, 1, "Token 使用统计（最近 1 日）"),
            (TimePeriod.WEEK, 4, "Token 使用统计（最近 4 周）"),
            (TimePeriod.MONTH, 3, "Token 使用统计（最近 3 月）"),
            (TimePeriod.TOTAL, 1, "Token 使用统计（全部）"),
        ],
    )
    def test_title_format(self, period, count, expected):
        assert _build_title(period, count) == expected


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
