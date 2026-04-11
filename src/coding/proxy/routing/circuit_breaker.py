"""熔断器 (Circuit Breaker) — 状态机实现."""

from __future__ import annotations

import logging
import threading
import time
from enum import Enum

logger = logging.getLogger(__name__)


class CircuitState(Enum):
    CLOSED = "closed"  # 正常：使用主后端
    OPEN = "open"  # 故障：使用备选后端
    HALF_OPEN = "half_open"  # 试探：测试主后端是否恢复


class CircuitBreaker:
    """线程安全的熔断器.

    状态转换:
    - CLOSED → OPEN: 连续 failure_threshold 次失败
    - OPEN → HALF_OPEN: recovery_timeout 后
    - HALF_OPEN → CLOSED: 连续 success_threshold 次成功
    - HALF_OPEN → OPEN: 任意一次失败（指数退避）
    """

    def __init__(
        self,
        failure_threshold: int = 3,
        recovery_timeout_seconds: int = 300,
        success_threshold: int = 2,
        max_recovery_seconds: int = 3600,
        *,
        vendor_name: str = "",
    ) -> None:
        self._failure_threshold = failure_threshold
        self._recovery_timeout = recovery_timeout_seconds
        self._success_threshold = success_threshold
        self._max_recovery = max_recovery_seconds
        self._vendor_label = f" [{vendor_name}]" if vendor_name else ""

        self._state = CircuitState.CLOSED
        self._failure_count = 0
        self._success_count = 0
        self._last_failure_time: float | None = None
        self._current_recovery = recovery_timeout_seconds
        self._lock = threading.Lock()

    @property
    def state(self) -> CircuitState:
        with self._lock:
            self._check_recovery()
            return self._state

    def can_execute(self) -> bool:
        """判断是否可以在主后端上执行请求."""
        with self._lock:
            self._check_recovery()
            return self._state in (CircuitState.CLOSED, CircuitState.HALF_OPEN)

    def record_success(self) -> None:
        """记录一次成功调用."""
        with self._lock:
            self._failure_count = 0
            if self._state == CircuitState.HALF_OPEN:
                self._success_count += 1
                if self._success_count >= self._success_threshold:
                    self._transition_to(CircuitState.CLOSED)
                    logger.info(
                        "Circuit breaker%s: HALF_OPEN → CLOSED "
                        "(recovered, %d/%d consecutive successes)",
                        self._vendor_label,
                        self._success_count,
                        self._success_threshold,
                    )
            elif self._state == CircuitState.CLOSED:
                # 正常状态下成功，无需操作
                pass

    def record_failure(
        self,
        retry_after_seconds: float | None = None,
        *,
        force_open: bool = False,
    ) -> None:
        """记录一次失败调用.

        Args:
            retry_after_seconds: 从响应头解析出的建议恢复时间（秒）。
                若提供且大于当前指数退避值，将覆盖以避免过早探测。
            force_open: 是否忽略 failure_threshold，立即将熔断器转为 OPEN。
                用于 429/rate limit 等具有明确恢复窗口的错误，
                此类错误无需多次采样即可确定供应商不可用。
        """
        with self._lock:
            self._failure_count += 1
            self._success_count = 0
            self._last_failure_time = time.monotonic()

            if self._state == CircuitState.HALF_OPEN:
                self._transition_to(CircuitState.OPEN)
                self._backoff_recovery(hint_seconds=retry_after_seconds)
                logger.warning(
                    "Circuit breaker%s: HALF_OPEN → OPEN "
                    "(recovery probe failed, backoff %ds → next retry in %ds)",
                    self._vendor_label,
                    self._current_recovery,
                    self._current_recovery,
                )
            elif self._state == CircuitState.CLOSED:
                if force_open or self._failure_count >= self._failure_threshold:
                    self._transition_to(CircuitState.OPEN)
                    # force_open 场景下，retry-after 是权威信号（如 429 Retry-After），
                    # 即使不超过当前退避值也应采用；非 force_open 时仅在有优势时覆盖。
                    if force_open and retry_after_seconds is not None:
                        self._current_recovery = min(
                            retry_after_seconds,
                            self._max_recovery,
                        )
                    elif (
                        retry_after_seconds
                        and retry_after_seconds > self._current_recovery
                    ):
                        self._current_recovery = min(
                            retry_after_seconds,
                            self._max_recovery,
                        )
                    if force_open:
                        logger.warning(
                            "Circuit breaker%s: CLOSED → OPEN "
                            "(forced, rate-limited, retry-after=%ss → next retry in %ds)",
                            self._vendor_label,
                            retry_after_seconds or "N/A",
                            self._current_recovery,
                        )
                    else:
                        logger.warning(
                            "Circuit breaker%s: CLOSED → OPEN "
                            "(%d consecutive failures, next retry in %ds)",
                            self._vendor_label,
                            self._failure_count,
                            self._current_recovery,
                        )

    def reset(self) -> None:
        """手动重置熔断器为 CLOSED 状态."""
        with self._lock:
            self._transition_to(CircuitState.CLOSED)
            self._current_recovery = self._recovery_timeout
            logger.info(
                "Circuit breaker%s: manually reset to CLOSED", self._vendor_label
            )

    def get_info(self) -> dict:
        """获取熔断器状态信息."""
        with self._lock:
            self._check_recovery()
            return {
                "state": self._state.value,
                "failure_count": self._failure_count,
                "success_count": self._success_count,
                "current_recovery_seconds": self._current_recovery,
                "last_failure_time": self._last_failure_time,
            }

    def _check_recovery(self) -> None:
        """检查是否应从 OPEN 转为 HALF_OPEN."""
        if self._state != CircuitState.OPEN:
            return
        if self._last_failure_time is None:
            return
        elapsed = time.monotonic() - self._last_failure_time
        if elapsed >= self._current_recovery:
            self._transition_to(CircuitState.HALF_OPEN)
            elapsed_s = int(elapsed)
            logger.info(
                "Circuit breaker%s: OPEN → HALF_OPEN (recovery timeout, waited %ds/%ds)",
                self._vendor_label,
                elapsed_s,
                self._current_recovery,
            )

    def _transition_to(self, new_state: CircuitState) -> None:
        self._state = new_state
        if new_state == CircuitState.CLOSED:
            self._failure_count = 0
            self._success_count = 0
            self._current_recovery = self._recovery_timeout
        elif new_state == CircuitState.HALF_OPEN:
            self._success_count = 0

    def _backoff_recovery(self, hint_seconds: float | None = None) -> None:
        """指数退避恢复超时，支持 server-hinted 覆盖."""
        exponential = min(self._current_recovery * 2, self._max_recovery)
        if hint_seconds is not None and hint_seconds > exponential:
            # Server 告知的恢复时间优先于指数退避
            self._current_recovery = min(hint_seconds, self._max_recovery)
        else:
            self._current_recovery = exponential
