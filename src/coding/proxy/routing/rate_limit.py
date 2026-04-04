"""速率限制信息解析 — 从 HTTP 响应头提取恢复时间."""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class RateLimitInfo:
    """从 429 响应中提取的速率限制信息."""

    retry_after_seconds: float | None = None
    requests_reset_at: float | None = None  # monotonic timestamp
    tokens_reset_at: float | None = None  # monotonic timestamp
    is_cap_error: bool = False


def parse_rate_limit_headers(
    headers: Any,
    status_code: int,
    error_body: str | None = None,
) -> RateLimitInfo:
    """从 HTTP 响应头和状态码解析速率限制信息.

    Args:
        headers: httpx.Headers 或 dict-like 响应头
        status_code: HTTP 状态码
        error_body: 错误响应体文本（用于检测 cap error）

    Returns:
        RateLimitInfo 实例
    """
    info = RateLimitInfo()

    if status_code not in (429, 403):
        return info

    # 检测 cap error
    if error_body:
        msg = error_body.lower()
        info.is_cap_error = any(
            p in msg for p in ("usage cap", "quota", "limit exceeded")
        )

    # 解析 retry-after (标准 HTTP header)
    retry_after = _get_header(headers, "retry-after")
    if retry_after:
        info.retry_after_seconds = _parse_retry_after(retry_after)

    # 解析 anthropic-ratelimit-requests-reset (ISO 8601 datetime)
    requests_reset = _get_header(headers, "anthropic-ratelimit-requests-reset")
    if requests_reset:
        info.requests_reset_at = _parse_reset_time(requests_reset)

    # 解析 anthropic-ratelimit-tokens-reset (ISO 8601 datetime)
    tokens_reset = _get_header(headers, "anthropic-ratelimit-tokens-reset")
    if tokens_reset:
        info.tokens_reset_at = _parse_reset_time(tokens_reset)

    return info


def compute_effective_retry_seconds(info: RateLimitInfo) -> float | None:
    """从 RateLimitInfo 中计算最保守的恢复等待时间.

    取所有可用信号中的最大值，并加 10% 安全余量。
    """
    candidates: list[float] = []

    if info.retry_after_seconds is not None:
        candidates.append(info.retry_after_seconds * 1.1)

    now = time.monotonic()
    if info.requests_reset_at is not None:
        remaining = info.requests_reset_at - now
        if remaining > 0:
            candidates.append(remaining * 1.1)

    if info.tokens_reset_at is not None:
        remaining = info.tokens_reset_at - now
        if remaining > 0:
            candidates.append(remaining * 1.1)

    return max(candidates) if candidates else None


def compute_rate_limit_deadline(info: RateLimitInfo) -> float | None:
    """从 RateLimitInfo 中计算最保守的恢复截止 monotonic 时间戳.

    与 compute_effective_retry_seconds() 互补:
    - 后者返回相对秒数（给 CircuitBreaker 用于退避计算）
    - 本函数返回绝对 monotonic 时间戳（给 VendorTier 用于精确门控）

    取所有可用时间信号中的最大值，并加 10% 安全余量。
    """
    candidates: list[float] = []
    now = time.monotonic()

    if info.retry_after_seconds is not None:
        candidates.append(now + info.retry_after_seconds * 1.1)

    if info.requests_reset_at is not None and info.requests_reset_at > now:
        remaining = info.requests_reset_at - now
        candidates.append(now + remaining * 1.1)

    if info.tokens_reset_at is not None and info.tokens_reset_at > now:
        remaining = info.tokens_reset_at - now
        candidates.append(now + remaining * 1.1)

    return max(candidates) if candidates else None


def _get_header(headers: Any, name: str) -> str | None:
    """统一获取 header 值（兼容 httpx.Headers 和 dict）."""
    if headers is None:
        return None
    if hasattr(headers, "get"):
        val = headers.get(name)
        return val if val else None
    if isinstance(headers, dict):
        lower_name = name.lower()
        for k, v in headers.items():
            if k.lower() == lower_name:
                return v
    return None


def _parse_retry_after(value: str) -> float | None:
    """解析 Retry-After header (秒数或 HTTP date)."""
    try:
        return float(value)
    except ValueError:
        pass
    try:
        dt = parsedate_to_datetime(value)
        return max(0, (dt - datetime.now(timezone.utc)).total_seconds())
    except (ValueError, TypeError):
        logger.warning("Cannot parse retry-after header: %s", value)
        return None


def _parse_reset_time(value: str) -> float | None:
    """解析 ISO 8601 datetime 为 monotonic timestamp."""
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        remaining = (dt - datetime.now(timezone.utc)).total_seconds()
        return time.monotonic() + max(0, remaining)
    except (ValueError, TypeError):
        logger.warning("Cannot parse reset time: %s", value)
        return None
