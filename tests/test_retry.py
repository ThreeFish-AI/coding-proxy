"""routing.retry 模块单元测试 — calculate_delay（指数退避 + Equal Jitter）."""

import random

from coding.proxy.routing.retry import RetryConfig, calculate_delay

# Equal Jitter 蒙特卡洛采样次数（纯计算，开销可忽略）
_SAMPLES = 500


def _cfg(**overrides) -> RetryConfig:
    """构造测试用 RetryConfig（Zhipu 实际配置为默认基线）."""
    defaults = dict(
        max_retries=4,
        initial_delay_ms=1000,
        max_delay_ms=30000,
        backoff_multiplier=2.0,
        jitter=True,
    )
    defaults.update(overrides)
    return RetryConfig(**defaults)


# --- 无抖动：精确指数（回归基线）---


def test_calculate_delay_no_jitter_exact_exponential():
    """jitter=False 时延迟为纯指数 1000 → 2000 → 4000 → 8000 ms."""
    cfg = _cfg(jitter=False)
    assert calculate_delay(0, cfg) == 1000.0
    assert calculate_delay(1, cfg) == 2000.0
    assert calculate_delay(2, cfg) == 4000.0
    assert calculate_delay(3, cfg) == 8000.0


# --- Equal Jitter：区间边界 ---


def test_calculate_delay_equal_jitter_bounds():
    """Equal Jitter 落在 [temp/2, temp]：[500,1000]/[1000,2000]/[2000,4000]/[4000,8000]."""
    cfg = _cfg(jitter=True)
    bounds = [(500.0, 1000.0), (1000.0, 2000.0), (2000.0, 4000.0), (4000.0, 8000.0)]
    for attempt, (low, high) in enumerate(bounds):
        for _ in range(_SAMPLES):
            delay = calculate_delay(attempt, cfg)
            assert low <= delay <= high, (
                f"attempt={attempt} delay={delay} 越界 [{low}, {high}]"
            )


def test_calculate_delay_capped_at_max():
    """触及 max_delay_ms 封顶后，区间退化为 [max/2, max] = [15000, 30000]."""
    cfg = _cfg(jitter=True)
    # attempt=5: 1000 * 2^5 = 32000 > max=30000 → temp=30000
    for _ in range(_SAMPLES):
        delay = calculate_delay(5, cfg)
        assert 15000.0 <= delay <= 30000.0


# --- 单调非递减（multiplier=2.0 下数学保证）---


def test_calculate_delay_monotonic_non_decreasing():
    """相邻重试延迟单调非递减（multiplier>=2.0 且未封顶时成立）.

    数学保证：delay[i] ∈ [temp_i/2, temp_i]，delay[i+1] ∈ [temp_i, 2·temp_i]，
    故 delay[i] <= temp_i <= delay[i+1] 恒成立（用 <= 而非 <，体现边界相切）。
    """
    cfg = _cfg(jitter=True)
    for _ in range(_SAMPLES):
        delays = [calculate_delay(a, cfg) for a in range(4)]
        for i in range(len(delays) - 1):
            assert delays[i] <= delays[i + 1], (
                f"非单调：delays={delays}（index {i} > {i + 1}）"
            )


# --- 健壮性：极小 initial 不报错 ---


def test_calculate_delay_tiny_initial_no_error():
    """initial_delay_ms 极小值时不抛异常，仍落在合法区间."""
    cfg = _cfg(initial_delay_ms=1, jitter=True)
    delay = calculate_delay(0, cfg)  # temp=1 → [0.5, 1.0]
    assert 0.5 <= delay <= 1.0


# --- 可复现性：固定 random seed ---


def test_calculate_delay_reproducible_with_seed():
    """固定 random seed 后延迟序列可复现（验证 jitter 可观测、可调试）."""
    cfg = _cfg(jitter=True)
    random.seed(42)
    first = [calculate_delay(a, cfg) for a in range(4)]
    random.seed(42)
    second = [calculate_delay(a, cfg) for a in range(4)]
    assert first == second
