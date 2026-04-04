"""供应商层级 — 将供应商实例与弹性设施（熔断器 + 配额守卫）聚合为路由单元."""

from __future__ import annotations

import logging
import time

from dataclasses import dataclass, field

from ..vendors.base import BaseVendor
from .circuit_breaker import CircuitBreaker, CircuitState
from .quota_guard import QuotaGuard, QuotaState
from .retry import RetryConfig

logger = logging.getLogger(__name__)


@dataclass
class VendorTier:
    """一个路由层级：供应商实例 + 关联的熔断器和配额守卫."""

    vendor: BaseVendor
    circuit_breaker: CircuitBreaker | None = field(default=None)
    quota_guard: QuotaGuard | None = field(default=None)
    weekly_quota_guard: QuotaGuard | None = field(default=None)
    retry_config: RetryConfig | None = field(default=None)

    # Rate Limit 精确截止时间（monotonic timestamp），0 表示无限制
    _rate_limit_deadline: float = field(default=0.0, repr=False)

    @property
    def name(self) -> str:
        return self.vendor.get_name()

    @property
    def is_terminal(self) -> bool:
        """终端层无熔断器，不触发故障转移."""
        return self.circuit_breaker is None

    @property
    def rate_limit_remaining_seconds(self) -> float:
        """Rate limit 剩余等待秒数（<= 0 表示已到期）."""
        return max(0.0, self._rate_limit_deadline - time.monotonic())

    @property
    def is_rate_limited(self) -> bool:
        """是否处于 rate limit 冷却期."""
        return self._rate_limit_deadline > time.monotonic()

    def can_execute(self) -> bool:
        """综合判断此层是否可用."""
        if self.circuit_breaker and not self.circuit_breaker.can_execute():
            return False
        if self.quota_guard and not self.quota_guard.can_use_primary():
            return False
        if self.weekly_quota_guard and not self.weekly_quota_guard.can_use_primary():
            return False
        return True

    def record_success(self, usage_tokens: int = 0) -> None:
        """记录成功：通知熔断器和配额守卫，清除 rate limit deadline."""
        if self.circuit_breaker:
            self.circuit_breaker.record_success()
        if self.quota_guard:
            self.quota_guard.record_primary_success()
            if usage_tokens > 0:
                self.quota_guard.record_usage(usage_tokens)
        if self.weekly_quota_guard:
            self.weekly_quota_guard.record_primary_success()
            if usage_tokens > 0:
                self.weekly_quota_guard.record_usage(usage_tokens)
        self._rate_limit_deadline = 0.0

    def record_failure(
        self,
        *,
        is_cap_error: bool = False,
        retry_after_seconds: float | None = None,
        rate_limit_deadline: float | None = None,
    ) -> None:
        """记录失败：通知熔断器；如为 cap 错误则通知配额守卫.

        Args:
            is_cap_error: 是否为配额上限错误
            retry_after_seconds: 从响应头解析的建议恢复时间
            rate_limit_deadline: 精确的 rate limit 截止 monotonic 时间戳
        """
        if self.circuit_breaker:
            self.circuit_breaker.record_failure(retry_after_seconds=retry_after_seconds)
        if self.quota_guard and is_cap_error:
            self.quota_guard.notify_cap_error(retry_after_seconds=retry_after_seconds)
        if self.weekly_quota_guard and is_cap_error:
            self.weekly_quota_guard.notify_cap_error(retry_after_seconds=retry_after_seconds)

        if rate_limit_deadline is not None and rate_limit_deadline > self._rate_limit_deadline:
            self._rate_limit_deadline = rate_limit_deadline
            logger.info(
                "Tier %s: rate limit deadline updated, %.1fs remaining",
                self.name,
                rate_limit_deadline - time.monotonic(),
            )

    async def can_execute_with_health_check(self) -> bool:
        """带健康检查的可用性判断（异步，慢路径）.

        三层恢复门控:
        1. Rate Limit Deadline — 截止时间未到，直接拒绝
        2. Health Check — 轻量级供应商健康探测
        3. Cautious Probe — 通过前两层后，允许真实请求作为探针
        """
        # ── 第一层: Rate Limit Deadline 门控 ──
        if self.is_rate_limited:
            remaining = self.rate_limit_remaining_seconds
            logger.debug(
                "Tier %s: rate limit deadline active, %.1fs remaining, blocking",
                self.name,
                remaining,
            )
            return False

        cb_allows = self.circuit_breaker.can_execute() if self.circuit_breaker else True
        qg_allows = self.quota_guard.can_use_primary() if self.quota_guard else True
        wqg_allows = self.weekly_quota_guard.can_use_primary() if self.weekly_quota_guard else True

        if not cb_allows and not qg_allows and not wqg_allows:
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
        if self.weekly_quota_guard:
            if self.weekly_quota_guard._state == QuotaState.QUOTA_EXCEEDED and wqg_allows:
                is_probe_scenario = True

        if not is_probe_scenario:
            return cb_allows and qg_allows and wqg_allows

        # ── 第二层: Health Check 门控 ──
        logger.info("Tier %s: probe scenario, running health check", self.name)
        healthy = await self.vendor.check_health()
        if not healthy:
            logger.warning("Tier %s: health check failed, staying degraded", self.name)
            self.record_failure()
            return False

        # ── 第三层: Cautious Probe（允许真实请求通过）──
        logger.info("Tier %s: health check passed, allowing cautious probe", self.name)
        return True

    def reset_rate_limit(self) -> None:
        """手动清除 rate limit deadline."""
        self._rate_limit_deadline = 0.0

    def get_rate_limit_info(self) -> dict:
        """获取 rate limit deadline 状态信息."""
        now = time.monotonic()
        remaining = max(0.0, self._rate_limit_deadline - now)
        return {
            "is_rate_limited": self._rate_limit_deadline > now,
            "remaining_seconds": round(remaining, 1),
        }


# 向后兼容别名
BackendTier = VendorTier
