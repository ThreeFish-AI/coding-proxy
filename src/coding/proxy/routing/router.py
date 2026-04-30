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
from .session_policy import SessionPolicyResolver
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
        session_policy_resolver: SessionPolicyResolver | None = None,
    ) -> None:
        if not tiers:
            raise ValueError("至少需要一个供应商层级")
        self._tiers = tiers
        self._active_vendor_name: str | None = (
            None  # 当前活跃供应商名称（由 Executor 成功时写入）
        )

        # 正交分解的子组件
        self._recorder = UsageRecorder(token_logger=token_logger)
        self._session_mgr = RouteSessionManager(compat_session_store)
        self._executor = _RouteExecutor(
            router=self,  # 传入 router 引用，用于写入活跃供应商状态
            tiers=tiers,
            usage_recorder=self._recorder,
            session_manager=self._session_mgr,
            reauth_coordinator=reauth_coordinator,
            session_policy_resolver=session_policy_resolver,
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

    # ── 运行时 N-tier 链路重排序 ─────────────────────────────

    def get_vendor_names(self) -> list[str]:
        """返回当前 tiers 的供应商名称列表（按优先级顺序）."""
        return [t.name for t in self._tiers]

    def reorder_tiers(self, vendor_names: list[str]) -> None:
        """原地重排序 N-tier 链路.

        使用切片赋值保持列表引用同一性，使 ``_RouteExecutor`` 立即可见。

        Args:
            vendor_names: 新的供应商名称顺序（必须包含所有当前 tier）。

        Raises:
            ValueError: 名称不存在、有重复、或未覆盖所有 tier。
        """
        name_to_tier = {t.name: t for t in self._tiers}
        current_names = set(name_to_tier)

        # 校验：重复
        if len(vendor_names) != len(set(vendor_names)):
            seen: set[str] = set()
            dups = [n for n in vendor_names if n in seen or seen.add(n)]  # type: ignore[func-returns-value]
            raise ValueError(f"vendor 名称重复: {', '.join(dups)}")

        # 校验：名称存在性
        unknown = [n for n in vendor_names if n not in current_names]
        if unknown:
            raise ValueError(
                f"未知 vendor: {', '.join(unknown)}; "
                f"可用: {', '.join(sorted(current_names))}"
            )

        # 校验：全量覆盖
        provided = set(vendor_names)
        if provided != current_names:
            missing = current_names - provided
            raise ValueError(f"缺少 vendor: {', '.join(sorted(missing))}")

        self._tiers[:] = [name_to_tier[n] for n in vendor_names]

    def promote_vendor(self, vendor_name: str) -> None:
        """将指定 vendor 提升至最高优先级，其余保持相对顺序.

        Args:
            vendor_name: 要提升的供应商名称。

        Raises:
            ValueError: 名称不存在。
        """
        current_names = self.get_vendor_names()
        if vendor_name not in current_names:
            available = sorted(t.name for t in self._tiers)
            raise ValueError(
                f"未知 vendor: {vendor_name}; 可用: {', '.join(available)}"
            )
        new_order = [vendor_name] + [n for n in current_names if n != vendor_name]
        self.reorder_tiers(new_order)

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
