"""VendorTier 单元测试."""

import time
from unittest.mock import AsyncMock, MagicMock

import pytest

from coding.proxy.routing.circuit_breaker import CircuitBreaker, CircuitState
from coding.proxy.routing.quota_guard import QuotaGuard
from coding.proxy.routing.tier import VendorTier


def _make_vendor(name: str = "test") -> MagicMock:
    vendor = MagicMock()
    vendor.get_name.return_value = name
    return vendor


# --- can_execute ---


def test_can_execute_no_cb_no_qg():
    """终端层（无 CB/QG）始终可执行."""
    tier = VendorTier(vendor=_make_vendor())
    assert tier.can_execute()


def test_can_execute_cb_closed():
    """CB CLOSED 时可执行."""
    cb = CircuitBreaker()
    tier = VendorTier(vendor=_make_vendor(), circuit_breaker=cb)
    assert cb.state == CircuitState.CLOSED
    assert tier.can_execute()


def test_can_execute_cb_open():
    """CB OPEN 时不可执行."""
    cb = CircuitBreaker(failure_threshold=1)
    cb.record_failure()
    tier = VendorTier(vendor=_make_vendor(), circuit_breaker=cb)
    assert cb.state == CircuitState.OPEN
    assert not tier.can_execute()


def test_can_execute_qg_exceeded():
    """QG QUOTA_EXCEEDED 时不可执行."""
    qg = QuotaGuard(
        enabled=True,
        token_budget=100,
        window_seconds=3600,
        probe_interval_seconds=99999,
    )
    qg.notify_cap_error()
    tier = VendorTier(vendor=_make_vendor(), quota_guard=qg)
    assert not tier.can_execute()


def test_can_execute_cb_ok_qg_exceeded():
    """CB 正常但 QG 超限 → 不可执行."""
    cb = CircuitBreaker()
    qg = QuotaGuard(
        enabled=True,
        token_budget=100,
        window_seconds=3600,
        probe_interval_seconds=99999,
    )
    qg.notify_cap_error()
    tier = VendorTier(vendor=_make_vendor(), circuit_breaker=cb, quota_guard=qg)
    assert not tier.can_execute()


def test_can_execute_cb_open_qg_ok():
    """CB OPEN 但 QG 正常 → 不可执行（CB 优先判断）."""
    cb = CircuitBreaker(failure_threshold=1)
    cb.record_failure()
    qg = QuotaGuard(enabled=True, token_budget=100000, window_seconds=3600)
    tier = VendorTier(vendor=_make_vendor(), circuit_breaker=cb, quota_guard=qg)
    assert not tier.can_execute()


# --- name / is_terminal ---


def test_name_delegates_to_vendor():
    tier = VendorTier(vendor=_make_vendor("anthropic"))
    assert tier.name == "anthropic"


def test_is_terminal_without_cb():
    """无 CB 视为终端层."""
    tier = VendorTier(vendor=_make_vendor())
    assert tier.is_terminal


def test_is_not_terminal_with_cb():
    """有 CB 非终端层."""
    tier = VendorTier(vendor=_make_vendor(), circuit_breaker=CircuitBreaker())
    assert not tier.is_terminal


# --- record_success ---


def test_record_success_updates_cb():
    cb = CircuitBreaker(failure_threshold=2)
    cb.record_failure()
    tier = VendorTier(vendor=_make_vendor(), circuit_breaker=cb)
    tier.record_success(100)
    # CB failure_count 应被重置
    assert cb.state == CircuitState.CLOSED


def test_record_success_updates_qg():
    """record_success 传播 usage_tokens 到 QG."""
    qg = QuotaGuard(enabled=True, token_budget=1000, window_seconds=3600)
    tier = VendorTier(vendor=_make_vendor(), quota_guard=qg)
    tier.record_success(500)
    info = qg.get_info()
    assert info["window_usage_tokens"] == 500


def test_record_success_zero_tokens_no_qg_update():
    """usage_tokens=0 时不更新 QG 窗口."""
    qg = QuotaGuard(enabled=True, token_budget=1000, window_seconds=3600)
    tier = VendorTier(vendor=_make_vendor(), quota_guard=qg)
    tier.record_success(0)
    info = qg.get_info()
    assert info["window_usage_tokens"] == 0


