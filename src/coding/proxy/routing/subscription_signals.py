"""订阅用量被动信号 — Anthropic unified ratelimit 响应头旁路采集.

Anthropic 官方通道是纯透传（代理不持有 OAuth 凭证），Claude Code 订阅的
5h/7d 用量只能从上游响应头被动采集（OAuth 订阅流量专属的
``anthropic-ratelimit-unified-*`` 头；API key 流量不返回，解析得空即跳过）。

单位契约（社区逆向共识，与 ``/api/oauth/usage`` JSON 不同源）：
- ``*-utilization``：0.0–1.0 小数（JSON 端点为 0–100，勿混淆）
- ``*-reset``：Unix epoch 秒（JSON 端点为 ISO 8601，勿混淆）

存内存快照（进程级单例）：随首个订阅请求出现、随每次请求刷新，
重启后清零属预期（无落库必要，下一个请求即回填）。
"""

from __future__ import annotations

import math
import time
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

import httpx

from .rate_limit import _get_header

_UNIFIED_PREFIX = "anthropic-ratelimit-unified-"


@dataclass(frozen=True)
class AnthropicUsageSnapshot:
    """Anthropic 订阅用量被动快照（单次响应头的瞬时值）."""

    five_hour_percent: float | None
    five_hour_reset_epoch: int | None
    seven_day_percent: float | None
    seven_day_reset_epoch: int | None
    status: str | None
    captured_at_epoch: float


def _parse_percent(raw: str | None) -> float | None:
    """0.0–1.0 小数 → 0–100 百分比（钳制），解析失败返回 None."""
    if raw is None:
        return None
    try:
        value = float(raw)
    except ValueError:
        return None
    if not math.isfinite(value):
        return None
    return max(0.0, min(100.0, value * 100.0))


def _parse_epoch(raw: str | None) -> int | None:
    if raw is None:
        return None
    try:
        value = float(raw)
    except ValueError:
        return None
    if not math.isfinite(value):
        return None
    return int(value)


def parse_unified_ratelimit(
    headers: Mapping[str, Any] | httpx.Headers,
) -> AnthropicUsageSnapshot | None:
    """解析 unified ratelimit 头；无任何 unified 头（API key 流量）返回 None.

    取头复用 ``rate_limit._get_header``：大小写无关仅对 httpx.Headers
    保证；普通 dict 混合大小写键会走大小写敏感的 dict.get（生产路径
    只传 httpx.Headers，不受影响）。
    """
    five_hour_raw = _get_header(headers, f"{_UNIFIED_PREFIX}5h-utilization")
    seven_day_raw = _get_header(headers, f"{_UNIFIED_PREFIX}7d-utilization")
    if five_hour_raw is None and seven_day_raw is None:
        return None
    return AnthropicUsageSnapshot(
        five_hour_percent=_parse_percent(five_hour_raw),
        five_hour_reset_epoch=_parse_epoch(
            _get_header(headers, f"{_UNIFIED_PREFIX}5h-reset")
        ),
        seven_day_percent=_parse_percent(seven_day_raw),
        seven_day_reset_epoch=_parse_epoch(
            _get_header(headers, f"{_UNIFIED_PREFIX}7d-reset")
        ),
        status=_get_header(headers, f"{_UNIFIED_PREFIX}status"),
        captured_at_epoch=time.time(),
    )


class _AnthropicSignalStore:
    """进程级快照单例（GIL 下单赋值原子，无需锁）."""

    def __init__(self) -> None:
        self._snapshot: AnthropicUsageSnapshot | None = None

    def capture(self, headers: Mapping[str, Any] | httpx.Headers) -> None:
        """旁路采集：解析失败 / 非 unified 头静默跳过，绝不影响转发链路."""
        try:
            snapshot = parse_unified_ratelimit(headers)
        except Exception:  # pragma: no cover - 防御性：采集绝不抛出
            snapshot = None
        if snapshot is not None:
            self._snapshot = snapshot

    def get(self) -> AnthropicUsageSnapshot | None:
        return self._snapshot

    def reset(self) -> None:
        """清空快照（测试用）."""
        self._snapshot = None


_store = _AnthropicSignalStore()


def capture_anthropic_ratelimit(headers: Mapping[str, Any] | httpx.Headers) -> None:
    """供 vendor 钩子调用的旁路采集入口."""
    _store.capture(headers)


def get_anthropic_snapshot() -> AnthropicUsageSnapshot | None:
    """读取最近一次采集的快照（Dashboard 订阅状态消费）."""
    return _store.get()


def reset_anthropic_snapshot() -> None:
    """清空快照（测试隔离用）."""
    _store.reset()
