"""熔断器状态转换单元测试."""

from coding.proxy.routing.circuit_breaker import CircuitBreaker, CircuitState


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


# ── force_open: 429 / rate limit 立即开路 ──────────────────────


def test_force_open_immediately_opens_from_closed():
    """force_open=True 时，单次失败即可从 CLOSED → OPEN（无需累积至阈值）."""
    cb = CircuitBreaker(failure_threshold=3, recovery_timeout_seconds=30)
    assert cb.state == CircuitState.CLOSED

    cb.record_failure(force_open=True)
    assert cb.state == CircuitState.OPEN
    assert not cb.can_execute()


def test_force_open_uses_retry_after_as_recovery():
    """force_open + retry_after_seconds → 使用 server-hinted 恢复时间作为 recovery."""
    cb = CircuitBreaker(failure_threshold=3, recovery_timeout_seconds=300)

    cb.record_failure(retry_after_seconds=60.0, force_open=True)
    assert cb.state == CircuitState.OPEN

    info = cb.get_info()
    assert info["current_recovery_seconds"] == 60.0


def test_force_open_respects_max_recovery():
    """force_open 的 retry_after 超过 max_recovery 时应被截断."""
    cb = CircuitBreaker(
        failure_threshold=3,
        recovery_timeout_seconds=300,
        max_recovery_seconds=3600,
    )

    cb.record_failure(retry_after_seconds=7200.0, force_open=True)  # 2h > 1h max
    info = cb.get_info()
    assert info["current_recovery_seconds"] == 3600.0


def test_force_open_without_retry_uses_default_backoff():
    """force_open 但无 retry_after → 使用默认 recovery_timeout 作为退避."""
    cb = CircuitBreaker(failure_threshold=3, recovery_timeout_seconds=300)

    cb.record_failure(force_open=True)  # 无 retry_after
    info = cb.get_info()
    assert info["current_recovery_seconds"] == 300


def test_force_open_in_half_open_stays_same_behavior():
    """HALF_OPEN 状态下 force_open 不改变既有行为（本来就立即 OPEN）.

    验证 HALF_OPEN 分支的 force_open 走的是同一条立即 OPEN 路径，
    与非 force_open 行为一致.
    """
    cb = CircuitBreaker(failure_threshold=1, recovery_timeout_seconds=30)
    cb.record_failure()  # CLOSED → OPEN
    assert cb.state == CircuitState.OPEN

    # 手动推进到 HALF_OPEN（将 last_failure_time 往前推超过 recovery）
    import time as _time

    cb._last_failure_time = _time.monotonic() - cb._recovery_timeout - 1
    assert cb.state == CircuitState.HALF_OPEN

    # HALF_OPEN 下任意失败（含 force_open）都应立即回到 OPEN
    cb.record_failure(force_open=True)
    # 断言：_check_recovery 未触发（刚进入 OPEN），state 应为 OPEN
    assert cb.state == CircuitState.OPEN


def test_no_force_open_preserves_original_threshold():
    """force_open=False（默认）保持原有的 failure_threshold 累积行为."""
    cb = CircuitBreaker(failure_threshold=3, recovery_timeout_seconds=30)

    cb.record_failure()  # count=1, still CLOSED
    assert cb.state == CircuitState.CLOSED

    cb.record_failure()  # count=2, still CLOSED
    assert cb.state == CircuitState.CLOSED

    cb.record_failure()  # count=3, → OPEN
    assert cb.state == CircuitState.OPEN


def test_force_open_with_small_hint_overrides_current():
    """force_open 时即使 hint < current_recovery 也使用 hint（429 权威信号）.

    模拟场景：CB 已因累积失败进入 OPEN（current_recovery 较大），
    随后 reset 回 CLOSED，再收到带较短 retry-after 的 429.
    """
    cb = CircuitBreaker(failure_threshold=3, recovery_timeout_seconds=300)
    # 先通过正常累积进入 OPEN，建立较大的 current_recovery
    cb.record_failure()
    cb.record_failure()
    cb.record_failure()  # → OPEN, current_recovery=300
    assert cb.state == CircuitState.OPEN

    # reset 回 CLOSED（模拟恢复后重试）
    cb.reset()
    assert cb.state == CircuitState.CLOSED
    assert cb.get_info()["current_recovery_seconds"] == 300

    # 再一次 force_open，hint 较小 — 应使用权威 hint
    cb.record_failure(retry_after_seconds=10.0, force_open=True)
    info = cb.get_info()
    assert info["current_recovery_seconds"] == 10.0
