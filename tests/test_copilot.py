"""CopilotTokenManager 和 CopilotBackend 单元测试."""

from __future__ import annotations

import json
import time
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from coding.proxy.backends.copilot import CopilotBackend, CopilotTokenManager
from coding.proxy.backends.token_manager import TokenAcquireError
from coding.proxy.config.schema import CopilotConfig, FailoverConfig


# --- CopilotTokenManager ---


@pytest.mark.asyncio
async def test_token_manager_exchange():
    """首次调用 get_token 触发 token 交换."""
    tm = CopilotTokenManager("ghp_test", "https://github.com/github-copilot/chat/token")

    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = {"access_token": "cop_abc", "expires_in": 1800}
    mock_response.raise_for_status = MagicMock()

    mock_client = AsyncMock()
    mock_client.get.return_value = mock_response
    mock_client.is_closed = False
    tm._client = mock_client

    token = await tm.get_token()
    assert token == "cop_abc"
    mock_client.get.assert_awaited_once()

    # 验证请求头包含 GitHub PAT
    call_kwargs = mock_client.get.call_args
    headers = call_kwargs.kwargs.get("headers", call_kwargs[1].get("headers", {}))
    assert headers["authorization"] == "token ghp_test"


@pytest.mark.asyncio
async def test_token_manager_caching():
    """重复调用不重复交换（使用缓存）."""
    tm = CopilotTokenManager("ghp_test", "https://fake")

    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = {"access_token": "cached_token", "expires_in": 1800}
    mock_response.raise_for_status = MagicMock()

    mock_client = AsyncMock()
    mock_client.get.return_value = mock_response
    mock_client.is_closed = False
    tm._client = mock_client

    token1 = await tm.get_token()
    token2 = await tm.get_token()
    assert token1 == token2 == "cached_token"
    # 仅调用一次交换
    assert mock_client.get.await_count == 1


@pytest.mark.asyncio
async def test_token_manager_refresh_on_expiry():
    """token 过期后重新交换."""
    tm = CopilotTokenManager("ghp_test", "https://fake")

    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = {"access_token": "token_v1", "expires_in": 1800}
    mock_response.raise_for_status = MagicMock()

    mock_client = AsyncMock()
    mock_client.get.return_value = mock_response
    mock_client.is_closed = False
    tm._client = mock_client

    token1 = await tm.get_token()
    assert token1 == "token_v1"

    # 模拟过期
    tm._expires_at = 0.0

    mock_response2 = MagicMock()
    mock_response2.status_code = 200
    mock_response2.json.return_value = {"access_token": "token_v2", "expires_in": 1800}
    mock_response2.raise_for_status = MagicMock()
    mock_client.get.return_value = mock_response2

    token2 = await tm.get_token()
    assert token2 == "token_v2"
    assert mock_client.get.await_count == 2


@pytest.mark.asyncio
async def test_token_manager_invalidate():
    """invalidate 后下次调用触发重新交换."""
    tm = CopilotTokenManager("ghp_test", "https://fake")

    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = {"access_token": "tok", "expires_in": 1800}
    mock_response.raise_for_status = MagicMock()

    mock_client = AsyncMock()
    mock_client.get.return_value = mock_response
    mock_client.is_closed = False
    tm._client = mock_client

    await tm.get_token()
    tm.invalidate()
    assert tm._expires_at == 0.0

    await tm.get_token()
    assert mock_client.get.await_count == 2


@pytest.mark.asyncio
async def test_token_manager_close():
    """close 关闭内部 HTTP 客户端."""
    tm = CopilotTokenManager("ghp_test", "https://fake")
    mock_client = AsyncMock()
    mock_client.is_closed = False
    tm._client = mock_client

    await tm.close()
    mock_client.aclose.assert_awaited_once()


@pytest.mark.asyncio
async def test_token_manager_missing_access_token_raises_token_acquire_error():
    """响应缺少 access_token 时抛 TokenAcquireError，而非 KeyError."""
    tm = CopilotTokenManager("ghp_test", "https://fake")

    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = {"message": "license check required"}

    mock_client = AsyncMock()
    mock_client.get.return_value = mock_response
    mock_client.is_closed = False
    tm._client = mock_client

    with pytest.raises(TokenAcquireError) as exc_info:
        await tm.get_token()

    assert "非预期响应" in str(exc_info.value)
    assert "license check required" in str(exc_info.value)
    assert tm.get_diagnostics()["last_error"] == str(exc_info.value)


@pytest.mark.asyncio
async def test_token_manager_401_needs_reauth():
    """401 交换失败应触发 needs_reauth."""
    tm = CopilotTokenManager("ghp_test", "https://fake")

    mock_response = MagicMock()
    mock_response.status_code = 401
    mock_response.json.return_value = {"message": "Bad credentials"}

    mock_client = AsyncMock()
    mock_client.get.return_value = mock_response
    mock_client.is_closed = False
    tm._client = mock_client

    with pytest.raises(TokenAcquireError) as exc_info:
        await tm.get_token()

    assert exc_info.value.needs_reauth is True
    assert "GitHub token 无效或已过期" in str(exc_info.value)


# --- CopilotBackend ---


def test_copilot_get_name():
    config = CopilotConfig(github_token="ghp_test")
    backend = CopilotBackend(config, FailoverConfig())
    assert backend.get_name() == "copilot"


@pytest.mark.asyncio
async def test_copilot_prepare_request_filters_and_injects_token():
    """_prepare_request 过滤 hop-by-hop 头并注入 Copilot token."""
    config = CopilotConfig(github_token="ghp_test")
    backend = CopilotBackend(config, FailoverConfig())

    # Mock token manager
    backend._token_manager.get_token = AsyncMock(return_value="cop_injected")

    body = {"model": "claude-sonnet-4-20250514", "messages": []}
    headers = {
        "authorization": "Bearer original",
        "host": "localhost",
        "content-length": "100",
        "anthropic-version": "2023-06-01",
    }
    prepared_body, prepared_headers = await backend._prepare_request(body, headers)
    assert prepared_body is body  # 透传
    assert "host" not in prepared_headers
    assert "content-length" not in prepared_headers
    assert prepared_headers["authorization"] == "Bearer cop_injected"
    assert prepared_headers["anthropic-version"] == "2023-06-01"


def test_copilot_inherits_failover():
    """Copilot 继承基类 failover 判断."""
    failover = FailoverConfig(status_codes=[429, 503], error_types=["rate_limit_error"])
    config = CopilotConfig(github_token="ghp_test")
    backend = CopilotBackend(config, failover)

    assert backend.should_trigger_failover(429, None)
    assert not backend.should_trigger_failover(200, None)
    assert backend.should_trigger_failover(429, {
        "error": {"type": "rate_limit_error", "message": "limited"}
    })
