"""后端层级 — 将后端实例与弹性设施（熔断器 + 配额守卫）聚合为路由单元."""

from __future__ import annotations

from dataclasses import dataclass, field

from ..backends.base import BaseBackend
from .circuit_breaker import CircuitBreaker
from .quota_guard import QuotaGuard


@dataclass
class BackendTier:
    """一个路由层级：后端实例 + 关联的熔断器和配额守卫."""

    backend: BaseBackend
    circuit_breaker: CircuitBreaker | None = field(default=None)
    quota_guard: QuotaGuard | None = field(default=None)

    @property
    def name(self) -> str:
        return self.backend.get_name()

    @property
    def is_terminal(self) -> bool:
        """终端层无熔断器，不触发故障转移."""
        return self.circuit_breaker is None

    def can_execute(self) -> bool:
        """综合判断此层是否可用."""
        if self.circuit_breaker and not self.circuit_breaker.can_execute():
            return False
        if self.quota_guard and not self.quota_guard.can_use_primary():
            return False
        return True

    def record_success(self, usage_tokens: int = 0) -> None:
        """记录成功：通知熔断器和配额守卫."""
        if self.circuit_breaker:
            self.circuit_breaker.record_success()
        if self.quota_guard:
            self.quota_guard.record_primary_success()
            if usage_tokens > 0:
                self.quota_guard.record_usage(usage_tokens)

    def record_failure(self, *, is_cap_error: bool = False) -> None:
        """记录失败：通知熔断器；如为 cap 错误则通知配额守卫."""
        if self.circuit_breaker:
            self.circuit_breaker.record_failure()
        if self.quota_guard and is_cap_error:
            self.quota_guard.notify_cap_error()
