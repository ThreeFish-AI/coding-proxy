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
        self._lock = threading.Lock()

    @property
    def enabled(self) -> bool:
        return self._enabled

    def can_use_primary(self) -> bool:
        """判断是否可以使用主后端."""
        if not self._enabled:
            return True
        with self._lock:
            self._expire()
            if self._state == QuotaState.WITHIN_QUOTA:
                if self._budget > 0 and self._total >= int(self._budget * self._threshold):
                    self._transition_to(QuotaState.QUOTA_EXCEEDED)
                    logger.warning(
                        "Quota guard: WITHIN_QUOTA → EXCEEDED (%.1f%%)",
                        self._total / self._budget * 100,
                    )
                    return False
                return True
            # QUOTA_EXCEEDED — cap 错误触发时仅允许探测恢复，不做预算自动恢复
            if not self._cap_error_active and self._budget > 0 and self._total < int(self._budget * self._threshold):
                self._transition_to(QuotaState.WITHIN_QUOTA)
                logger.info("Quota guard: EXCEEDED → WITHIN_QUOTA (usage dropped)")
                return True
            now = time.monotonic()
            if now - self._last_probe >= self._probe_interval:
                self._last_probe = now
                logger.info("Quota guard: allowing probe request")
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
                logger.info("Quota guard: EXCEEDED → WITHIN_QUOTA (probe success)")

    def notify_cap_error(self) -> None:
        """外部通知检测到用量上限错误."""
        if not self._enabled:
            return
        with self._lock:
            if self._state != QuotaState.QUOTA_EXCEEDED:
                self._transition_to(QuotaState.QUOTA_EXCEEDED)
                self._cap_error_active = True
                logger.warning("Quota guard: cap error detected → EXCEEDED")

    def load_baseline(self, total_tokens: int) -> None:
        """从数据库加载窗口历史用量基线."""
        if not self._enabled or total_tokens <= 0:
            return
        with self._lock:
            midpoint = time.monotonic() - self._window / 2
            self._entries.append((midpoint, total_tokens))
            self._total += total_tokens
            logger.info("Quota guard: loaded baseline %d tokens", total_tokens)

    def reset(self) -> None:
        """手动重置为 WITHIN_QUOTA 状态."""
        with self._lock:
            self._transition_to(QuotaState.WITHIN_QUOTA)
            self._entries.clear()
            self._total = 0
            logger.info("Quota guard: manually reset to WITHIN_QUOTA")

    def get_info(self) -> dict:
        """获取配额守卫状态信息."""
        with self._lock:
            self._expire()
            return {
                "state": self._state.value,
                "window_usage_tokens": self._total,
                "budget_tokens": self._budget,
                "usage_percent": round(self._total / self._budget * 100, 1) if self._budget > 0 else 0,
                "threshold_percent": self._threshold * 100,
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
        elif new_state == QuotaState.QUOTA_EXCEEDED:
            self._last_probe = time.monotonic()
