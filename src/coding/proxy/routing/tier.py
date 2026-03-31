"""后端层级 — 将后端实例与弹性设施（熔断器 + 配额守卫）聚合为路由单元."""

from __future__ import annotations

import logging

from dataclasses import dataclass, field

from ..backends.base import BaseBackend
from .circuit_breaker import CircuitBreaker, CircuitState
from .quota_guard import QuotaGuard, QuotaState

logger = logging.getLogger(__name__)


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

    def record_failure(
        self,
        *,
        is_cap_error: bool = False,
        retry_after_seconds: float | None = None,
    ) -> None:
        """记录失败：通知熔断器；如为 cap 错误则通知配额守卫.

        Args:
            is_cap_error: 是否为配额上限错误
            retry_after_seconds: 从响应头解析的建议恢复时间
        """
        if self.circuit_breaker:
            self.circuit_breaker.record_failure(retry_after_seconds=retry_after_seconds)
        if self.quota_guard and is_cap_error:
            self.quota_guard.notify_cap_error(retry_after_seconds=retry_after_seconds)

    async def can_execute_with_health_check(self) -> bool:
        """带健康检查的可用性判断（异步，慢路径）.

        正常状态快速返回；探测场景先执行后端健康检查，通过后才允许真实请求。
        """
        cb_allows = self.circuit_breaker.can_execute() if self.circuit_breaker else True
        qg_allows = self.quota_guard.can_use_primary() if self.quota_guard else True

        if not cb_allows and not qg_allows:
            return False

        # 检测是否为探测场景
        is_probe_scenario = False
        if self.circuit_breaker:
            if self.circuit_breaker.state == CircuitState.HALF_OPEN:
                is_probe_scenario = True
        if self.quota_guard:
            # QG 允许探测（在 QUOTA_EXCEEDED 状态下但返回 True）
            if self.quota_guard._state == QuotaState.QUOTA_EXCEEDED and qg_allows:
                is_probe_scenario = True

        if not is_probe_scenario:
            return cb_allows and qg_allows

        # 探测场景：先做健康检查
        logger.info("Tier %s: probe scenario, running health check", self.name)
        healthy = await self.backend.check_health()
        if not healthy:
            logger.warning("Tier %s: health check failed, staying degraded", self.name)
            self.record_failure()
            return False

        logger.info("Tier %s: health check passed, allowing request", self.name)
        return True
