"""用量配额守卫单元测试."""

import time
from unittest.mock import patch

from coding.proxy.routing.quota_guard import QuotaGuard


def _make_guard(**overrides):
    defaults = {
        "enabled": True,
        "token_budget": 1000,
        "window_seconds": 3600,
        "threshold_percent": 99.0,
        "probe_interval_seconds": 300,
    }
    defaults.update(overrides)
    return QuotaGuard(**defaults)


def test_initial_state_is_within_quota():
    qg = _make_guard()
    info = qg.get_info()
    assert info["state"] == "within_quota"
    assert info["window_usage_tokens"] == 0


def test_disabled_always_returns_true():
    qg = _make_guard(enabled=False)
    assert qg.can_use_primary() is True
    qg.record_usage(999999)
    assert qg.can_use_primary() is True


def test_within_budget_allows_primary():
    qg = _make_guard(token_budget=1000, threshold_percent=99.0)
    qg.record_usage(980)
    assert qg.can_use_primary() is True


def test_exceeding_threshold_triggers_exceeded():
    qg = _make_guard(token_budget=1000, threshold_percent=99.0)
    qg.record_usage(990)
    assert qg.can_use_primary() is False
    assert qg.get_info()["state"] == "quota_exceeded"


def test_window_expiry_restores_within_quota():
    qg = _make_guard(token_budget=1000, threshold_percent=99.0, window_seconds=10)
    base = time.monotonic()
    with patch("coding.proxy.routing.quota_guard.time") as mock_time:
        mock_time.monotonic.return_value = base
        qg.record_usage(995)
        assert qg.can_use_primary() is False

        # 窗口滑出后用量归零
        mock_time.monotonic.return_value = base + 11
        assert qg.can_use_primary() is True
        assert qg.get_info()["state"] == "within_quota"


def test_notify_cap_error_triggers_exceeded():
    qg = _make_guard()
    assert qg.can_use_primary() is True
    qg.notify_cap_error()
    info = qg.get_info()
    assert info["state"] == "quota_exceeded"


def test_probe_allowed_after_interval():
    qg = _make_guard(probe_interval_seconds=10)
    base = time.monotonic()
    with patch("coding.proxy.routing.quota_guard.time") as mock_time:
        mock_time.monotonic.return_value = base
        qg.notify_cap_error()
        assert qg.can_use_primary() is False

        # probe_interval 后允许探测
        mock_time.monotonic.return_value = base + 11
        assert qg.can_use_primary() is True
        # 第二次调用仍然 False（探测已消耗）
        assert qg.can_use_primary() is False


def test_probe_success_restores_within_quota():
    qg = _make_guard()
    qg.notify_cap_error()
    assert qg.get_info()["state"] == "quota_exceeded"
    qg.record_primary_success()
    assert qg.get_info()["state"] == "within_quota"


def test_reset_preserves_window_usage():
    """复位只清状态机，滑动窗口用量计数须原样保留."""
    qg = _make_guard()
    qg.record_usage(500)
    qg.notify_cap_error()
    qg.reset()
    info = qg.get_info()
    assert info["state"] == "within_quota"
    assert info["window_usage_tokens"] == 500


def test_reset_clears_cap_error_stall():
    """cap 错误卡死态：复位清除 _cap_error_active，预算未超时立即恢复放行."""
    qg = _make_guard(token_budget=1000)
    qg.record_usage(100)
    qg.notify_cap_error()
    assert qg.can_use_primary() is False
    qg.reset()
    assert qg.can_use_primary() is True
    assert qg.get_info()["window_usage_tokens"] == 100


def test_reset_does_not_unblock_genuinely_exhausted_quota():
    """用量确实超阈值时，复位不放行也不伪造用量——保持 EXCEEDED，不做状态回环."""
    qg = _make_guard(token_budget=1000, threshold_percent=99.0)
    qg.record_usage(995)
    assert qg.can_use_primary() is False
    qg.reset()
    assert qg.get_info()["state"] == "quota_exceeded"
    assert qg.can_use_primary() is False
    assert qg.get_info()["window_usage_tokens"] == 995


def test_reset_does_not_delay_probe_when_over_budget():
    """回归：用量仍超阈值时复位不得推后探测时钟.

    若 reset() 走 WITHIN_QUOTA → 下一次判定再回落 QUOTA_EXCEEDED 的回环，
    `_transition_to(EXCEEDED)` 会把 `_last_probe` 刷成当前时刻，令探测恢复
    凭空推迟一个 probe_interval；反复点击「状态复位」即可无限饿死探测。
    """
    qg = _make_guard(
        token_budget=1000, threshold_percent=99.0, probe_interval_seconds=300
    )
    base = time.monotonic()
    with patch("coding.proxy.routing.quota_guard.time") as mock_time:
        mock_time.monotonic.return_value = base
        qg.record_usage(995)
        assert qg.can_use_primary() is False  # → EXCEEDED，_last_probe = base

        # 距下次探测仅剩 10s 时复位
        mock_time.monotonic.return_value = base + 290
        qg.reset()

        # 原定探测点（base+300）之后应照常放行探测，而非被推到 base+590
        mock_time.monotonic.return_value = base + 301
        assert qg.can_use_primary() is True


def test_reset_still_clears_cap_stall_when_over_budget():
    """用量超阈值但由 cap 错误拉长了探测间隔时，复位须还原 probe_interval."""
    qg = _make_guard(
        token_budget=1000, threshold_percent=99.0, probe_interval_seconds=300
    )
    base = time.monotonic()
    with patch("coding.proxy.routing.quota_guard.time") as mock_time:
        mock_time.monotonic.return_value = base
        qg.record_usage(995)
        qg.notify_cap_error(retry_after_seconds=3600)  # 探测间隔被拉到 3960s
        assert qg.can_use_primary() is False

        qg.reset()
        mock_time.monotonic.return_value = base + 301
        # 若 _effective_probe_interval 未还原为 300，此处仍会被拒
        assert qg.can_use_primary() is True


def test_get_info_returns_correct_data():
    qg = _make_guard(token_budget=2000, threshold_percent=80.0)
    qg.record_usage(1000)
    info = qg.get_info()
    assert info["window_usage_tokens"] == 1000
    assert info["budget_tokens"] == 2000
    assert info["usage_percent"] == 50.0
    assert info["threshold_percent"] == 80.0


def test_load_baseline():
    qg = _make_guard(token_budget=1000, threshold_percent=99.0)
    qg.load_baseline(995)
    assert qg.can_use_primary() is False
    assert qg.get_info()["state"] == "quota_exceeded"


def test_zero_budget_error_only_mode():
    """token_budget=0 时不做主动追踪，仅响应 cap 错误."""
    qg = _make_guard(token_budget=0)
    qg.record_usage(999999)
    # 无预算时不触发阈值切换
    assert qg.can_use_primary() is True
    # 但 cap 错误仍然生效
    qg.notify_cap_error()
    assert qg.get_info()["state"] == "quota_exceeded"
    # 探测恢复
    qg.record_primary_success()
    assert qg.get_info()["state"] == "within_quota"


def test_record_usage_ignores_non_positive():
    qg = _make_guard()
    qg.record_usage(0)
    qg.record_usage(-10)
    assert qg.get_info()["window_usage_tokens"] == 0


def test_disabled_ignores_cap_error():
    qg = _make_guard(enabled=False)
    qg.notify_cap_error()
    assert qg.can_use_primary() is True
