"""请求路由器 — N-tier 链式路由与自动故障转移（薄代理层）.

核心路由逻辑已正交分解至：
- :mod:`.executor`      — 统一的 tier 迭代门控引擎 (_RouteExecutor)
- :mod:`.usage_recorder`  — 用量记录、定价日志与证据构建 (UsageRecorder)
- :mod:`.session_manager`— 兼容性会话生命周期管理 (RouteSessionManager)

本文件保留 ``RequestRouter`` 公开接口，内部委托给上述模块。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, AsyncIterator

if TYPE_CHECKING:
    from ..pricing import PricingTable

from .executor import _RouteExecutor
from .session_manager import RouteSessionManager
from .tier import BackendTier
from .usage_recorder import UsageRecorder
from ..compat.session_store import CompatSessionStore
from ..logging.db import TokenLogger


class RequestRouter:
    """路由请求到合适的后端层级，按优先级链式故障转移."""

    def __init__(
        self,
        tiers: list[BackendTier],
        token_logger: TokenLogger | None = None,
        reauth_coordinator: Any | None = None,
        compat_session_store: CompatSessionStore | None = None,
    ) -> None:
        if not tiers:
            raise ValueError("至少需要一个后端层级")
        self._tiers = tiers

        # 正交分解的子组件
        self._recorder = UsageRecorder(token_logger=token_logger)
        self._session_mgr = RouteSessionManager(compat_session_store)
        self._executor = _RouteExecutor(
            tiers=tiers,
            usage_recorder=self._recorder,
            session_manager=self._session_mgr,
            reauth_coordinator=reauth_coordinator,
        )

    def set_pricing_table(self, table: PricingTable) -> None:
        """注入 PricingTable 实例（由 lifespan 在启动阶段调用）."""
        self._recorder.set_pricing_table(table)

    @property
    def tiers(self) -> list[BackendTier]:
        return self._tiers

    # ── 公开路由接口（委托给 _RouteExecutor）───────────────

    async def route_stream(
        self,
        body: dict[str, Any],
        headers: dict[str, str],
    ) -> AsyncIterator[tuple[bytes, str]]:
        """路由流式请求，按优先级尝试各层级."""
        async for chunk, backend_name in self._executor.execute_stream(body, headers):
            yield chunk, backend_name

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
            await tier.backend.close()
