"""BaseTokenManager DCL 缓存机制单元测试."""

from __future__ import annotations

import asyncio
import time
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from coding.proxy.vendors.token_manager import BaseTokenManager, TokenAcquireError


class _StubTokenManager(BaseTokenManager):
    """测试用 TokenManager 桩实现."""

    def __init__(self, results: list[tuple[str, float]] | None = None) -> None:
        super().__init__()
        self._results = results or [("token_1", 1800.0)]
        self._call_count = 0

    async def _acquire(self) -> tuple[str, float]:
        idx = min(self._call_count, len(self._results) - 1)
        self._call_count += 1
        return self._results[idx]


class _FailingTokenManager(BaseTokenManager):
    """测试用: _acquire 始终失败."""

    def __init__(self, *, needs_reauth: bool = False) -> None:
        super().__init__()
        self._needs_reauth = needs_reauth

    async def _acquire(self) -> tuple[str, float]:
        raise TokenAcquireError("模拟失败", needs_reauth=self._needs_reauth)


class _UnexpectedFailingTokenManager(BaseTokenManager):
    """测试用: _acquire 抛出非 TokenAcquireError."""

    async def _acquire(self) -> tuple[str, float]:
        raise KeyError("access_token")


# --- TokenAcquireError ---


def test_token_acquire_error_default():
    err = TokenAcquireError("test")
    assert str(err) == "test"
    assert err.needs_reauth is False


def test_token_acquire_error_needs_reauth():
    err = TokenAcquireError("expired", needs_reauth=True)
    assert err.needs_reauth is True


# --- BaseTokenManager: DCL 缓存 ---


@pytest.mark.asyncio
async def test_get_token_first_call():
    """首次调用触发 _acquire."""
    tm = _StubTokenManager([("abc", 1800.0)])
    token = await tm.get_token()
    assert token == "abc"
    assert tm._call_count == 1


@pytest.mark.asyncio
async def test_get_token_caching():
    """重复调用使用缓存，不重复 _acquire."""
    tm = _StubTokenManager([("cached", 3600.0)])
    t1 = await tm.get_token()
    t2 = await tm.get_token()
    assert t1 == t2 == "cached"
    assert tm._call_count == 1


@pytest.mark.asyncio
async def test_get_token_refresh_on_expiry():
    """token 过期后重新 _acquire."""
    tm = _StubTokenManager([("v1", 1800.0), ("v2", 1800.0)])
    t1 = await tm.get_token()
    assert t1 == "v1"

    # 模拟过期
    tm._expires_at = 0.0
    t2 = await tm.get_token()
    assert t2 == "v2"
    assert tm._call_count == 2


@pytest.mark.asyncio
async def test_invalidate():
    """invalidate 清除过期时间."""
    tm = _StubTokenManager([("tok", 1800.0)])
    await tm.get_token()
    assert tm._expires_at > 0

    tm.invalidate()
    assert tm._expires_at == 0.0


@pytest.mark.asyncio
async def test_acquire_error_propagation():
    """_acquire 抛出 TokenAcquireError 正常传播."""
    tm = _FailingTokenManager(needs_reauth=True)
    with pytest.raises(TokenAcquireError) as exc_info:
        await tm.get_token()
    assert exc_info.value.needs_reauth is True


@pytest.mark.asyncio
async def test_acquire_unexpected_error_wrapped():
    """_acquire 抛出意外异常时统一包装为 TokenAcquireError."""
    tm = _UnexpectedFailingTokenManager()
    with pytest.raises(TokenAcquireError) as exc_info:
        await tm.get_token()
    assert "Token 获取异常" in str(exc_info.value)
    assert exc_info.value.needs_reauth is False
    assert tm.get_diagnostics()["last_error"].startswith("Token 获取异常")


@pytest.mark.asyncio
async def test_concurrent_safety():
    """并发调用只触发一次 _acquire（DCL 保证）."""
    call_count = 0

    class _SlowManager(BaseTokenManager):
        async def _acquire(self) -> tuple[str, float]:
            nonlocal call_count
            call_count += 1
            await asyncio.sleep(0.05)
            return ("concurrent_tok", 3600.0)

    tm = _SlowManager()
    results = await asyncio.gather(
        tm.get_token(), tm.get_token(), tm.get_token(),
    )
    assert all(r == "concurrent_tok" for r in results)
    assert call_count == 1


# --- 客户端管理 ---


@pytest.mark.asyncio
async def test_get_client_lazy_creation():
    """_get_client 惰性创建 httpx 客户端."""
    tm = _StubTokenManager()
    assert tm._client is None
    client = tm._get_client()
    assert client is not None
    assert isinstance(client, httpx.AsyncClient)
    await tm.close()


@pytest.mark.asyncio
async def test_close():
    """close 关闭内部客户端."""
    tm = _StubTokenManager()
    _ = tm._get_client()
    await tm.close()
    assert tm._client.is_closed


@pytest.mark.asyncio
async def test_refresh_margin():
    """验证 _REFRESH_MARGIN 提前刷新."""
    class _MarginManager(BaseTokenManager):
        _REFRESH_MARGIN = 500

        async def _acquire(self) -> tuple[str, float]:
            return ("tok", 600.0)  # expires_in=600, margin=500 → 有效期仅 100s

    tm = _MarginManager()
    await tm.get_token()
    # expires_at 应该约等于 now + 100
    expected = time.monotonic() + 100
    assert abs(tm._expires_at - expected) < 2.0
