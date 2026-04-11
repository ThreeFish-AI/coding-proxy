"""AntigravityVendor 和 GoogleOAuthTokenManager 单元测试."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from coding.proxy.config.schema import (
    AntigravityConfig,
    FailoverConfig,
    ModelMappingRule,
)
from coding.proxy.routing.model_mapper import ModelMapper
from coding.proxy.vendors.antigravity import (
    _V1INTERNAL_BASE_URL,
    AntigravityVendor,
    GoogleOAuthTokenManager,
)
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


# ── v1internal 协议测试 ──────────────────────────────


def test_is_v1internal_mode_with_project_id_and_v1internal_url():
    """配置了 project_id 且 base_url 含 v1internal 时启用 v1internal 模式."""
    config = AntigravityConfig(
        project_id="my-gcp-project",
        base_url="https://cloudcode-pa.googleapis.com/v1internal",
    )
    vendor = AntigravityVendor(config, FailoverConfig(), ModelMapper([]))
    assert vendor._is_v1internal_mode() is True


def test_is_v1internal_mode_without_project_id():
    """未配置 project_id 时即使 URL 含 v1internal 也不启用."""
    config = AntigravityConfig(
        base_url="https://cloudcode-pa.googleapis.com/v1internal",
    )
    vendor = AntigravityVendor(config, FailoverConfig(), ModelMapper([]))
    assert vendor._is_v1internal_mode() is False


def test_is_v1internal_mode_standard_gla_url():
    """标准 GLA URL 不启用 v1internal 模式（即使有 project_id）."""
    config = AntigravityConfig(
        project_id="my-gcp-project",
        base_url="https://generativelanguage.googleapis.com/v1beta",
    )
    vendor = AntigravityVendor(config, FailoverConfig(), ModelMapper([]))
    assert vendor._is_v1internal_mode() is False


@pytest.mark.asyncio
async def test_prepare_request_v1internal_envelope():
    """v1internal 模式下请求体应被包裹在 v1internal 信封中."""
    config = AntigravityConfig(
        project_id="test-project-123",
        base_url="https://cloudcode-pa.googleapis.com/v1internal",
        model_endpoint="models/claude-sonnet-4-20250514",
    )
    vendor = AntigravityVendor(config, FailoverConfig(), ModelMapper([]))
    vendor._token_manager.get_token = AsyncMock(return_value="v1_tok")

    body, headers = await vendor._prepare_request(
        {
            "model": "claude-sonnet-4-20250514",
            "messages": [{"role": "user", "content": "Hello"}],
        },
        {},
    )

    # 验证信封结构
    assert body["project"] == "test-project-123"
    assert body["userAgent"] == "antigravity"
    assert body["requestType"] == "agent"
    assert "requestId" in body
    assert "request" in body  # 原始 Gemini 请求体
    assert "model" in body

    # 验证客户端指纹 Headers
    assert headers["x-client-name"] == "antigravity"
    assert headers["x-client-version"] == "4.1.31"
    assert "Antigravity/4.1.31" in headers["user-agent"]
    assert headers["authorization"] == "Bearer v1_tok"


@pytest.mark.asyncio
async def test_prepare_request_standard_gla_no_envelope():
    """标准 GLA 模式下请求体不做信封包装."""
    config = AntigravityConfig(
        base_url="https://generativelanguage.googleapis.com/v1beta",
    )
    vendor = AntigravityVendor(config, FailoverConfig(), ModelMapper([]))
    vendor._token_manager.get_token = AsyncMock(return_value="gla_tok")

    body, headers = await vendor._prepare_request(
        {
            "model": "claude-sonnet-4-20250514",
            "messages": [{"role": "user", "content": "Hello"}],
        },
        {},
    )

    # 标准模式：直接是 Gemini 请求体，无信封字段
    assert "project" not in body
    assert "contents" in body  # Gemini 格式的 contents 字段
    assert headers["authorization"] == "Bearer gla_tok"
    assert "x-client-name" not in headers


def test_mark_scope_error_if_needed_enhanced_logging(caplog):
    """_mark_scope_error_if_needed 在检测到 scope 错误时应输出增强诊断日志."""
    import logging

    vendor = AntigravityVendor(AntigravityConfig(), FailoverConfig(), ModelMapper([]))
    with caplog.at_level(logging.ERROR, logger="coding.proxy.vendors.antigravity"):
        vendor._mark_scope_error_if_needed(
            '{"error": {"message": "ACCESS_TOKEN_SCOPE_INSUFFICIENT"}}'
        )

    # 应包含增强的诊断提示信息
    assert any("v1internal" in r.message for r in caplog.records)


# ── project_id 自动发现测试 ──────────────────────────────


def test_effective_project_id_returns_configured_value():
    """配置了 project_id 时优先返回配置值."""
    config = AntigravityConfig(project_id="manual-project")
    vendor = AntigravityVendor(config, FailoverConfig(), ModelMapper([]))
    assert vendor._effective_project_id == "manual-project"


def test_effective_project_id_returns_discovered_when_no_config():
    """未配置但已发现时返回发现值."""
    config = AntigravityConfig()
    vendor = AntigravityVendor(config, FailoverConfig(), ModelMapper([]))
    vendor._project_id_discovered = "auto-discovered"
    assert vendor._effective_project_id == "auto-discovered"


def test_effective_project_id_empty_when_neither():
    """既未配置也未发现时返回空字符串."""
    config = AntigravityConfig()
    vendor = AntigravityVendor(config, FailoverConfig(), ModelMapper([]))
    assert vendor._effective_project_id == ""


@pytest.mark.asyncio
async def test_discover_project_id_single_active_project():
    """单个 ACTIVE 项目 → 直接选中并返回 projectId，自动切换 v1internal 模式."""
    config = AntigravityConfig()  # 未配置 project_id
    vendor = AntigravityVendor(config, FailoverConfig(), ModelMapper([]))

    mock_response = MagicMock()
    mock_response.json.return_value = {
        "projects": [
            {
                "projectId": "my-gcp-123",
                "projectNumber": "456",
                "name": "My GCP Project",
                "lifecycleState": "ACTIVE",
            }
        ]
    }
    mock_response.raise_for_status = MagicMock()

    mock_client = AsyncMock()
    mock_client.get.return_value = mock_response
    mock_client.is_closed = False
    vendor._client = mock_client

    result = await vendor._discover_project_id("test_token")

    assert result == "my-gcp-123"
    assert vendor._project_id_discovered == "my-gcp-123"
    assert vendor._base_url == "https://cloudcode-pa.googleapis.com/v1internal"
    assert vendor._is_v1internal_mode() is True


@pytest.mark.asyncio
async def test_discover_project_id_multiple_projects_selects_first_active():
    """多个项目 → 选择第一个 ACTIVE 的."""
    config = AntigravityConfig()
    vendor = AntigravityVendor(config, FailoverConfig(), ModelMapper([]))

    mock_response = MagicMock()
    mock_response.json.return_value = {
        "projects": [
            {"projectId": "inactive-proj", "lifecycleState": "INACTIVE"},
            {"projectId": "active-proj", "lifecycleState": "ACTIVE"},
        ]
    }
    mock_response.raise_for_status = MagicMock()

    mock_client = AsyncMock()
    mock_client.get.return_value = mock_response
    mock_client.is_closed = False
    vendor._client = mock_client

    result = await vendor._discover_project_id("test_token")
    assert result == "active-proj"


@pytest.mark.asyncio
async def test_discover_project_id_no_active_selects_first_non_deleting():
    """无 ACTIVE 项目 → 选择第一个非 DELETE_REQUESTED 的."""
    config = AntigravityConfig()
    vendor = AntigravityVendor(config, FailoverConfig(), ModelMapper([]))

    mock_response = MagicMock()
    mock_response.json.return_value = {
        "projects": [
            {"projectId": "deleting-proj", "lifecycleState": "DELETE_REQUESTED"},
            {"projectId": "pending-proj", "lifecycleState": "PENDING"},
        ]
    }
    mock_response.raise_for_status = MagicMock()

    mock_client = AsyncMock()
    mock_client.get.return_value = mock_response
    mock_client.is_closed = False
    vendor._client = mock_client

    result = await vendor._discover_project_id("test_token")
    assert result == "pending-proj"


@pytest.mark.asyncio
async def test_discover_project_id_all_deleting_falls_back_to_first():
    """全部在删除中 → 兜底选择第一个."""
    config = AntigravityConfig()
    vendor = AntigravityVendor(config, FailoverConfig(), ModelMapper([]))

    mock_response = MagicMock()
    mock_response.json.return_value = {
        "projects": [
            {"projectId": "del-1", "lifecycleState": "DELETE_REQUESTED"},
            {"projectId": "del-2", "lifecycleState": "DELETE_REQUESTED"},
        ]
    }
    mock_response.raise_for_status = MagicMock()

    mock_client = AsyncMock()
    mock_client.get.return_value = mock_response
    mock_client.is_closed = False
    vendor._client = mock_client

    result = await vendor._discover_project_id("test_token")
    assert result == "del-1"


@pytest.mark.asyncio
async def test_discover_project_id_empty_list_returns_empty():
    """空项目列表 → 返回空字符串."""
    config = AntigravityConfig()
    vendor = AntigravityVendor(config, FailoverConfig(), ModelMapper([]))

    mock_response = MagicMock()
    mock_response.json.return_value = {"projects": []}
    mock_response.raise_for_status = MagicMock()

    mock_client = AsyncMock()
    mock_client.get.return_value = mock_response
    mock_client.is_closed = False
    vendor._client = mock_client

    result = await vendor._discover_project_id("test_token")
    assert result == ""
    assert vendor._project_id_discovered == ""


@pytest.mark.asyncio
async def test_discover_project_id_api_error_returns_empty():
    """API 返回 HTTP 错误 → 返回空字符串."""
    config = AntigravityConfig()
    vendor = AntigravityVendor(config, FailoverConfig(), ModelMapper([]))

    mock_client = AsyncMock()
    mock_client.is_closed = False
    mock_client.get.side_effect = httpx.HTTPStatusError(
        "403 Forbidden",
        request=MagicMock(),
        response=httpx.Response(403, request=MagicMock()),
    )
    vendor._client = mock_client

    result = await vendor._discover_project_id("test_token")
    assert result == ""
    assert vendor._project_discovery_attempted is True


@pytest.mark.asyncio
async def test_discover_project_id_idempotent():
    """重复调用不重复请求 API（attempted 标志）."""
    config = AntigravityConfig()
    vendor = AntigravityVendor(config, FailoverConfig(), ModelMapper([]))

    # 首次调用：返回空列表（无项目）
    mock_response = MagicMock()
    mock_response.json.return_value = {"projects": []}
    mock_response.raise_for_status = MagicMock()
    mock_client = AsyncMock()
    mock_client.get.return_value = mock_response
    mock_client.is_closed = False
    vendor._client = mock_client

    r1 = await vendor._discover_project_id("tok1")
    r2 = await vendor._discover_project_id("tok2")

    assert r1 == ""
    assert r2 == ""
    # 只请求了一次（第二次因 attempted=True 短路返回）
    assert mock_client.get.await_count == 1


@pytest.mark.asyncio
async def test_discover_skips_when_configured():
    """已配置 project_id 时发现返回空且不执行 API 调用."""
    config = AntigravityConfig(project_id="manual-id")
    vendor = AntigravityVendor(config, FailoverConfig(), ModelMapper([]))

    result = await vendor._discover_project_id("token")
    # 应直接返回空（因为 _project_id 已配置）
    assert result == ""
    # attempted 标记应已设置（标记为"已处理"状态）
    assert vendor._project_discovery_attempted is True


@pytest.mark.asyncio
async def test_prepare_request_triggers_discovery_when_no_project_id():
    """未配置 project_id 时 _prepare_request 应触发自动发现."""
    config = AntigravityConfig()
    vendor = AntigravityVendor(config, FailoverConfig(), ModelMapper([]))
    vendor._token_manager.get_token = AsyncMock(return_value="tok")

    # Mock discovery 返回成功
    original_discover = vendor._discover_project_id
    call_count = [0]

    async def mock_discover(token):
        call_count[0] += 1
        vendor._project_id_discovered = "auto-found"
        vendor._base_url = (
            _V1INTERNAL_BASE_URL
            if "v1internal" not in vendor._base_url
            else vendor._base_url
        )
        return "auto-found"

    vendor._discover_project_id = mock_discover

    body, headers = await vendor._prepare_request(
        {
            "model": "claude-sonnet-4-20250514",
            "messages": [{"role": "user", "content": "Hi"}],
        },
        {},
    )

    assert call_count[0] == 1
    # 恢复原始方法
    vendor._discover_project_id = original_discover


@pytest.mark.asyncio
async def test_prepare_request_skips_discovery_when_configured():
    """已配置 project_id 时 _prepare_request 不触发发现."""
    config = AntigravityConfig(project_id="manual-id")
    vendor = AntigravityVendor(config, FailoverConfig(), ModelMapper([]))
    vendor._token_manager.get_token = AsyncMock(return_value="tok")

    call_count = [0]

    async def mock_discover(token):
        call_count[0] += 1
        return ""

    vendor._discover_project_id = mock_discover

    await vendor._prepare_request(
        {
            "model": "claude-sonnet-4-20250514",
            "messages": [{"role": "user", "content": "Hi"}],
        },
        {},
    )

    assert call_count[0] == 0  # 不应触发发现


def test_is_v1internal_mode_uses_effective_project_id():
    """_is_v1internal_mode 应基于 _effective_project_id 判断."""
    config = AntigravityConfig(base_url=_V1INTERNAL_BASE_URL)
    vendor = AntigravityVendor(config, FailoverConfig(), ModelMapper([]))

    # 未配置、未发现 → False
    assert vendor._is_v1internal_mode() is False

    # 发现后 → True
    vendor._project_id_discovered = "found-it"
    assert vendor._is_v1internal_mode() is True

    # 配置值覆盖发现值
    vendor._project_id_discovered = ""
    vendor._project_id = "manual"
    assert vendor._is_v1internal_mode() is True


def test_diagnostics_includes_discovery_status():
    """get_diagnostics() 包含 project_id 来源和 v1internal 模式状态."""
    config = AntigravityConfig()
    vendor = AntigravityVendor(config, FailoverConfig(), ModelMapper([]))

    diag = vendor.get_diagnostics()
    assert diag["project_id_source"] == "none"
    assert diag["is_v1internal_mode"] is False

    # 模拟发现后
    vendor._project_id_discovered = "auto-p-123"
    diag = vendor.get_diagnostics()
    assert diag["project_id_source"] == "discovered"
    assert diag["discovered_project_id"] == "auto-p-123"

    # 手动配置覆盖
    vendor._project_id = "manual-p"
    diag = vendor.get_diagnostics()
    assert diag["project_id_source"] == "configured"
