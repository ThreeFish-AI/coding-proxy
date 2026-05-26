"""每模型并发限制器 — 基于 asyncio.Semaphore 的公平排队.

为每个映射后的模型（如 ``glm-5v-turbo``）独立维护一个 ``asyncio.Semaphore``，
确保同一时间点该模型的并行请求数不超过配置的上限。当所有槽位被占满时，
新请求按 FIFO 顺序排队等待，直到有槽位释放。

设计要点：
  - **惰性创建**：仅在首次请求到达时才为该模型创建 Semaphore，避免冷启动开销
  - **FIFO 公平**：``asyncio.Semaphore`` 内部使用 FIFO 队列，天然满足排队语义
  - **按映射后模型名键控**：与上游真实承载能力对齐，而非按客户端请求名（如 ``claude-sonnet-*``）
"""

from __future__ import annotations

import asyncio
import logging

from ..config.vendors import ZhipuConcurrencyConfig

logger = logging.getLogger(__name__)


class ModelConcurrencyLimiter:
    """按模型名提供独立并发槽位的限制器.

    用法::

        limiter = ModelConcurrencyLimiter(config)
        sem = await limiter.acquire("glm-5v-turbo")
        try:
            ...  # 执行请求
        finally:
            sem.release()
    """

    def __init__(self, config: ZhipuConcurrencyConfig) -> None:
        self._config = config
        self._semaphores: dict[str, asyncio.Semaphore] = {}

    def _get_semaphore(self, model: str) -> asyncio.Semaphore:
        """获取（或惰性创建）指定模型的信号量."""
        sem = self._semaphores.get(model)
        if sem is None:
            limit = self._config.get_limit(model)
            sem = asyncio.Semaphore(limit)
            self._semaphores[model] = sem
            logger.debug(
                "ModelConcurrencyLimiter: created semaphore model=%s limit=%d",
                model,
                limit,
            )
        return sem

    async def acquire(self, model: str) -> asyncio.Semaphore:
        """获取指定模型的并发槽位，必要时阻塞排队.

        返回已获取的 Semaphore 实例，调用方负责在请求完成后调用 ``release()``。
        """
        sem = self._get_semaphore(model)
        await sem.acquire()
        return sem

    def get_diagnostics(self) -> dict[str, dict[str, int]]:
        """返回每个模型的并发状态快照（用于可观测性）."""
        snapshot: dict[str, dict[str, int]] = {}
        for model, sem in self._semaphores.items():
            limit = self._config.get_limit(model)
            # asyncio.Semaphore 内部 _value 表示剩余可用槽位
            available = sem._value  # noqa: SLF001 — 公开 API 未暴露
            in_use = max(limit - available, 0)
            # _waiters 为正在排队等待的协程集合，无等待者时为 None
            waiters = getattr(sem, "_waiters", None)  # noqa: SLF001
            pending = len(waiters) if waiters else 0
            snapshot[model] = {
                "limit": limit,
                "in_use": in_use,
                "available": max(available, 0),
                "pending": pending,
            }
        return snapshot


__all__ = ["ModelConcurrencyLimiter"]
