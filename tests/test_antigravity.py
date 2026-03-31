"""AntigravityBackend 和 GoogleOAuthTokenManager 单元测试."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from coding.proxy.backends.antigravity import AntigravityBackend, GoogleOAuthTokenManager
from coding.proxy.config.schema import AntigravityConfig, FailoverConfig


# --- GoogleOAuthTokenManager ---


@pytest.mark.asyncio
async def test_token_manager_refresh():
    """首次调用 get_token 触发 refresh."""
    tm = GoogleOAuthTokenManager("client_id", "client_secret", "refresh_tok")

    mock_response = MagicMock()
    mock_response.json.return_value = {"access_token": "goog_abc", "expires_in": 3600}
    mock_response.raise_for_status = MagicMock()

    mock_client = AsyncMock()
    mock_client.post.return_value = mock_response
    mock_client.is_closed = False
    tm._client = mock_client

    token = await tm.get_token()
    assert token == "goog_abc"
    mock_client.post.assert_awaited_once()

    # 验证请求参数
    call_kwargs = mock_client.post.call_args
    data = call_kwargs.kwargs.get("data", call_kwargs[1].get("data", {}))
    assert data["client_id"] == "client_id"
    assert data["client_secret"] == "client_secret"
    assert data["refresh_token"] == "refresh_tok"
    assert data["grant_type"] == "refresh_token"


@pytest.mark.asyncio
async def test_token_manager_caching():
    """重复调用不重复刷新（使用缓存）."""
    tm = GoogleOAuthTokenManager("cid", "csecret", "rtok")

    mock_response = MagicMock()
    mock_response.json.return_value = {"access_token": "cached", "expires_in": 3600}
    mock_response.raise_for_status = MagicMock()

    mock_client = AsyncMock()
    mock_client.post.return_value = mock_response
    mock_client.is_closed = False
    tm._client = mock_client

    token1 = await tm.get_token()
    token2 = await tm.get_token()
    assert token1 == token2 == "cached"
    assert mock_client.post.await_count == 1


@pytest.mark.asyncio
async def test_token_manager_refresh_on_expiry():
    """token 过期后重新刷新."""
    tm = GoogleOAuthTokenManager("cid", "csecret", "rtok")

    mock_response = MagicMock()
    mock_response.json.return_value = {"access_token": "v1", "expires_in": 3600}
    mock_response.raise_for_status = MagicMock()

    mock_client = AsyncMock()
    mock_client.post.return_value = mock_response
    mock_client.is_closed = False
    tm._client = mock_client

    await tm.get_token()

    # 模拟过期
    tm._expires_at = 0.0

    mock_response2 = MagicMock()
    mock_response2.json.return_value = {"access_token": "v2", "expires_in": 3600}
    mock_response2.raise_for_status = MagicMock()
    mock_client.post.return_value = mock_response2

    token2 = await tm.get_token()
    assert token2 == "v2"
    assert mock_client.post.await_count == 2


@pytest.mark.asyncio
async def test_token_manager_invalidate():
    """invalidate 后下次调用触发重新刷新."""
    tm = GoogleOAuthTokenManager("cid", "csecret", "rtok")

    mock_response = MagicMock()
    mock_response.json.return_value = {"access_token": "tok", "expires_in": 3600}
    mock_response.raise_for_status = MagicMock()

    mock_client = AsyncMock()
    mock_client.post.return_value = mock_response
    mock_client.is_closed = False
    tm._client = mock_client

    await tm.get_token()
    tm.invalidate()
    assert tm._expires_at == 0.0

    await tm.get_token()
    assert mock_client.post.await_count == 2


@pytest.mark.asyncio
async def test_token_manager_close():
    """close 关闭内部 HTTP 客户端."""
    tm = GoogleOAuthTokenManager("cid", "csecret", "rtok")
    mock_client = AsyncMock()
    mock_client.is_closed = False
    tm._client = mock_client

    await tm.close()
    mock_client.aclose.assert_awaited_once()


# --- AntigravityBackend ---


def test_get_name():
    config = AntigravityConfig()
    backend = AntigravityBackend(config, FailoverConfig())
    assert backend.get_name() == "antigravity"


@pytest.mark.asyncio
async def test_prepare_request_converts_and_injects_token():
    """_prepare_request 转换为 Gemini 格式并注入 OAuth token."""
    config = AntigravityConfig()
    backend = AntigravityBackend(config, FailoverConfig())
    backend._token_manager.get_token = AsyncMock(return_value="goog_token")

    body = {
        "model": "claude-sonnet-4-20250514",
        "messages": [{"role": "user", "content": "Hello"}],
        "max_tokens": 100,
    }
    headers = {"authorization": "Bearer original"}
    prepared_body, prepared_headers = await backend._prepare_request(body, headers)

    # 验证格式转换
    assert "contents" in prepared_body
    assert prepared_body["contents"][0]["parts"] == [{"text": "Hello"}]
    assert prepared_body["generationConfig"]["maxOutputTokens"] == 100

    # 验证 token 注入
    assert prepared_headers["authorization"] == "Bearer goog_token"
    assert prepared_headers["content-type"] == "application/json"


def test_on_error_status_invalidates_token():
    """401/403 触发 token 失效."""
    config = AntigravityConfig()
    backend = AntigravityBackend(config, FailoverConfig())

    # 设置一个有效的 expires_at
    backend._token_manager._expires_at = 999999999.0

    backend._on_error_status(401)
    assert backend._token_manager._expires_at == 0.0


def test_on_error_status_ignores_other_codes():
    """非 401/403 不触发 token 失效."""
    config = AntigravityConfig()
    backend = AntigravityBackend(config, FailoverConfig())
    backend._token_manager._expires_at = 999999999.0

    backend._on_error_status(429)
    assert backend._token_manager._expires_at == 999999999.0


def test_inherits_failover():
    """继承基类 failover 判断."""
    failover = FailoverConfig(status_codes=[429, 503], error_types=["rate_limit_error"])
    config = AntigravityConfig()
    backend = AntigravityBackend(config, failover)

    assert backend.should_trigger_failover(429, None)
    assert not backend.should_trigger_failover(200, None)
    assert backend.should_trigger_failover(429, {
        "error": {"type": "rate_limit_error", "message": "limited"}
    })


def test_model_endpoint_in_config():
    """model_endpoint 可配置."""
    config = AntigravityConfig(model_endpoint="models/claude-opus-4-20250514")
    backend = AntigravityBackend(config, FailoverConfig())
    assert backend._model_endpoint == "models/claude-opus-4-20250514"
