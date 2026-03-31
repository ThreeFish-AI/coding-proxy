"""BackendTier 单元测试."""

from unittest.mock import MagicMock

from coding.proxy.routing.circuit_breaker import CircuitBreaker, CircuitState
from coding.proxy.routing.quota_guard import QuotaGuard, QuotaState
from coding.proxy.routing.tier import BackendTier


def _make_backend(name: str = "test") -> MagicMock:
    backend = MagicMock()
    backend.get_name.return_value = name
    return backend


# --- can_execute ---


def test_can_execute_no_cb_no_qg():
    """终端层（无 CB/QG）始终可执行."""
    tier = BackendTier(backend=_make_backend())
    assert tier.can_execute()


def test_can_execute_cb_closed():
    """CB CLOSED 时可执行."""
    cb = CircuitBreaker()
    tier = BackendTier(backend=_make_backend(), circuit_breaker=cb)
    assert cb.state == CircuitState.CLOSED
    assert tier.can_execute()


def test_can_execute_cb_open():
    """CB OPEN 时不可执行."""
    cb = CircuitBreaker(failure_threshold=1)
    cb.record_failure()
    tier = BackendTier(backend=_make_backend(), circuit_breaker=cb)
    assert cb.state == CircuitState.OPEN
    assert not tier.can_execute()


def test_can_execute_qg_exceeded():
    """QG QUOTA_EXCEEDED 时不可执行."""
    qg = QuotaGuard(enabled=True, token_budget=100, window_seconds=3600, probe_interval_seconds=99999)
    qg.notify_cap_error()
    tier = BackendTier(backend=_make_backend(), quota_guard=qg)
    assert not tier.can_execute()


def test_can_execute_cb_ok_qg_exceeded():
    """CB 正常但 QG 超限 → 不可执行."""
    cb = CircuitBreaker()
    qg = QuotaGuard(enabled=True, token_budget=100, window_seconds=3600, probe_interval_seconds=99999)
    qg.notify_cap_error()
    tier = BackendTier(backend=_make_backend(), circuit_breaker=cb, quota_guard=qg)
    assert not tier.can_execute()


def test_can_execute_cb_open_qg_ok():
    """CB OPEN 但 QG 正常 → 不可执行（CB 优先判断）."""
    cb = CircuitBreaker(failure_threshold=1)
    cb.record_failure()
    qg = QuotaGuard(enabled=True, token_budget=100000, window_seconds=3600)
    tier = BackendTier(backend=_make_backend(), circuit_breaker=cb, quota_guard=qg)
    assert not tier.can_execute()


# --- name / is_terminal ---


def test_name_delegates_to_backend():
    tier = BackendTier(backend=_make_backend("anthropic"))
    assert tier.name == "anthropic"


def test_is_terminal_without_cb():
    """无 CB 视为终端层."""
    tier = BackendTier(backend=_make_backend())
    assert tier.is_terminal


def test_is_not_terminal_with_cb():
    """有 CB 非终端层."""
    tier = BackendTier(backend=_make_backend(), circuit_breaker=CircuitBreaker())
    assert not tier.is_terminal


# --- record_success ---


def test_record_success_updates_cb():
    cb = CircuitBreaker(failure_threshold=2)
    cb.record_failure()
    tier = BackendTier(backend=_make_backend(), circuit_breaker=cb)
    tier.record_success(100)
    # CB failure_count 应被重置
    assert cb.state == CircuitState.CLOSED


def test_record_success_updates_qg():
    """record_success 传播 usage_tokens 到 QG."""
    qg = QuotaGuard(enabled=True, token_budget=1000, window_seconds=3600)
    tier = BackendTier(backend=_make_backend(), quota_guard=qg)
    tier.record_success(500)
    info = qg.get_info()
    assert info["window_usage_tokens"] == 500


def test_record_success_zero_tokens_no_qg_update():
    """usage_tokens=0 时不更新 QG 窗口."""
    qg = QuotaGuard(enabled=True, token_budget=1000, window_seconds=3600)
    tier = BackendTier(backend=_make_backend(), quota_guard=qg)
    tier.record_success(0)
    info = qg.get_info()
    assert info["window_usage_tokens"] == 0


# --- record_failure ---


def test_record_failure_updates_cb():
    cb = CircuitBreaker(failure_threshold=2)
    tier = BackendTier(backend=_make_backend(), circuit_breaker=cb)
    tier.record_failure()
    tier.record_failure()
    assert cb.state == CircuitState.OPEN


def test_record_failure_cap_error_notifies_qg():
    """is_cap_error=True 时通知 QG."""
    qg = QuotaGuard(enabled=True, token_budget=1000, window_seconds=3600, probe_interval_seconds=99999)
    tier = BackendTier(backend=_make_backend(), quota_guard=qg)
    tier.record_failure(is_cap_error=True)
    assert not qg.can_use_primary()


def test_record_failure_non_cap_no_qg_notify():
    """非 cap error 不通知 QG."""
    qg = QuotaGuard(enabled=True, token_budget=1000, window_seconds=3600)
    tier = BackendTier(backend=_make_backend(), quota_guard=qg)
    tier.record_failure(is_cap_error=False)
    assert qg.can_use_primary()
