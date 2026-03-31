"""RuntimeReauthCoordinator 单元测试."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from coding.proxy.auth.runtime import ReauthState, RuntimeReauthCoordinator
from coding.proxy.auth.store import ProviderTokens, TokenStoreManager


def _make_store() -> TokenStoreManager:
    """构造一个不写磁盘的 TokenStoreManager."""
    store = TokenStoreManager()
    store._data = {}
    store.save = MagicMock()  # 阻止写磁盘
    return store


def _make_mock_provider(name: str, tokens: ProviderTokens | None = None):
    """构造 Mock OAuthProvider."""
    prov = AsyncMock()
    prov.get_name.return_value = name
    prov.login.return_value = tokens or ProviderTokens(
        access_token="new_access",
        refresh_token="new_refresh",
    )
    return prov


# --- 基础功能 ---


@pytest.mark.asyncio
async def test_request_reauth_success():
    """成功的重认证流程: login → store.set → updater."""
    store = _make_store()
    mock_provider = _make_mock_provider("github")
    updater = MagicMock()

    coordinator = RuntimeReauthCoordinator(
        token_store=store,
        providers={"github": mock_provider},
        token_updaters={"github": updater},
    )

    await coordinator.request_reauth("github")
    # 等待后台任务完成
    await asyncio.sleep(0.1)

    mock_provider.login.assert_awaited_once()
    store.save.assert_called()
    updater.assert_called_once_with("new_access")
    assert coordinator._states["github"] == ReauthState.COMPLETED


@pytest.mark.asyncio
async def test_request_reauth_google():
    """Google 重认证使用 refresh_token 更新."""
    store = _make_store()
    mock_provider = _make_mock_provider("google", ProviderTokens(
        access_token="goog_access",
        refresh_token="goog_refresh",
    ))
    updater = MagicMock()

    coordinator = RuntimeReauthCoordinator(
        token_store=store,
        providers={"google": mock_provider},
        token_updaters={"google": updater},
    )

    await coordinator.request_reauth("google")
    await asyncio.sleep(0.1)

    updater.assert_called_once_with("goog_refresh")
    assert coordinator._states["google"] == ReauthState.COMPLETED


@pytest.mark.asyncio
async def test_request_reauth_failure():
    """login 失败 → 状态变为 FAILED."""
    store = _make_store()
    mock_provider = AsyncMock()
    mock_provider.login.side_effect = RuntimeError("auth failed")

    coordinator = RuntimeReauthCoordinator(
        token_store=store,
        providers={"github": mock_provider},
        token_updaters={"github": MagicMock()},
    )

    await coordinator.request_reauth("github")
    await asyncio.sleep(0.1)

    assert coordinator._states["github"] == ReauthState.FAILED
    assert "auth failed" in coordinator._last_error["github"]


@pytest.mark.asyncio
async def test_request_reauth_unknown_provider():
    """未知 provider 不报错."""
    store = _make_store()
    coordinator = RuntimeReauthCoordinator(
        token_store=store, providers={}, token_updaters={},
    )
    # 不应抛异常
    await coordinator.request_reauth("unknown")


@pytest.mark.asyncio
async def test_idempotent_pending():
    """PENDING 状态下重复调用不并发触发."""
    store = _make_store()
    call_count = 0

    async def _slow_login():
        nonlocal call_count
        call_count += 1
        await asyncio.sleep(0.2)
        return ProviderTokens(access_token="tok", refresh_token="ref")

    mock_provider = AsyncMock()
    mock_provider.login = _slow_login

    coordinator = RuntimeReauthCoordinator(
        token_store=store,
        providers={"github": mock_provider},
        token_updaters={"github": MagicMock()},
    )

    # 并发请求
    await coordinator.request_reauth("github")
    await asyncio.sleep(0.01)
    await coordinator.request_reauth("github")  # 应被跳过
    await asyncio.sleep(0.3)

    assert call_count == 1


# --- get_status ---


def test_get_status_idle():
    store = _make_store()
    coordinator = RuntimeReauthCoordinator(
        token_store=store,
        providers={"github": AsyncMock(), "google": AsyncMock()},
        token_updaters={},
    )
    status = coordinator.get_status()
    assert status["github"]["state"] == "idle"
    assert status["google"]["state"] == "idle"


@pytest.mark.asyncio
async def test_get_status_after_failure():
    store = _make_store()
    mock_provider = AsyncMock()
    mock_provider.login.side_effect = RuntimeError("boom")

    coordinator = RuntimeReauthCoordinator(
        token_store=store,
        providers={"github": mock_provider},
        token_updaters={"github": MagicMock()},
    )

    await coordinator.request_reauth("github")
    await asyncio.sleep(0.1)

    status = coordinator.get_status()
    assert status["github"]["state"] == "failed"
    assert "boom" in status["github"]["error"]


@pytest.mark.asyncio
async def test_get_status_after_success():
    store = _make_store()
    mock_provider = _make_mock_provider("github")

    coordinator = RuntimeReauthCoordinator(
        token_store=store,
        providers={"github": mock_provider},
        token_updaters={"github": MagicMock()},
    )

    await coordinator.request_reauth("github")
    await asyncio.sleep(0.1)

    status = coordinator.get_status()
    assert status["github"]["state"] == "completed"
    assert "completed_ago_seconds" in status["github"]
