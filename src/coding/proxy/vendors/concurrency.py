"""每模型并发限制器 — 支持运行时动态调整的公平排队.

为每个映射后的模型（如 ``glm-5v-turbo``）独立维护一个 ``_ConcurrencySlot`，
确保同一时间点该模型的并行请求数不超过配置的上限。当所有槽位被占满时，
新请求按 FIFO 顺序排队等待，直到有槽位释放。

设计要点：
  - **惰性创建**：仅在首次请求到达时才为该模型创建 Slot，避免冷启动开销
  - **FIFO 公平**：``asyncio.Event`` + while 循环天然满足 FIFO 排队语义
  - **动态调整**：支持运行时修改 per-model limit，无需重启进程
  - **按映射后模型名键控**：与上游真实承载能力对齐，而非按客户端请求名
"""

from __future__ import annotations

import asyncio
import logging

from ..config.vendors import ZhipuConcurrencyConfig

logger = logging.getLogger(__name__)


class _ConcurrencySlot:
    """支持动态 limit 的并发槽位.

    使用 ``asyncio.Event`` 作为等待/通知原语，在 ``acquire`` 中 await 等待，
    在 ``release`` / ``set_limit`` 中唤醒。``set_limit`` 修改上限后立即唤醒
    所有等待者，由它们重新判断是否可获得槽位。
    """

    def __init__(self, limit: int) -> None:
        self._limit = limit
        self._in_use: int = 0
        self._pending: int = 0
        self._wake = asyncio.Event()
        self._wake.set()

    async def acquire(self) -> _ConcurrencySlot:
        """获取一个并发槽位，必要时阻塞排队.

        返回 ``self``，调用方在请求完成后调用 ``release()``。
        """
        # Fast path
        if self._in_use < self._limit:
            self._in_use += 1
            return self
        # Slow path — 等待槽位释放
        self._pending += 1
        try:
            while True:
                self._wake.clear()
                await self._wake.wait()
                if self._in_use < self._limit:
                    self._in_use += 1
                    return self
        finally:
            self._pending -= 1

    def release(self) -> None:
        """释放一个并发槽位."""
        self._in_use = max(0, self._in_use - 1)
        self._wake.set()

    def set_limit(self, new_limit: int) -> None:
        """动态调整并发上限.

        增大 limit 时立即唤醒等待者；缩小时已持有的槽位不受影响，
        新 limit 在后续 acquire 中自然生效。
        """
        self._limit = new_limit
        self._wake.set()

    @property
    def limit(self) -> int:
        return self._limit

    @property
    def in_use(self) -> int:
        return self._in_use

    @property
    def available(self) -> int:
        return max(0, self._limit - self._in_use)

    @property
    def pending(self) -> int:
        return self._pending


class ModelConcurrencyLimiter:
    """按模型名提供独立并发槽位的限制器.

    用法::

        limiter = ModelConcurrencyLimiter(config)
        slot = await limiter.acquire("glm-5v-turbo")
        try:
            ...  # 执行请求
        finally:
            slot.release()
    """

    def __init__(self, config: ZhipuConcurrencyConfig) -> None:
        self._config = config
        self._slots: dict[str, _ConcurrencySlot] = {}

    def _get_or_create_slot(self, model: str) -> _ConcurrencySlot:
        """获取（或惰性创建）指定模型的并发槽位."""
        slot = self._slots.get(model)
        if slot is None:
            limit = self._config.get_limit(model)
            slot = _ConcurrencySlot(limit)
            self._slots[model] = slot
            logger.debug(
                "ModelConcurrencyLimiter: created slot model=%s limit=%d",
                model,
                limit,
            )
        return slot

    async def acquire(self, model: str) -> _ConcurrencySlot:
        """获取指定模型的并发槽位，必要时阻塞排队.

        返回已获取的 Slot 实例，调用方负责在请求完成后调用 ``release()``。
        """
        slot = self._get_or_create_slot(model)
        await slot.acquire()
        return slot

    def set_limit(self, model: str, new_limit: int) -> None:
        """运行时修改指定模型的并发上限.

        同时更新 config.models 以确保后续惰性创建使用新值。
        """
        slot = self._slots.get(model)
        if slot is None:
            slot = _ConcurrencySlot(new_limit)
            self._slots[model] = slot
        else:
            slot.set_limit(new_limit)
        self._config.models[model] = new_limit
        logger.info(
            "ModelConcurrencyLimiter: updated limit model=%s new_limit=%d",
            model,
            new_limit,
        )

    def get_diagnostics(self) -> dict[str, dict[str, int]]:
        """返回每个模型的并发状态快照（用于可观测性）."""
        snapshot: dict[str, dict[str, int]] = {}
        for model, slot in self._slots.items():
            snapshot[model] = {
                "limit": slot.limit,
                "in_use": slot.in_use,
                "available": slot.available,
                "pending": slot.pending,
            }
        return snapshot


__all__ = ["ModelConcurrencyLimiter"]
