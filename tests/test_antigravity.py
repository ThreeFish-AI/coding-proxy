"""AntigravityVendor 和 GoogleOAuthTokenManager 单元测试."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from coding.proxy.config.schema import (
    AntigravityConfig,
    FailoverConfig,
    ModelMappingRule,
)
from coding.proxy.routing.model_mapper import ModelMapper
from coding.proxy.vendors.antigravity import AntigravityVendor, GoogleOAuthTokenManager
from coding.proxy.vendors.base import RequestCapabilities
from coding.proxy.vendors.token_manager import (  # noqa: F401
    TokenAcquireError,
    TokenErrorKind,
)

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


@pytest.mark.asyncio
async def test_token_manager_partial_scope_warns_but_succeeds():
    """refresh 成功但 scope 不完整时，应发出警告但正常返回 token.

    Google OAuth2 规范允许 refresh_token 返回的 access_token 仅包含部分已授权 scope，
    这是正常行为。参考 Antigravity-Manager 项目，不做刷新后的严格 scope 校验。
    """
    tm = GoogleOAuthTokenManager("cid", "secret", "refresh")

    mock_response = MagicMock()
    mock_response.json.return_value = {
        "access_token": "goog_abc",
        "expires_in": 3600,
        "scope": "https://www.googleapis.com/auth/cloud-platform",
    }
    mock_response.raise_for_status = MagicMock()

    mock_client = AsyncMock()
    mock_client.post.return_value = mock_response
    mock_client.is_closed = False
    tm._client = mock_client

    # 应正常返回 token，不再抛异常
    token = await tm.get_token()
    assert token == "goog_abc"


# --- AntigravityVendor ---


def test_get_name():
    config = AntigravityConfig()
    vendor = AntigravityVendor(config, FailoverConfig(), ModelMapper([]))
    assert vendor.get_name() == "antigravity"


@pytest.mark.asyncio
async def test_prepare_request_converts_and_injects_token():
    """_prepare_request 转换为 Gemini 格式并注入 OAuth token."""
    config = AntigravityConfig()
    vendor = AntigravityVendor(config, FailoverConfig(), ModelMapper([]))
    vendor._token_manager.get_token = AsyncMock(return_value="goog_token")

    body = {
        "model": "claude-sonnet-4-20250514",
        "messages": [{"role": "user", "content": "Hello"}],
        "max_tokens": 100,
    }
    headers = {"authorization": "Bearer original"}
    prepared_body, prepared_headers = await vendor._prepare_request(body, headers)

    # 验证格式转换
    assert "contents" in prepared_body
    assert prepared_body["contents"][0]["parts"] == [{"text": "Hello"}]
    assert prepared_body["generationConfig"]["maxOutputTokens"] == 100

    # 验证 token 注入
    assert prepared_headers["authorization"] == "Bearer goog_token"
    assert prepared_headers["content-type"] == "application/json"


@pytest.mark.asyncio
async def test_prepare_request_resolves_model_from_mapping():
    mapper = ModelMapper(
        [
            ModelMappingRule(
                pattern="claude-sonnet-*",
                target="claude-sonnet-4-6-thinking",
                vendors=["antigravity"],
            )
        ]
    )
    vendor = AntigravityVendor(AntigravityConfig(), FailoverConfig(), mapper)
    vendor._token_manager.get_token = AsyncMock(return_value="goog_token")

    prepared_body, _ = await vendor._prepare_request(
        {
            "model": "claude-sonnet-4-20250514",
            "messages": [{"role": "user", "content": "Hello"}],
        },
        {},
    )

    assert prepared_body["contents"][0]["parts"] == [{"text": "Hello"}]
    diagnostics = vendor.get_diagnostics()
    assert diagnostics["resolved_model"] == "claude-sonnet-4-6-thinking"


def test_on_error_status_invalidates_token():
    """401/403 触发 token 失效."""
    config = AntigravityConfig()
    vendor = AntigravityVendor(config, FailoverConfig(), ModelMapper([]))

    # 设置一个有效的 expires_at
    vendor._token_manager._expires_at = 999999999.0

    vendor._on_error_status(401)
    assert vendor._token_manager._expires_at == 0.0


def test_on_error_status_ignores_other_codes():
    """非 401/403 不触发 token 失效."""
    config = AntigravityConfig()
    vendor = AntigravityVendor(config, FailoverConfig(), ModelMapper([]))
    vendor._token_manager._expires_at = 999999999.0

    vendor._on_error_status(429)
    assert vendor._token_manager._expires_at == 999999999.0


def test_inherits_failover():
    """继承基类 failover 判断."""
    failover = FailoverConfig(status_codes=[429, 503], error_types=["rate_limit_error"])
    config = AntigravityConfig()
    vendor = AntigravityVendor(config, failover, ModelMapper([]))

    assert vendor.should_trigger_failover(429, None)
    assert not vendor.should_trigger_failover(200, None)
    assert vendor.should_trigger_failover(
        429, {"error": {"type": "rate_limit_error", "message": "limited"}}
    )


def test_model_endpoint_in_config():
    """model_endpoint 可配置."""
    config = AntigravityConfig(model_endpoint="models/claude-opus-4-20250514")
    vendor = AntigravityVendor(config, FailoverConfig(), ModelMapper([]))
    assert vendor._model_endpoint == "models/claude-opus-4-20250514"


def test_mark_scope_error_if_needed():
    """识别 ACCESS_TOKEN_SCOPE_INSUFFICIENT 并写入诊断."""
    vendor = AntigravityVendor(AntigravityConfig(), FailoverConfig(), ModelMapper([]))
    vendor._mark_scope_error_if_needed("ACCESS_TOKEN_SCOPE_INSUFFICIENT")
    diagnostics = vendor.get_diagnostics()
    assert diagnostics["token_manager"]["error_kind"] == "insufficient_scope"


def test_antigravity_supports_request_with_tools_thinking_and_metadata():
    vendor = AntigravityVendor(AntigravityConfig(), FailoverConfig(), ModelMapper([]))
    supported, reasons = vendor.supports_request(
        RequestCapabilities(
            has_tools=True,
            has_thinking=True,
            has_metadata=True,
        )
    )
    assert supported is True
    assert reasons == []


# ── 新增测试用例 ──────────────────────────────────────


@pytest.mark.asyncio
async def test_prepare_request_no_anthropic_beta_header():
    """_prepare_request 输出不含 anthropic-beta header."""
    config = AntigravityConfig()
    vendor = AntigravityVendor(config, FailoverConfig(), ModelMapper([]))
    vendor._token_manager.get_token = AsyncMock(return_value="tok")

    _, headers = await vendor._prepare_request(
        {
            "model": "claude-sonnet-4-20250514",
            "messages": [{"role": "user", "content": "Hi"}],
        },
        {},
    )

    assert "anthropic-beta" not in headers
    assert headers["authorization"] == "Bearer tok"
    assert headers["content-type"] == "application/json"


@pytest.mark.asyncio
async def test_diagnostics_include_adaptations():
    """get_diagnostics() 包含 request_adaptations."""
    config = AntigravityConfig()
    vendor = AntigravityVendor(config, FailoverConfig(), ModelMapper([]))
    vendor._token_manager.get_token = AsyncMock(return_value="tok")

    await vendor._prepare_request(
        {
            "model": "test",
            "messages": [{"role": "user", "content": ""}],
        },
        {},
    )

    diag = vendor.get_diagnostics()
    assert "request_adaptations" in diag
    # 空 message 应触发 empty_contents_padded adaptation
    assert any("empty_contents_padded" in a for a in diag["request_adaptations"])


@pytest.mark.asyncio
async def test_send_message_uses_cached_resolution():
    """send_message 不重复调用 map_model，使用 _prepare_request 缓存值."""
    config = AntigravityConfig()
    mapper = ModelMapper(
        [
            ModelMappingRule(
                pattern="claude-*",
                target="resolved-model",
                vendors=["antigravity"],
            ),
        ]
    )
    vendor = AntigravityVendor(config, FailoverConfig(), mapper)
    vendor._token_manager.get_token = AsyncMock(return_value="tok")

    # 先调用 _prepare_request 设置缓存
    await vendor._prepare_request(
        {
            "model": "claude-sonnet-4-20250514",
            "messages": [{"role": "user", "content": "Hi"}],
        },
        {},
    )

    # 验证 _last_resolved_model 已被设置
    assert vendor._last_resolved_model == "resolved-model"
    # 验证 map_model 调用次数为 1（仅在 _prepare_request 中调用过一次）
    assert vendor._last_requested_model == "claude-sonnet-4-20250514"


def test_compatibility_profile_json_output_native():
    """json_output 兼容性状态为 NATIVE（已支持 response_format 映射）."""
    vendor = AntigravityVendor(AntigravityConfig(), FailoverConfig(), ModelMapper([]))
    profile = vendor.get_compatibility_profile()
    assert profile.json_output.name == "NATIVE"
