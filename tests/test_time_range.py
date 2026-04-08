"""resolve_time_range / _build_title 纯函数单元测试."""

from datetime import date, datetime
from unittest.mock import patch
from zoneinfo import ZoneInfo

from coding.proxy.logging.stats import (
    _TOTAL_SENTINEL_DAYS,
    _build_title,
    resolve_time_range,
)

_SHANGHAI = ZoneInfo("Asia/Shanghai")


# ── resolve_time_range ────────────────────────────────────────


class TestResolveTimeRange:
    """resolve_time_range 将快捷标志转换为等价天数."""

    def test_default_returns_seven(self):
        assert resolve_time_range() == 7

    def test_custom_days(self):
        assert resolve_time_range(days=30) == 30

    def test_days_clamps_to_one(self):
        assert resolve_time_range(days=0) == 1
        assert resolve_time_range(days=-5) == 1

    def test_total_returns_sentinel(self):
        assert resolve_time_range(total=True) == _TOTAL_SENTINEL_DAYS

    def test_total_overrides_week_and_month(self):
        """total 优先级最高."""
        assert (
            resolve_time_range(week=True, month=True, total=True)
            == _TOTAL_SENTINEL_DAYS
        )

    def test_month_on_first_day(self):
        """月初（1 日）应返回 1."""
        fake_now = datetime(2026, 4, 1, 10, 0, tzinfo=_SHANGHAI)
        with patch("coding.proxy.logging.stats._local_tz", return_value=_SHANGHAI):
            with patch(
                "coding.proxy.logging.stats.datetime",
                wraps=datetime,
            ) as mock_dt:
                mock_dt.now.return_value = fake_now
                result = resolve_time_range(month=True)
        assert result == 1

    def test_month_mid_month(self):
        """月中应返回 (today - 1st) + 1."""
        fake_now = datetime(2026, 4, 15, 10, 0, tzinfo=_SHANGHAI)
        with patch("coding.proxy.logging.stats._local_tz", return_value=_SHANGHAI):
            with patch(
                "coding.proxy.logging.stats.datetime",
                wraps=datetime,
            ) as mock_dt:
                mock_dt.now.return_value = fake_now
                result = resolve_time_range(month=True)
        assert result == 15

    def test_week_monday(self):
        """周一应返回 1."""
        # 2026-04-06 是周一
        fake_now = datetime(2026, 4, 6, 10, 0, tzinfo=_SHANGHAI)
        with patch("coding.proxy.logging.stats._local_tz", return_value=_SHANGHAI):
            with patch(
                "coding.proxy.logging.stats.datetime",
                wraps=datetime,
            ) as mock_dt:
                mock_dt.now.return_value = fake_now
                result = resolve_time_range(week=True)
        assert result == 1

    def test_week_sunday(self):
        """周日应返回 7."""
        # 2026-04-12 是周日
        fake_now = datetime(2026, 4, 12, 10, 0, tzinfo=_SHANGHAI)
        with patch("coding.proxy.logging.stats._local_tz", return_value=_SHANGHAI):
            with patch(
                "coding.proxy.logging.stats.datetime",
                wraps=datetime,
            ) as mock_dt:
                mock_dt.now.return_value = fake_now
                result = resolve_time_range(week=True)
        assert result == 7

    def test_month_overrides_week(self):
        """month 优先级高于 week."""
        fake_now = datetime(2026, 4, 8, 10, 0, tzinfo=_SHANGHAI)
        with patch("coding.proxy.logging.stats._local_tz", return_value=_SHANGHAI):
            with patch(
                "coding.proxy.logging.stats.datetime",
                wraps=datetime,
            ) as mock_dt:
                mock_dt.now.return_value = fake_now
                result = resolve_time_range(week=True, month=True)
        # month: 4 月 8 日 → 8 天
        assert result == 8


# ── _build_title ──────────────────────────────────────────────


class TestBuildTitle:
    """_build_title 根据天数生成语义化标题."""

    def test_normal_days(self):
        assert _build_title(7) == "Token 使用统计（最近 7 天）"

    def test_one_day(self):
        assert _build_title(1) == "Token 使用统计（最近 1 天）"

    def test_total_sentinel(self):
        assert _build_title(_TOTAL_SENTINEL_DAYS) == "Token 使用统计（全部）"
