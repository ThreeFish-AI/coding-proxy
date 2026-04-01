"""rate_limit 模块单元测试."""

import time

from coding.proxy.routing.rate_limit import (
    RateLimitInfo,
    compute_effective_retry_seconds,
    compute_rate_limit_deadline,
    parse_rate_limit_headers,
)


# --- compute_rate_limit_deadline ---


def test_compute_deadline_from_retry_after():
    """retry_after_seconds → monotonic deadline."""
    info = RateLimitInfo(retry_after_seconds=60.0)
    now = time.monotonic()
    deadline = compute_rate_limit_deadline(info)
    assert deadline is not None
    # 60 * 1.1 = 66 秒后
    assert 65 < deadline - now < 68


def test_compute_deadline_from_reset_timestamps():
    """requests_reset_at/tokens_reset_at → deadline（取最大值）."""
    now = time.monotonic()
    info = RateLimitInfo(
        requests_reset_at=now + 120,
        tokens_reset_at=now + 300,
    )
    deadline = compute_rate_limit_deadline(info)
    assert deadline is not None
    # 取 tokens_reset（300s × 1.1 = 330s）
    assert 329 < deadline - now < 332


def test_compute_deadline_none_when_no_info():
    """无 rate limit 信息 → None."""
    info = RateLimitInfo()
    assert compute_rate_limit_deadline(info) is None


def test_compute_deadline_ignores_past_timestamps():
    """已过期的 reset timestamp 被忽略."""
    now = time.monotonic()
    info = RateLimitInfo(
        requests_reset_at=now - 10,  # 已过期
        retry_after_seconds=30.0,
    )
    deadline = compute_rate_limit_deadline(info)
    assert deadline is not None
    # 只用 retry_after: 30 * 1.1 = 33s
    assert 32 < deadline - now < 35


def test_compute_deadline_all_past_returns_none():
    """所有时间戳都已过期 → None."""
    now = time.monotonic()
    info = RateLimitInfo(
        requests_reset_at=now - 100,
        tokens_reset_at=now - 50,
    )
    assert compute_rate_limit_deadline(info) is None


def test_compute_deadline_takes_max_of_all_signals():
    """多信号并存时取最大值."""
    now = time.monotonic()
    info = RateLimitInfo(
        retry_after_seconds=10.0,          # → now + 11
        requests_reset_at=now + 200,       # → now + 220
        tokens_reset_at=now + 100,         # → now + 110
    )
    deadline = compute_rate_limit_deadline(info)
    assert deadline is not None
    # 最大值是 requests_reset: 200 * 1.1 = 220
    assert 219 < deadline - now < 222


# --- compute_effective_retry_seconds (existing, regression) ---


def test_effective_retry_seconds_from_retry_after():
    info = RateLimitInfo(retry_after_seconds=60.0)
    result = compute_effective_retry_seconds(info)
    assert result is not None
    assert 65 < result < 67  # 60 * 1.1 = 66


def test_effective_retry_seconds_none_when_empty():
    info = RateLimitInfo()
    assert compute_effective_retry_seconds(info) is None


# --- parse_rate_limit_headers (existing, regression) ---


def test_parse_rate_limit_headers_cap_error():
    headers = {"retry-after": "120"}
    info = parse_rate_limit_headers(headers, 429, "usage cap exceeded")
    assert info.is_cap_error is True
    assert info.retry_after_seconds == 120.0


def test_parse_rate_limit_headers_non_rate_limit():
    """非 429/403 状态码 → 空信息."""
    info = parse_rate_limit_headers({}, 500, "internal error")
    assert info.retry_after_seconds is None
    assert info.is_cap_error is False