# --- record_failure ---


def test_record_failure_updates_cb():
    cb = CircuitBreaker(failure_threshold=2)
    tier = VendorTier(vendor=_make_vendor(), circuit_breaker=cb)
    tier.record_failure()
    tier.record_failure()
    assert cb.state == CircuitState.OPEN


def test_record_failure_cap_error_notifies_qg():
    """is_cap_error=True 时通知 QG."""
    qg = QuotaGuard(
        enabled=True,
        token_budget=1000,
        window_seconds=3600,
        probe_interval_seconds=99999,
    )
    tier = VendorTier(vendor=_make_vendor(), quota_guard=qg)
    tier.record_failure(is_cap_error=True)
    assert not qg.can_use_primary()


def test_record_failure_non_cap_no_qg_notify():
    """非 cap error 不通知 QG."""
    qg = QuotaGuard(enabled=True, token_budget=1000, window_seconds=3600)
    tier = VendorTier(vendor=_make_vendor(), quota_guard=qg)
    tier.record_failure(is_cap_error=False)
    assert qg.can_use_primary()


# --- rate limit deadline ---


def test_rate_limit_deadline_initial_zero():
    """初始状态无 rate limit."""
    tier = VendorTier(vendor=_make_vendor())
    assert not tier.is_rate_limited
    assert tier.rate_limit_remaining_seconds == 0.0


def test_record_failure_sets_deadline():
    """record_failure 设置 rate limit deadline."""
    cb = CircuitBreaker(failure_threshold=1)
    tier = VendorTier(vendor=_make_vendor(), circuit_breaker=cb)
    future = time.monotonic() + 60
    tier.record_failure(rate_limit_deadline=future)
    assert tier.is_rate_limited
    assert tier.rate_limit_remaining_seconds > 59


def test_record_failure_keeps_later_deadline():
    """多次 failure 保留更远的 deadline."""
    tier = VendorTier(vendor=_make_vendor(), circuit_breaker=CircuitBreaker())
    now = time.monotonic()
    tier.record_failure(rate_limit_deadline=now + 60)
    tier.record_failure(rate_limit_deadline=now + 30)  # 更早的 deadline
    assert tier.rate_limit_remaining_seconds > 59  # 保留更远的


def test_record_failure_without_deadline_no_change():
    """不提供 deadline 时不更新."""
    tier = VendorTier(vendor=_make_vendor(), circuit_breaker=CircuitBreaker())
    tier.record_failure()
    assert not tier.is_rate_limited


def test_record_success_clears_deadline():
    """成功请求清除 rate limit deadline."""
    tier = VendorTier(vendor=_make_vendor(), circuit_breaker=CircuitBreaker())
    tier.record_failure(rate_limit_deadline=time.monotonic() + 60)
    assert tier.is_rate_limited
    tier.record_success(100)
    assert not tier.is_rate_limited


def test_reset_rate_limit_clears_deadline():
    """手动重置清除 deadline."""
    tier = VendorTier(vendor=_make_vendor())
    tier._rate_limit_deadline = time.monotonic() + 999
    tier.reset_rate_limit()
    assert not tier.is_rate_limited


def test_get_rate_limit_info_no_limit():
    """无 rate limit 时返回正确结构."""
    tier = VendorTier(vendor=_make_vendor())
    info = tier.get_rate_limit_info()
    assert info["is_rate_limited"] is False
    assert info["remaining_seconds"] == 0.0


def test_get_rate_limit_info_active():
    """活跃 rate limit 时返回正确结构."""
    tier = VendorTier(vendor=_make_vendor())
    tier._rate_limit_deadline = time.monotonic() + 120
    info = tier.get_rate_limit_info()
    assert info["is_rate_limited"] is True
    assert info["remaining_seconds"] > 119


# --- can_execute_with_health_check + deadline ---


@pytest.mark.asyncio
async def test_health_check_blocked_by_deadline():
    """rate limit deadline 未到期 → 直接拒绝，不调用 check_health."""
    vendor = _make_vendor()
    vendor.check_health = AsyncMock(return_value=True)
    cb = CircuitBreaker(failure_threshold=1)
    tier = VendorTier(vendor=vendor, circuit_breaker=cb)

    tier._rate_limit_deadline = time.monotonic() + 300

    result = await tier.can_execute_with_health_check()
    assert result is False
    vendor.check_health.assert_not_called()


