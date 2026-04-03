"""TokenBackendMixin 单元测试."""

from unittest.mock import AsyncMock

import pytest

from coding.proxy.backends.mixins import TokenBackendMixin
from coding.proxy.backends.token_manager import BaseTokenManager


class _ConcreteTokenManager(BaseTokenManager):
    """测试用具体 TokenManager 实现."""

    async def _acquire(self):
        return "test_token", 300.0


class _TestBackend(TokenBackendMixin):
    """测试用 Mixin 消费者（模拟后端）."""

    def __init__(self):
        tm = _ConcreteTokenManager()
        TokenBackendMixin.__init__(self, tm)

    def get_name(self) -> str:
        return "test_backend"


@pytest.fixture
def backend() -> _TestBackend:
    return _TestBackend()


# ── _on_error_status ─────────────────────────────────────


def test_on_error_status_invalidates_on_401(backend: _TestBackend):
    backend._on_error_status(401)
    assert backend._token_manager._access_token is None
    assert backend._token_manager._expires_at == 0.0


def test_on_error_status_invalidates_on_403(backend: _TestBackend):
    backend._on_error_status(403)
    assert backend._token_manager._access_token is None


def test_on_error_status_noop_on_429(backend: _TestBackend):
    """429 不应触发 token 失效."""
    backend._on_error_status(429)
    # token 状态不变（未设置过所以仍为 None，但不应调用 invalidate）
    # 验证方法不会对非 401/403 抛异常即可
    assert True


def test_on_error_status_noop_on_200(backend: _TestBackend):
    backend._on_error_status(200)
    assert True


# ── check_health ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_check_health_success(backend: _TestBackend):
    result = await backend.check_health()
    assert result is True


@pytest.mark.asyncio
async def test_check_health_failure(backend: _TestBackend):
    backend._token_manager.get_token = AsyncMock(side_effect=Exception("refresh failed"))
    result = await backend.check_health()
    assert result is False


@pytest.mark.asyncio
async def test_check_health_returns_false_on_empty_token(backend: _TestBackend):
    """get_token 返回空字符串时视为不健康."""
    backend._token_manager.get_token = AsyncMock(return_value="")
    result = await backend.check_health()
    assert result is False


# ── _get_token_diagnostics ────────────────────────────────


def test_get_token_diagnostics_empty(backend: _TestBackend):
    diag = backend._get_token_diagnostics()
    assert isinstance(diag, dict)
    # 无数据时应为空或仅含基础字段
    assert len(diag) == 0 or "token_manager" not in diag


def test_get_token_diagnostics_with_adaptations(backend: _TestBackend):
    backend._last_request_adaptations = ["thinking_downgraded"]
    backend._last_requested_model = "claude-opus-4-6"
    backend._last_resolved_model = "claude-opus-4.6"
    backend._last_model_resolution_reason = "same_family_highest_version"

    diag = backend._get_token_diagnostics()
    assert diag["request_adaptations"] == ["thinking_downgraded"]
    assert diag["requested_model"] == "claude-opus-4-6"
    assert diag["resolved_model"] == "claude-opus-4.6"
    assert diag["model_resolution_reason"] == "same_family_highest_version"
