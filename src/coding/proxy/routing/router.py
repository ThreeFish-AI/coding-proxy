"""请求路由器 — N-tier 链式路由与自动故障转移（薄代理层）.

核心路由逻辑已正交分解至：
- :mod:`.executor`      — 统一的 tier 迭代门控引擎 (_RouteExecutor)
- :mod:`.usage_recorder`  — 用量记录、定价日志与证据构建 (UsageRecorder)
- :mod:`.session_manager`— 兼容性会话生命周期管理 (RouteSessionManager)

本文件保留 ``RequestRouter`` 公开接口，内部委托给上述模块。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from ..pricing import PricingTable

from .executor import _RouteExecutor
from .session_manager import RouteSessionManager
from .tier import VendorTier

# 向后兼容别名
BackendTier = VendorTier
from ..compat.session_store import CompatSessionStore
from ..logging.db import TokenLogger
from .usage_recorder import UsageRecorder


class RequestRouter:
    """路由请求到合适的供应商层级，按优先级链式故障转移."""

    def __init__(
        self,
        tiers: list[VendorTier],
        token_logger: TokenLogger | None = None,
        reauth_coordinator: Any | None = None,
        compat_session_store: CompatSessionStore | None = None,
    ) -> None:
        if not tiers:
            raise ValueError("至少需要一个供应商层级")
        self._tiers = tiers
        self._active_vendor_name: str | None = None  # 当前活跃供应商名称（由 Executor 成功时写入）

        # 正交分解的子组件
        self._recorder = UsageRecorder(token_logger=token_logger)
        self._session_mgr = RouteSessionManager(compat_session_store)
        self._executor = _RouteExecutor(
            router=self,  # 传入 router 引用，用于写入活跃供应商状态
            tiers=tiers,
            usage_recorder=self._recorder,
            session_manager=self._session_mgr,
            reauth_coordinator=reauth_coordinator,
        )

    def set_pricing_table(self, table: PricingTable) -> None:
        """注入 PricingTable 实例（由 lifespan 在启动阶段调用）."""
        self._recorder.set_pricing_table(table)

    @property
    def tiers(self) -> list[VendorTier]:
        return self._tiers

    @property
    def active_vendor_name(self) -> str | None:
        """当前活跃供应商名称（由 Executor 在成功响应时写入）."""
        return self._active_vendor_name

    # ── 公开路由接口（委托给 _RouteExecutor）───────────────

    async def route_stream(
        self,
        body: dict[str, Any],
        headers: dict[str, str],
    ) -> AsyncIterator[tuple[bytes, str]]:
        """路由流式请求，按优先级尝试各层级."""
        async for chunk, vendor_name in self._executor.execute_stream(body, headers):
            yield chunk, vendor_name

    async def route_message(
        self,
        body: dict[str, Any],
        headers: dict[str, str],
    ) -> Any:
        """路由非流式请求，按优先级尝试各层级."""
        return await self._executor.execute_message(body, headers)

    # ── 生命周期 ───────────────────────────────────────────

    async def close(self) -> None:
        for tier in self._tiers:
            await tier.vendor.close()