@pytest.mark.asyncio
async def test_health_check_allowed_after_deadline():
    """rate limit deadline 已过期 → 正常走健康检查流程."""
    vendor = _make_vendor()
    vendor.check_health = AsyncMock(return_value=True)
    cb = CircuitBreaker(failure_threshold=1, recovery_timeout_seconds=0)
    cb.record_failure()  # → OPEN → 立即 HALF_OPEN (recovery=0)
    tier = VendorTier(vendor=vendor, circuit_breaker=cb)

    tier._rate_limit_deadline = time.monotonic() - 1  # 已过期

    result = await tier.can_execute_with_health_check()
    assert result is True
    vendor.check_health.assert_called_once()


@pytest.mark.asyncio
async def test_health_check_failure_blocks_probe():
    """健康检查失败 → 阻止探测."""
    vendor = _make_vendor()
    vendor.check_health = AsyncMock(return_value=False)
    cb = CircuitBreaker(failure_threshold=1, recovery_timeout_seconds=0)
    cb.record_failure()
    tier = VendorTier(vendor=vendor, circuit_breaker=cb)

    result = await tier.can_execute_with_health_check()
    assert result is False


# --- weekly_quota_guard ---


def test_can_execute_weekly_qg_exceeded():
    """weekly guard 超限时 can_execute() 返回 False."""
    wqg = QuotaGuard(
        enabled=True,
        token_budget=100,
        window_seconds=604800,
        probe_interval_seconds=99999,
    )
    wqg.notify_cap_error()
    tier = VendorTier(vendor=_make_vendor(), weekly_quota_guard=wqg)
    assert not tier.can_execute()


def test_can_execute_qg_ok_weekly_qg_exceeded():
    """5h guard 正常但 weekly guard 超限 → 不可执行."""
    qg = QuotaGuard(enabled=True, token_budget=100000, window_seconds=18000)
    wqg = QuotaGuard(
        enabled=True,
        token_budget=100,
        window_seconds=604800,
        probe_interval_seconds=99999,
    )
    wqg.notify_cap_error()
    tier = VendorTier(vendor=_make_vendor(), quota_guard=qg, weekly_quota_guard=wqg)
    assert not tier.can_execute()


def test_both_guards_must_pass():
    """5h guard EXCEEDED + weekly guard EXCEEDED → 不可执行."""
    qg = QuotaGuard(
        enabled=True,
        token_budget=100,
        window_seconds=18000,
        probe_interval_seconds=99999,
    )
    qg.notify_cap_error()
    wqg = QuotaGuard(
        enabled=True,
        token_budget=100,
        window_seconds=604800,
        probe_interval_seconds=99999,
    )
    wqg.notify_cap_error()
    tier = VendorTier(vendor=_make_vendor(), quota_guard=qg, weekly_quota_guard=wqg)
    assert not tier.can_execute()


def test_record_success_updates_weekly_qg():
    """record_success() 传播 usage_tokens 到 weekly guard."""
    wqg = QuotaGuard(enabled=True, token_budget=1000, window_seconds=604800)
    tier = VendorTier(vendor=_make_vendor(), weekly_quota_guard=wqg)
    tier.record_success(500)
    info = wqg.get_info()
    assert info["window_usage_tokens"] == 500


def test_record_failure_cap_error_notifies_weekly_qg():
    """is_cap_error=True 时同时通知 weekly guard."""
    wqg = QuotaGuard(
        enabled=True,
        token_budget=1000,
        window_seconds=604800,
        probe_interval_seconds=99999,
    )
    tier = VendorTier(vendor=_make_vendor(), weekly_quota_guard=wqg)
    tier.record_failure(is_cap_error=True)
    assert not wqg.can_use_primary()


def test_record_failure_non_cap_no_weekly_qg_notify():
    """非 cap error 不通知 weekly guard."""
    wqg = QuotaGuard(enabled=True, token_budget=1000, window_seconds=604800)
    tier = VendorTier(vendor=_make_vendor(), weekly_quota_guard=wqg)
    tier.record_failure(is_cap_error=False)
    assert wqg.can_use_primary()
