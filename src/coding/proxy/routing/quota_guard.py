"""用量配额守卫 (Quota Guard) — 滑动窗口限额与探测恢复."""

from __future__ import annotations

import logging
import threading
import time
from collections import deque
from enum import Enum

logger = logging.getLogger(__name__)


class QuotaState(Enum):
    WITHIN_QUOTA = "within_quota"
    QUOTA_EXCEEDED = "quota_exceeded"


class QuotaGuard:
    """基于滑动窗口的用量配额守卫.

    状态转换:
    - WITHIN_QUOTA → QUOTA_EXCEEDED: 窗口用量 >= budget × threshold% 或检测到 cap 错误
    - QUOTA_EXCEEDED → WITHIN_QUOTA: 窗口用量自然滑出 < threshold% 或探测成功
    """

    def __init__(
        self,
        *,
        enabled: bool = False,
        token_budget: int = 0,
        window_seconds: int = 18000,
        threshold_percent: float = 99.0,
        probe_interval_seconds: int = 300,
    ) -> None:
        self._enabled = enabled
        self._budget = token_budget
        self._window = window_seconds
        self._threshold = threshold_percent / 100.0
        self._probe_interval = probe_interval_seconds

        self._state = QuotaState.WITHIN_QUOTA
        self._entries: deque[tuple[float, int]] = deque()
        self._total: int = 0
        self._last_probe: float = 0.0
        self._cap_error_active: bool = False
        self._effective_probe_interval: float = probe_interval_seconds
        self._lock = threading.Lock()

    @property
    def enabled(self) -> bool:
        return self._enabled

    @property
    def window_hours(self) -> float:
        """滑动窗口小时数（供基线加载使用）."""
        return self._window / 3600

    @property
    def _window_label(self) -> str:
        """人类可读的窗口周期短标签."""
        w = self._window
        if w >= 86400 and w % 86400 == 0:
            return f"{w // 86400}d"
        if w >= 3600 and w % 3600 == 0:
            return f"{w // 3600}h"
        if w >= 60 and w % 60 == 0:
            return f"{w // 60}m"
        return f"{w}s"

    def can_use_primary(self) -> bool:
        """判断是否可以使用主后端."""
        if not self._enabled:
            return True
        with self._lock:
            self._expire()
            if self._state == QuotaState.WITHIN_QUOTA:
                if self._budget > 0 and self._total >= int(
                    self._budget * self._threshold
                ):
                    self._transition_to(QuotaState.QUOTA_EXCEEDED)
                    logger.warning(
                        "Quota guard [%s]: WITHIN_QUOTA → EXCEEDED (%.1f%%)",
                        self._window_label,
                        self._total / self._budget * 100,
                    )
                    return False
                return True
            # QUOTA_EXCEEDED — cap 错误触发时仅允许探测恢复，不做预算自动恢复
            if (
                not self._cap_error_active
                and self._budget > 0
                and self._total < int(self._budget * self._threshold)
            ):
                self._transition_to(QuotaState.WITHIN_QUOTA)
                logger.info(
                    "Quota guard [%s]: EXCEEDED → WITHIN_QUOTA (usage dropped)",
                    self._window_label,
                )
                return True
            now = time.monotonic()
            if now - self._last_probe >= self._effective_probe_interval:
                self._last_probe = now
                logger.info(
                    "Quota guard [%s]: allowing probe request",
                    self._window_label,
                )
                return True
            return False

    def record_usage(self, tokens: int) -> None:
        """记录新 token 用量到滑动窗口."""
        if not self._enabled or tokens <= 0:
            return
        with self._lock:
            self._entries.append((time.monotonic(), tokens))
            self._total += tokens

    def record_primary_success(self) -> None:
        """记录主后端请求成功（探测恢复触发点）."""
        if not self._enabled:
            return
        with self._lock:
            if self._state == QuotaState.QUOTA_EXCEEDED:
                self._transition_to(QuotaState.WITHIN_QUOTA)
                logger.info(
                    "Quota guard [%s]: EXCEEDED → WITHIN_QUOTA (probe success)",
                    self._window_label,
                )

    def notify_cap_error(self, retry_after_seconds: float | None = None) -> None:
        """外部通知检测到用量上限错误.

        Args:
            retry_after_seconds: 从响应头解析的建议恢复时间。
                若提供，更新探测间隔以避免过早探测。
        """
        if not self._enabled:
            return
        with self._lock:
            if self._state != QuotaState.QUOTA_EXCEEDED:
                self._transition_to(QuotaState.QUOTA_EXCEEDED)
            if retry_after_seconds is not None:
                self._effective_probe_interval = max(
                    retry_after_seconds * 1.1,
                    self._probe_interval,
                )
            self._cap_error_active = True
            logger.warning(
                "Quota guard [%s]: cap error detected → EXCEEDED (effective_probe=%ds)",
                self._window_label,
                int(self._effective_probe_interval),
            )

    def load_baseline(self, total_tokens: int, vendor: str | None = None) -> None:
        """从数据库加载窗口历史用量基线."""
        if not self._enabled or total_tokens <= 0:
            return
        with self._lock:
            midpoint = time.monotonic() - self._window / 2
            self._entries.append((midpoint, total_tokens))
            self._total += total_tokens
            if vendor:
                logger.info(
                    "Quota guard [%s/%s]: loaded baseline %d tokens",
                    vendor,
                    self._window_label,
                    total_tokens,
                )
            else:
                logger.info(
                    "Quota guard [%s]: loaded baseline %d tokens",
                    self._window_label,
                    total_tokens,
                )

    def reset(self) -> None:
        """手动重置为 WITHIN_QUOTA 状态."""
        with self._lock:
            self._transition_to(QuotaState.WITHIN_QUOTA)
            self._entries.clear()
            self._total = 0
            logger.info(
                "Quota guard [%s]: manually reset to WITHIN_QUOTA",
                self._window_label,
            )

    def get_info(self) -> dict:
        """获取配额守卫状态信息."""
        with self._lock:
            self._expire()
            return {
                "state": self._state.value,
                "window_usage_tokens": self._total,
                "budget_tokens": self._budget,
                "usage_percent": round(self._total / self._budget * 100, 1)
                if self._budget > 0
                else 0,
                "threshold_percent": self._threshold * 100,
                "window_hours": self.window_hours,
            }

    def _expire(self) -> None:
        """清除超出时间窗口的条目."""
        cutoff = time.monotonic() - self._window
        while self._entries and self._entries[0][0] < cutoff:
            _, tokens = self._entries.popleft()
            self._total -= tokens

    def _transition_to(self, new_state: QuotaState) -> None:
        self._state = new_state
        if new_state == QuotaState.WITHIN_QUOTA:
            self._cap_error_active = False
            self._effective_probe_interval = self._probe_interval
        elif new_state == QuotaState.QUOTA_EXCEEDED:
            self._last_probe = time.monotonic()
