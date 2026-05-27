"""统一并发控制器 — 支持监控 (monitor) 与限流 (limited) 双模式.

为每个映射后的模型（如 ``glm-5v-turbo``）独立维护一个 ``_ConcurrencySlot``，
根据模式提供不同语义：

  **monitor 模式** (config=None)
    - 仅计数 ``in_use``，不做排队与限流
    - ``pending`` 恒为 0，``available`` / ``limit`` 为 None
    - 所有 vendor 默认使用此模式

  **limited 模式** (config 非 None)
    - ``in_use`` 不超过 ``limit`` 时立即获取，超限时 FIFO 排队
    - ``pending`` 反映当前排队数，``peak_pending_recent`` 记录最近 10s 峰值
    - 由 ZhipuVendor 等需限流的 vendor 启用

设计要点：
  - **惰性创建**：仅在首次请求到达时才为该模型创建 Slot，避免冷启动开销
  - **FIFO 公平**：``asyncio.Event`` + while 循环天然满足 FIFO 排队语义（limited 模式）
  - **动态调整**：支持运行时修改 per-model limit，无需重启进程
  - **按映射后模型名键控**：与上游真实承载能力对齐，而非按客户端请求名
  - **峰值余晖**：记录 ``peak_pending_recent`` 使瞬时排队可观测
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from contextlib import asynccontextmanager
from typing import Any, Literal

from ..config.vendors import ZhipuConcurrencyConfig

logger = logging.getLogger(__name__)

# peak_pending_recent 滑窗宽度（秒）
_PEAK_WINDOW_SECONDS = 10.0


class _ConcurrencySlot:
    """支持双模式的并发槽位.

    ``limit=None`` (monitor) 时 acquire 走 fast path，仅计数。
    ``limit>0`` (limited) 时在满槽位后 FIFO 排队等待。
    """

    def __init__(self, limit: int | None) -> None:
        self._limit = limit
        self._in_use: int = 0
        self._pending: int = 0
        self._wake = asyncio.Event()
        self._wake.set()
        # peak_pending_recent 追踪：存储 (timestamp, pending_value) 元组
        self._peak_samples: deque[tuple[float, int]] = deque()

    async def acquire(self) -> None:
        """获取一个并发槽位.

        monitor 模式 (limit=None)：仅 in_use++，永不排队。
        limited 模式 (limit>0)：满槽时阻塞等待。
        """
        # monitor 模式：仅计数
        if self._limit is None:
            self._in_use += 1
            return

        # limited — fast path
        if self._in_use < self._limit:
            self._in_use += 1
            return

        # limited — slow path: FIFO 排队
        self._pending += 1
        self._observe_peak()
        try:
            while True:
                self._wake.clear()
                await self._wake.wait()
                if self._in_use < self._limit:
                    self._in_use += 1
                    return
        finally:
            self._pending -= 1

    def release(self) -> None:
        """释放一个并发槽位."""
        self._in_use = max(0, self._in_use - 1)
        if self._limit is not None:
            self._wake.set()

    def set_limit(self, new_limit: int) -> None:
        """动态调整并发上限.

        仅 limited 模式有效；monitor 模式调用抛 ValueError。
        """
        if self._limit is None:
            msg = "Cannot set limit on monitor-only slot"
            raise ValueError(msg)
        self._limit = new_limit
        self._wake.set()

    def _observe_peak(self) -> None:
        """记录当前 pending 值作为峰值采样点."""
        now = time.monotonic()
        self._peak_samples.append((now, self._pending))

    def _get_peak_pending_recent(self) -> int:
        """获取最近窗口内的 peak pending 值."""
        cutoff = time.monotonic() - _PEAK_WINDOW_SECONDS
        # 剔除过期采样
        while self._peak_samples and self._peak_samples[0][0] < cutoff:
            self._peak_samples.popleft()
        if not self._peak_samples:
            return 0
        return max(v for _, v in self._peak_samples)

    @property
    def limit(self) -> int | None:
        return self._limit

    @property
    def in_use(self) -> int:
        return self._in_use

    @property
    def available(self) -> int | None:
        if self._limit is None:
            return None
        return max(0, self._limit - self._in_use)

    @property
    def pending(self) -> int:
        return self._pending

    @property
    def peak_pending_recent(self) -> int:
        return self._get_peak_pending_recent()


class ModelConcurrencyController:
    """按模型名提供独立并发槽位的控制器.

    用法::

        # monitor 模式（默认）
        ctrl = ModelConcurrencyController(None)
        async with ctrl.track("model-a"):
            ...  # 执行请求

        # limited 模式（Zhipu 等）
        ctrl = ModelConcurrencyController(config)
        async with ctrl.track("glm-5v-turbo"):
            ...  # 满槽时排队等待
    """

    def __init__(self, config: ZhipuConcurrencyConfig | None) -> None:
        self._config = config
        self._slots: dict[str, _ConcurrencySlot] = {}

    @property
    def mode(self) -> Literal["monitor", "limited"]:
        """当前控制器模式."""
        return "limited" if self._config is not None else "monitor"

    def _get_or_create_slot(self, model: str) -> _ConcurrencySlot:
        """获取（或惰性创建）指定模型的并发槽位."""
        slot = self._slots.get(model)
        if slot is None:
            if self._config is not None:
                limit = self._config.get_limit(model)
            else:
                limit = None
            slot = _ConcurrencySlot(limit)
            self._slots[model] = slot
            if self._config is not None:
                logger.debug(
                    "ModelConcurrencyController: created slot mode=limited "
                    "model=%s limit=%d",
                    model,
                    limit,
                )
            else:
                logger.debug(
                    "ModelConcurrencyController: created slot mode=monitor model=%s",
                    model,
                )
        return slot

    @asynccontextmanager
    async def track(self, model: str):
        """异步上下文管理器：获取 → 执行 → 释放.

        用法::

            async with controller.track("glm-5v-turbo"):
                await vendor.send_message(...)
        """
        slot = self._get_or_create_slot(model)
        await slot.acquire()
        try:
            yield
        finally:
            slot.release()

    def set_limit(self, model: str, new_limit: int) -> None:
        """运行时修改指定模型的并发上限.

        仅 limited 模式支持；monitor 模式抛 ValueError。
        """
        if self._config is None:
            msg = f"vendor is monitor-only; cannot update limit for model '{model}'"
            raise ValueError(msg)
        slot = self._slots.get(model)
        if slot is None:
            slot = _ConcurrencySlot(new_limit)
            self._slots[model] = slot
        else:
            slot.set_limit(new_limit)
        self._config.models[model] = new_limit
        logger.info(
            "ModelConcurrencyController: updated limit model=%s new_limit=%d",
            model,
            new_limit,
        )

    def get_diagnostics(self) -> dict[str, dict[str, Any]]:
        """返回每个模型的并发状态快照（用于可观测性）."""
        snapshot: dict[str, dict[str, Any]] = {}
        mode = self.mode
        for model, slot in self._slots.items():
            entry: dict[str, Any] = {
                "mode": mode,
                "in_use": slot.in_use,
                "pending": slot.pending,
                "peak_pending_recent": slot.peak_pending_recent,
            }
            if mode == "limited":
                entry["limit"] = slot.limit
                entry["available"] = slot.available
            else:
                entry["limit"] = None
                entry["available"] = None
            snapshot[model] = entry
        return snapshot


# 向后兼容别名
ModelConcurrencyLimiter = ModelConcurrencyController

__all__ = ["ModelConcurrencyController", "ModelConcurrencyLimiter"]
