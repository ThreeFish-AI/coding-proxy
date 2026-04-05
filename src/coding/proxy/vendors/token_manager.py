"""Token Manager 抽象基类 — DCL 缓存机制提取.

类型定义（``TokenErrorKind`` / ``TokenAcquireError`` / ``TokenManagerDiagnostics``）
已迁移至 :mod:`coding.proxy.model.token`。本文件保留 ``BaseTokenManager``
抽象基类，类型通过 re-export 提供。

.. deprecated::
    未来版本将移除类型 re-export，请直接从 :mod:`coding.proxy.model.token` 导入。
"""

from __future__ import annotations

import asyncio
import logging
import time
from abc import ABC, abstractmethod

import httpx

# noqa: F401
from ..model.token import (
    TokenAcquireError,
    TokenErrorKind,
    TokenManagerDiagnostics,
)

logger = logging.getLogger(__name__)


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
        self._diagnostics: TokenManagerDiagnostics = TokenManagerDiagnostics()

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
            try:
                token, expires_in = await self._acquire()
            except TokenAcquireError as exc:
                self._record_error(exc)
                raise
            except Exception as exc:
                wrapped = TokenAcquireError(f"Token 获取异常: {exc}")
                self._record_error(wrapped)
                raise wrapped from exc
            self._access_token = token
            self._expires_at = time.monotonic() + expires_in - self._REFRESH_MARGIN
            self._clear_error()
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

    def get_diagnostics(self) -> dict[str, str | bool]:
        return self._diagnostics.to_dict()

    def mark_error(
        self,
        message: str,
        *,
        kind: TokenErrorKind = TokenErrorKind.TEMPORARY,
        needs_reauth: bool = False,
    ) -> None:
        self._record_error(
            TokenAcquireError.with_kind(
                message,
                kind=kind,
                needs_reauth=needs_reauth,
            )
        )

    def clear_diagnostics(self) -> None:
        self._clear_error()

    def _record_error(self, exc: TokenAcquireError) -> None:
        self._diagnostics = TokenManagerDiagnostics(
            last_error=str(exc),
            needs_reauth=exc.needs_reauth,
            error_kind=exc.kind.value,
            updated_at=time.time(),
        )
        logger.warning("Token acquire failed: %s", exc)

    def _clear_error(self) -> None:
        self._diagnostics = TokenManagerDiagnostics()

    async def close(self) -> None:
        if self._client and not self._client.is_closed:
            await self._client.aclose()
