"""Token Manager 抽象基类 — DCL 缓存机制提取."""

from __future__ import annotations

import asyncio
import logging
import time
from abc import ABC, abstractmethod

import httpx

logger = logging.getLogger(__name__)


class TokenAcquireError(Exception):
    """Token 获取失败.

    needs_reauth=True 表示长期凭证已失效，需要重新执行浏览器 OAuth 登录。
    needs_reauth=False 表示临时性故障（网络超时等），可自动恢复。
    """

    def __init__(self, message: str, *, needs_reauth: bool = False) -> None:
        super().__init__(message)
        self.needs_reauth = needs_reauth


class BaseTokenManager(ABC):
    """Token 缓存与自动刷新的通用机制.

    子类只需实现 ``_acquire()`` 返回 ``(access_token, expires_in_seconds)``。

    机制层提供:
    - Double-Check Locking (DCL) 并发安全
    - 惰性 httpx 客户端创建
    - ``get_token()`` / ``invalidate()`` / ``close()`` 标准生命周期
    """

    _REFRESH_MARGIN: int = 60  # 提前刷新余量（秒），子类可覆写

    def __init__(self) -> None:
        self._access_token: str | None = None
        self._expires_at: float = 0.0
        self._lock = asyncio.Lock()
        self._client: httpx.AsyncClient | None = None

    def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(timeout=httpx.Timeout(30.0))
        return self._client

    async def get_token(self) -> str:
        """获取有效 token（带缓存和自动刷新）.

        Raises:
            TokenAcquireError: 获取失败
        """
        if self._access_token and time.monotonic() < self._expires_at:
            return self._access_token

        async with self._lock:
            # Double-check after acquiring lock
            if self._access_token and time.monotonic() < self._expires_at:
                return self._access_token
            token, expires_in = await self._acquire()
            self._access_token = token
            self._expires_at = time.monotonic() + expires_in - self._REFRESH_MARGIN
            return self._access_token

    @abstractmethod
    async def _acquire(self) -> tuple[str, float]:
        """获取新 token.

        Returns:
            (access_token, expires_in_seconds) 元组

        Raises:
            TokenAcquireError: 获取失败，needs_reauth=True 表示需要重新登录
        """

    def invalidate(self) -> None:
        """标记当前 token 失效（触发下次请求时被动刷新）."""
        self._expires_at = 0.0

    async def close(self) -> None:
        if self._client and not self._client.is_closed:
            await self._client.aclose()
