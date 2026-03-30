"""熔断器状态转换单元测试."""

from coding_proxy.routing.circuit_breaker import CircuitBreaker, CircuitState


def test_initial_state_is_closed():
    cb = CircuitBreaker(failure_threshold=3, recovery_timeout_seconds=30)
    assert cb.state == CircuitState.CLOSED


def test_closed_to_open():
    cb = CircuitBreaker(failure_threshold=3, recovery_timeout_seconds=30)
    for _ in range(3):
        cb.record_failure()
    assert cb.state == CircuitState.OPEN


def test_open_rejects_requests():
    cb = CircuitBreaker(failure_threshold=3, recovery_timeout_seconds=300)
    for _ in range(3):
        cb.record_failure()
    assert not cb.can_execute()


def test_reset_to_closed():
    cb = CircuitBreaker(failure_threshold=3, recovery_timeout_seconds=30)
    for _ in range(3):
        cb.record_failure()
    assert cb.state == CircuitState.OPEN
    cb.reset()
    assert cb.state == CircuitState.CLOSED


def test_success_resets_failure_count():
    cb = CircuitBreaker(failure_threshold=3, recovery_timeout_seconds=30)
    cb.record_failure()
    cb.record_failure()
    cb.record_success()
    # 成功重置失败计数，再失败两次不会触发 OPEN
    cb.record_failure()
    cb.record_failure()
    assert cb.state == CircuitState.CLOSED


def test_get_info():
    cb = CircuitBreaker(failure_threshold=3, recovery_timeout_seconds=30)
    info = cb.get_info()
    assert info["state"] == "closed"
    assert info["failure_count"] == 0
