"""CopilotTokenManager 和 CopilotBackend 单元测试."""

from __future__ import annotations

import json
import time
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from coding.proxy.backends.base import CapabilityLossReason, RequestCapabilities
from coding.proxy.backends.copilot import (
    CopilotBackend,
    CopilotTokenManager,
    build_copilot_candidate_base_urls,
    normalize_copilot_requested_model,
    resolve_copilot_base_url,
    _select_copilot_model,
)
from coding.proxy.backends.token_manager import TokenAcquireError, TokenErrorKind
from coding.proxy.config.schema import CopilotConfig, FailoverConfig, ModelMappingRule
from coding.proxy.routing.model_mapper import ModelMapper


class _AsyncRequestClientStub:
    def __init__(self, response: httpx.Response) -> None:
        self._response = response
        self.is_closed = False

    async def request(self, *args, **kwargs) -> httpx.Response:
        return self._response

    async def aclose(self) -> None:
        self.is_closed = True

    async def __aenter__(self) -> "_AsyncRequestClientStub":
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        self.is_closed = True


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
async def test_token_manager_exchange_supports_token_refresh_in_shape():
    """兼容 Copilot 当前返回的 token/refresh_in 形态."""
    tm = CopilotTokenManager("ghp_test", "https://api.github.com/copilot_internal/v2/token")

    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = {"token": "cop_new", "refresh_in": 1740}

    mock_client = AsyncMock()
    mock_client.get.return_value = mock_response
    mock_client.is_closed = False
    tm._client = mock_client

    token = await tm.get_token()
    assert token == "cop_new"
    diagnostics = tm.get_exchange_diagnostics()
    assert diagnostics["raw_shape"] == "token_refresh_in"
    assert diagnostics["token_field"] == "token"
    assert diagnostics["expires_in_seconds"] == 1740


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


@pytest.mark.asyncio
async def test_token_manager_permission_upgrade_required():
    """200 但返回 capability 文档时，识别为权限升级需求而非普通过期."""
    tm = CopilotTokenManager("ghp_test", "https://fake")

    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = {
        "agent_mode_auto_approval": True,
        "chat_enabled": True,
        "chat_jetbrains_enabled": True,
    }

    mock_client = AsyncMock()
    mock_client.get.return_value = mock_response
    mock_client.is_closed = False
    tm._client = mock_client

    with pytest.raises(TokenAcquireError) as exc_info:
        await tm.get_token()

    assert exc_info.value.needs_reauth is True
    assert exc_info.value.kind == TokenErrorKind.PERMISSION_UPGRADE_REQUIRED


@pytest.mark.asyncio
async def test_token_manager_token_and_capabilities_still_succeeds():
    """响应同时含 token 与 capability 字段时，不应误判为权限不足."""
    tm = CopilotTokenManager("ghp_test", "https://fake")

    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = {
        "token": "cop_ok",
        "refresh_in": 1200,
        "chat_enabled": True,
        "chat_jetbrains_enabled": True,
    }

    mock_client = AsyncMock()
    mock_client.get.return_value = mock_response
    mock_client.is_closed = False
    tm._client = mock_client

    token = await tm.get_token()
    assert token == "cop_ok"
    diagnostics = tm.get_exchange_diagnostics()
    assert diagnostics["capabilities"]["chat_enabled"] is True


# --- CopilotBackend ---


def test_copilot_get_name():
    config = CopilotConfig(github_token="ghp_test")
    backend = CopilotBackend(config, FailoverConfig())
    assert backend.get_name() == "copilot"


def test_resolve_copilot_base_url():
    assert resolve_copilot_base_url("individual", "") == "https://api.individual.githubcopilot.com"
    assert resolve_copilot_base_url("business", "") == "https://api.business.githubcopilot.com"
    assert resolve_copilot_base_url("enterprise", "") == "https://api.enterprise.githubcopilot.com"
    assert resolve_copilot_base_url("individual", "https://custom.example.com") == "https://custom.example.com"


def test_build_copilot_candidate_base_urls():
    assert build_copilot_candidate_base_urls("individual", "") == [
        "https://api.individual.githubcopilot.com",
        "https://api.githubcopilot.com",
    ]
    assert build_copilot_candidate_base_urls("business", "") == [
        "https://api.business.githubcopilot.com",
        "https://api.githubcopilot.com",
    ]
    assert build_copilot_candidate_base_urls("individual", "https://custom.example.com/") == [
        "https://custom.example.com",
    ]


def test_normalize_copilot_requested_model():
    assert normalize_copilot_requested_model("claude-sonnet-4-20250514") == "claude-sonnet-4"
    assert normalize_copilot_requested_model("claude-opus-4-20250514") == "claude-opus-4"
    assert normalize_copilot_requested_model("claude-haiku-4-20250514") == "claude-haiku-4"
    assert normalize_copilot_requested_model("gpt-5.2") == "gpt-5.2"


def test_select_copilot_model_prefers_same_family_highest_version():
    selected, reason = _select_copilot_model(
        "claude-sonnet-4-20250514",
        ["claude-sonnet-4.5", "claude-sonnet-4.6", "claude-opus-4.6"],
    )
    assert selected == "claude-sonnet-4.6"
    assert reason == "same_family_highest_version"


def test_select_copilot_model_does_not_cross_family():
    selected, reason = _select_copilot_model(
        "claude-sonnet-4-20250514",
        ["claude-opus-4.6"],
    )
    assert selected is None
    assert reason == "no_same_family_model_available"


@pytest.mark.asyncio
async def test_copilot_prepare_request_filters_and_injects_token():
    """_prepare_request 过滤 hop-by-hop 头并注入 Copilot token."""
    config = CopilotConfig(github_token="ghp_test")
    backend = CopilotBackend(config, FailoverConfig())

    # Mock token manager
    backend._token_manager.get_token = AsyncMock(return_value="cop_injected")
    backend._model_resolver.fetch_available = AsyncMock(return_value=["claude-sonnet-4.6"])  # type: ignore[method-assign]

    body = {"model": "claude-sonnet-4-20250514", "messages": []}
    headers = {
        "authorization": "Bearer original",
        "host": "localhost",
        "content-length": "100",
        "anthropic-version": "2023-06-01",
    }
    prepared_body, prepared_headers = await backend._prepare_request(body, headers)
    assert prepared_body["model"] == "claude-sonnet-4.6"
    assert prepared_body["messages"] == []
    assert "host" not in prepared_headers
    assert "content-length" not in prepared_headers
    assert prepared_headers["authorization"] == "Bearer cop_injected"
    assert prepared_headers["anthropic-version"] == "2023-06-01"
    assert prepared_headers["copilot-integration-id"] == "vscode-chat"
    assert prepared_headers["openai-intent"] == "conversation-panel"
    assert prepared_headers["x-initiator"] == "user"


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


def test_copilot_capabilities_enable_thinking():
    config = CopilotConfig(github_token="ghp_test")
    backend = CopilotBackend(config, FailoverConfig())
    caps = backend.get_capabilities()
    assert caps.supports_tools is True
    assert caps.supports_images is True
    assert caps.supports_thinking is True


def test_copilot_supports_request_with_thinking_via_adapter():
    config = CopilotConfig(github_token="ghp_test")
    backend = CopilotBackend(config, FailoverConfig())

    supported, reasons = backend.supports_request(RequestCapabilities(has_thinking=True))

    assert supported is True
    assert CapabilityLossReason.THINKING not in reasons


@pytest.mark.asyncio
async def test_copilot_prepare_request_records_thinking_adaptations():
    config = CopilotConfig(github_token="ghp_test")
    backend = CopilotBackend(config, FailoverConfig())
    backend._token_manager.get_token = AsyncMock(return_value="cop_injected")
    backend._model_resolver.fetch_available = AsyncMock(return_value=["claude-sonnet-4.6"])  # type: ignore[method-assign]

    body = {
        "model": "claude-sonnet-4-20250514",
        "messages": [{
            "role": "assistant",
            "content": [{"type": "thinking", "thinking": "先分析"}],
        }],
        "thinking": {"budget_tokens": 1024},
    }

    prepared_body, _ = await backend._prepare_request(body, {"anthropic-version": "2023-06-01"})

    # thinking/extended_thinking 已被映射为 reasoning_effort，不应保留原始字段
    assert "thinking" not in prepared_body
    assert "extended_thinking" not in prepared_body
    # reasoning_effort 应存在（从 thinking dict 映射而来）
    assert prepared_body.get("reasoning_effort") == "medium"
    diagnostics = backend.get_diagnostics()
    # 适配标签应反映新的映射策略（thinking dict 触发）
    assert any("thinking_mapped_to_reasoning_effort" in a for a in diagnostics["request_adaptations"])
    assert any("thinking_block_used_as_content_fallback" in a for a in diagnostics["request_adaptations"])
    assert diagnostics["resolved_model"] == "claude-sonnet-4.6"


@pytest.mark.asyncio
async def test_copilot_prepare_request_uses_cached_models_without_refetch():
    config = CopilotConfig(github_token="ghp_test", models_cache_ttl_seconds=300)
    backend = CopilotBackend(config, FailoverConfig())
    backend._token_manager.get_token = AsyncMock(return_value="cop_injected")
    backend._model_resolver.catalog.available_models = ["claude-sonnet-4.6"]
    backend._model_resolver.catalog.fetched_at_unix = int(time.time())
    backend._model_resolver.fetch_available = AsyncMock(side_effect=AssertionError("should not refetch"))  # type: ignore[method-assign]

    prepared_body, _ = await backend._prepare_request(
        {"model": "claude-sonnet-4-20250514", "messages": []},
        {"anthropic-version": "2023-06-01"},
    )

    assert prepared_body["model"] == "claude-sonnet-4.6"


@pytest.mark.asyncio
async def test_copilot_request_with_421_retries_fresh_connection():
    config = CopilotConfig(github_token="ghp_test")
    backend = CopilotBackend(config, FailoverConfig())

    initial_request = httpx.Request("POST", "https://api.individual.githubcopilot.com/chat/completions")
    backend._client = _AsyncRequestClientStub(httpx.Response(  # type: ignore[assignment]
        421,
        content=b"Misdirected Request\n",
        request=initial_request,
    ))

    retry_client = _AsyncRequestClientStub(httpx.Response(
        200,
        content=b'{"ok":true}',
        headers={"content-type": "application/json"},
        request=initial_request,
    ))
    backend._create_fresh_client = lambda base_url: retry_client  # type: ignore[method-assign]

    response = await backend._request_with_421_retry(
        "POST",
        "/chat/completions",
        headers={"authorization": "Bearer cop"},
        json_body={"model": "claude-sonnet-4"},
    )

    assert response.status_code == 200
    diagnostics = backend.get_diagnostics()
    assert diagnostics["last_request_base_url"] == "https://api.individual.githubcopilot.com"
    assert diagnostics["last_421_base_url"] == "https://api.individual.githubcopilot.com"
    assert diagnostics["last_retry_base_url"] == "https://api.individual.githubcopilot.com"


@pytest.mark.asyncio
async def test_copilot_request_with_421_falls_back_to_public_domain():
    config = CopilotConfig(github_token="ghp_test")
    backend = CopilotBackend(config, FailoverConfig())

    initial_request = httpx.Request("POST", "https://api.individual.githubcopilot.com/chat/completions")
    backend._client = _AsyncRequestClientStub(httpx.Response(  # type: ignore[assignment]
        421,
        content=b"Misdirected Request\n",
        request=initial_request,
    ))

    retry_clients = [
        _AsyncRequestClientStub(httpx.Response(
            421,
            content=b"Misdirected Request\n",
            request=initial_request,
        )),
        _AsyncRequestClientStub(httpx.Response(
            200,
            content=b'{"ok":true}',
            headers={"content-type": "application/json"},
            request=httpx.Request("POST", "https://api.githubcopilot.com/chat/completions"),
        )),
    ]
    retry_urls: list[str] = []

    def _fake_create_fresh_client(base_url: str):
        retry_urls.append(base_url)
        return retry_clients.pop(0)

    backend._create_fresh_client = _fake_create_fresh_client  # type: ignore[method-assign]

    response = await backend._request_with_421_retry(
        "POST",
        "/chat/completions",
        headers={"authorization": "Bearer cop"},
        json_body={"model": "claude-sonnet-4"},
    )

    assert response.status_code == 200
    assert retry_urls == [
        "https://api.individual.githubcopilot.com",
        "https://api.githubcopilot.com",
    ]
    diagnostics = backend.get_diagnostics()
    assert diagnostics["resolved_base_url"] == "https://api.githubcopilot.com"
    assert diagnostics["last_retry_base_url"] == "https://api.githubcopilot.com"


@pytest.mark.asyncio
async def test_copilot_stream_421_retries_alternate_base_url():
    config = CopilotConfig(github_token="ghp_test")
    backend = CopilotBackend(config, FailoverConfig())
    backend._token_manager.get_token = AsyncMock(return_value="cop_injected")

    current_base_url = "https://api.individual.githubcopilot.com"
    fallback_base_url = "https://api.githubcopilot.com"
    stream_calls: list[str] = []

    async def _fake_stream_from_client(client, *, base_url, body, prepared_headers, request_model):
        stream_calls.append(base_url)
        request = httpx.Request("POST", f"{base_url}/chat/completions")
        if base_url != fallback_base_url:
            raise httpx.HTTPStatusError(
                "copilot API error: 421",
                request=request,
                response=httpx.Response(421, content=b"Misdirected Request\n", request=request),
            )
        yield b"event: message_stop\ndata: {\"type\":\"message_stop\"}\n\n"

    backend._stream_from_client = _fake_stream_from_client  # type: ignore[method-assign]
    backend._create_fresh_client = lambda base_url: _AsyncRequestClientStub(  # type: ignore[method-assign]
        httpx.Response(200, request=httpx.Request("POST", f"{base_url}/chat/completions"))
    )

    chunks = []
    async for chunk in backend.send_message_stream(
        {"model": "claude-sonnet-4-20250514", "messages": []},
        {"anthropic-version": "2023-06-01"},
    ):
        chunks.append(chunk)

    assert chunks
    assert stream_calls == [
        current_base_url,
        current_base_url,
        fallback_base_url,
    ]
    assert backend.get_diagnostics()["resolved_base_url"] == fallback_base_url


@pytest.mark.asyncio
async def test_copilot_stream_retries_after_model_not_supported():
    config = CopilotConfig(github_token="ghp_test")
    backend = CopilotBackend(config, FailoverConfig())
    backend._token_manager.get_token = AsyncMock(return_value="cop_injected")

    refreshed = False

    async def _fake_fetch_available_models(*, refresh_reason: str, **kwargs) -> list[str]:
        nonlocal refreshed
        refreshed = refreshed or refresh_reason == "model_not_supported_retry"
        return ["claude-sonnet-4.5"] if not refreshed else ["claude-sonnet-4.6"]

    backend._model_resolver.fetch_available = _fake_fetch_available_models  # type: ignore[method-assign]

    stream_models: list[str] = []

    async def _fake_stream_from_client(client, *, base_url, body, prepared_headers, request_model):
        stream_models.append(body["model"])
        request = httpx.Request("POST", f"{base_url}/chat/completions")
        if len(stream_models) == 1:
            raise httpx.HTTPStatusError(
                "copilot API error: 400",
                request=request,
                response=httpx.Response(
                    400,
                    content=b'{"error":{"message":"The requested model is not supported.","code":"model_not_supported","param":"model","type":"invalid_request_error"}}',
                    headers={"content-type": "application/json"},
                    request=request,
                ),
            )
        yield b"event: message_start\ndata: {\"type\":\"message_start\",\"message\":{\"id\":\"msg_1\",\"model\":\"claude-sonnet-4.6\",\"usage\":{\"input_tokens\":10}}}\n\n"
        yield b"event: message_stop\ndata: {\"type\":\"message_stop\"}\n\n"

    backend._stream_from_client = _fake_stream_from_client  # type: ignore[method-assign]

    chunks = []
    async for chunk in backend.send_message_stream(
        {"model": "claude-sonnet-4-20250514", "messages": []},
        {"anthropic-version": "2023-06-01"},
    ):
        chunks.append(chunk)

    assert chunks
    assert stream_models == ["claude-sonnet-4.5", "claude-sonnet-4.6"]
    assert backend.get_diagnostics()["resolved_model"] == "claude-sonnet-4.6"


@pytest.mark.asyncio
async def test_probe_models_reports_opus_46():
    config = CopilotConfig(github_token="ghp_test")
    backend = CopilotBackend(config, FailoverConfig())
    backend._token_manager.get_token = AsyncMock(return_value="cop_token")

    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.content = b'{"data":[{"id":"claude-opus-4.6"},{"id":"claude-sonnet-4"}]}'
    mock_response.json.return_value = {
        "data": [{"id": "claude-opus-4.6"}, {"id": "claude-sonnet-4"}]
    }

    mock_client = AsyncMock()
    mock_client.request.return_value = mock_response
    mock_client.is_closed = False
    backend._client = mock_client

    probe = await backend.probe_models()
    assert probe["probe_status"] == "ok"
    assert probe["has_claude_opus_4_6"] is True
    assert "claude-opus-4.6" in probe["available_models"]
    assert backend.get_diagnostics()["available_models_cache"] == ["claude-opus-4.6", "claude-sonnet-4"]


@pytest.mark.asyncio
async def test_copilot_send_message_translates_openai_response_to_anthropic():
    config = CopilotConfig(github_token="ghp_test")
    backend = CopilotBackend(config, FailoverConfig())
    backend._token_manager.get_token = AsyncMock(return_value="cop_token")
    backend._model_resolver.fetch_available = AsyncMock(return_value=["claude-opus-4.6"])  # type: ignore[method-assign]

    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.content = b'{"id":"chatcmpl_1","model":"claude-opus-4","choices":[{"message":{"role":"assistant","content":"hello"},"finish_reason":"stop","index":0,"logprobs":null}],"usage":{"prompt_tokens":7,"completion_tokens":3,"total_tokens":10}}'
    mock_response.headers = {"content-type": "application/json"}
    mock_response.json.return_value = {
        "id": "chatcmpl_1",
        "model": "claude-opus-4",
        "choices": [{
            "message": {"role": "assistant", "content": "hello"},
            "finish_reason": "stop",
            "index": 0,
            "logprobs": None,
        }],
        "usage": {"prompt_tokens": 7, "completion_tokens": 3, "total_tokens": 10},
    }

    mock_client = AsyncMock()
    mock_client.request.return_value = mock_response
    mock_client.is_closed = False
    backend._client = mock_client

    resp = await backend.send_message(
        {"model": "claude-opus-4-20250514", "messages": [{"role": "user", "content": "hi"}]},
        {"anthropic-version": "2023-06-01"},
    )
    body = json.loads(resp.raw_body)
    assert resp.status_code == 200
    assert body["type"] == "message"
    assert body["content"][0]["text"] == "hello"
    assert body["usage"]["input_tokens"] == 7
    assert resp.model_served == "claude-opus-4"


@pytest.mark.asyncio
async def test_copilot_send_message_retries_after_model_not_supported():
    config = CopilotConfig(github_token="ghp_test")
    backend = CopilotBackend(config, FailoverConfig())
    backend._token_manager.get_token = AsyncMock(return_value="cop_token")

    refreshed = False

    async def _fake_fetch_available_models(*, refresh_reason: str, **kwargs) -> list[str]:
        nonlocal refreshed
        refreshed = refreshed or refresh_reason == "model_not_supported_retry"
        return ["claude-sonnet-4.5"] if not refreshed else ["claude-sonnet-4.6"]

    backend._model_resolver.fetch_available = _fake_fetch_available_models  # type: ignore[method-assign]

    first_request = httpx.Request("POST", "https://api.individual.githubcopilot.com/chat/completions")
    success_payload = {
        "id": "chatcmpl_2",
        "model": "claude-sonnet-4.6",
        "choices": [{
            "message": {"role": "assistant", "content": "fixed"},
            "finish_reason": "stop",
            "index": 0,
            "logprobs": None,
        }],
        "usage": {"prompt_tokens": 5, "completion_tokens": 2, "total_tokens": 7},
    }
    responses = [
        httpx.Response(
            400,
            content=b'{"error":{"message":"The requested model is not supported.","code":"model_not_supported","param":"model","type":"invalid_request_error"}}',
            headers={"content-type": "application/json"},
            request=first_request,
        ),
        httpx.Response(
            200,
            json=success_payload,
            headers={"content-type": "application/json"},
            request=first_request,
        ),
    ]

    mock_client = AsyncMock()
    mock_client.request.side_effect = responses
    mock_client.is_closed = False
    backend._client = mock_client

    resp = await backend.send_message(
        {"model": "claude-sonnet-4-20250514", "messages": [{"role": "user", "content": "hi"}]},
        {"anthropic-version": "2023-06-01"},
    )

    assert resp.status_code == 200
    assert backend.get_diagnostics()["resolved_model"] == "claude-sonnet-4.6"
    assert backend.get_diagnostics()["last_model_refresh_reason"] == "model_not_supported_retry"


@pytest.mark.asyncio
async def test_copilot_send_message_returns_enriched_model_error_when_family_missing():
    config = CopilotConfig(github_token="ghp_test")
    backend = CopilotBackend(config, FailoverConfig())
    backend._token_manager.get_token = AsyncMock(return_value="cop_token")
    backend._model_resolver.fetch_available = AsyncMock(return_value=["claude-opus-4.6"])  # type: ignore[method-assign]

    request = httpx.Request("POST", "https://api.individual.githubcopilot.com/chat/completions")
    model_error = httpx.Response(
        400,
        content=b'{"error":{"message":"The requested model is not supported.","code":"model_not_supported","param":"model","type":"invalid_request_error"}}',
        headers={"content-type": "application/json"},
        request=request,
    )
    mock_client = AsyncMock()
    mock_client.request.side_effect = [model_error, model_error]
    mock_client.is_closed = False
    backend._client = mock_client

    resp = await backend.send_message(
        {"model": "claude-sonnet-4-20250514", "messages": [{"role": "user", "content": "hi"}]},
        {"anthropic-version": "2023-06-01"},
    )

    assert resp.status_code == 400
    assert resp.error_type == "invalid_request_error"
    payload = json.loads(resp.raw_body)
    assert payload["error"]["code"] == "model_not_supported"
    assert payload["error"]["details"]["available_models"] == ["claude-opus-4.6"]


@pytest.mark.asyncio
async def test_copilot_send_message_handles_non_json_success_without_crash():
    config = CopilotConfig(github_token="ghp_test")
    backend = CopilotBackend(config, FailoverConfig())
    backend._token_manager.get_token = AsyncMock(return_value="cop_token")

    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.content = b"<!doctype html>"
    mock_response.headers = {"content-type": "text/html"}
    mock_response.text = "<!doctype html>"
    mock_response.json.side_effect = json.JSONDecodeError("Expecting value", "", 0)

    mock_client = AsyncMock()
    mock_client.request.return_value = mock_response
    mock_client.is_closed = False
    backend._client = mock_client

    resp = await backend.send_message(
        {"model": "claude-opus-4-20250514", "messages": [{"role": "user", "content": "hi"}]},
        {"anthropic-version": "2023-06-01"},
    )
    assert resp.status_code == 502
    assert resp.error_type == "api_error"


# ===== ModelMapper 集成测试 =====

def _make_copilot_mapper(rules: list[ModelMappingRule]) -> ModelMapper:
    return ModelMapper(rules)


def _make_copilot_backend(mapper: ModelMapper | None = None) -> CopilotBackend:
    return CopilotBackend(CopilotConfig(github_token="ghp_test"), FailoverConfig(), model_mapper=mapper)


@pytest.mark.asyncio
async def test_resolve_model_uses_config_mapping_when_rule_matches():
    """配置规则命中时直接返回目标模型，不走内部解析（不调用 _get_available_models）."""
    mapper = _make_copilot_mapper([
        ModelMappingRule(pattern="claude-sonnet-.*", target="claude-sonnet-4.6", is_regex=True, backends=["copilot"]),
    ])
    backend = _make_copilot_backend(mapper)
    backend._model_resolver.get_available = AsyncMock(side_effect=AssertionError("不应调用 get_available"))

    resolved = await backend._resolve_request_model(
        "claude-sonnet-4-20250514", force_refresh=False, refresh_reason="test"
    )

    assert resolved == "claude-sonnet-4.6"
    assert backend._last_resolved_model == "claude-sonnet-4.6"
    assert backend._last_model_resolution_reason == "config_model_mapping"
    assert backend._last_requested_model == "claude-sonnet-4-20250514"


@pytest.mark.asyncio
async def test_resolve_model_falls_back_to_internal_when_no_copilot_rule():
    """配置规则无 copilot 条目时，走内部家族匹配策略."""
    mapper = _make_copilot_mapper([
        ModelMappingRule(pattern="claude-sonnet-.*", target="glm-5.1", is_regex=True, backends=["fallback"]),
    ])
    backend = _make_copilot_backend(mapper)
    backend._model_resolver.get_available = AsyncMock(return_value=["claude-sonnet-4.6", "claude-opus-4.6"])

    resolved = await backend._resolve_request_model(
        "claude-sonnet-4-20250514", force_refresh=False, refresh_reason="test"
    )

    assert resolved == "claude-sonnet-4.6"
    assert backend._last_model_resolution_reason == "same_family_highest_version"


@pytest.mark.asyncio
async def test_resolve_model_without_mapper_uses_internal_resolution():
    """model_mapper=None 时向后兼容，走内部家族匹配策略."""
    backend = _make_copilot_backend(mapper=None)
    backend._model_resolver.get_available = AsyncMock(return_value=["claude-haiku-4.5", "claude-sonnet-4.6"])

    resolved = await backend._resolve_request_model(
        "claude-haiku-4-20250514", force_refresh=False, refresh_reason="test"
    )

    assert resolved == "claude-haiku-4.5"
    assert backend._last_model_resolution_reason == "same_family_highest_version"


@pytest.mark.asyncio
async def test_resolve_model_config_mapping_all_three_families():
    """三个家族（sonnet / opus / haiku）的 copilot 规则均正确命中."""
    mapper = _make_copilot_mapper([
        ModelMappingRule(pattern="claude-sonnet-.*", target="claude-sonnet-4.6", is_regex=True, backends=["copilot"]),
        ModelMappingRule(pattern="claude-opus-.*", target="claude-opus-4.6", is_regex=True, backends=["copilot"]),
        ModelMappingRule(pattern="claude-haiku-.*", target="claude-haiku-4.5", is_regex=True, backends=["copilot"]),
    ])

    cases = [
        ("claude-sonnet-4-20250514", "claude-sonnet-4.6"),
        ("claude-opus-4-20250514", "claude-opus-4.6"),
        ("claude-haiku-4-20250514", "claude-haiku-4.5"),
    ]
    for requested, expected in cases:
        backend = _make_copilot_backend(mapper)
        backend._model_resolver.get_available = AsyncMock(side_effect=AssertionError("不应调用"))
        resolved = await backend._resolve_request_model(requested, force_refresh=False, refresh_reason="test")
        assert resolved == expected, f"{requested} 期望 {expected}，实际 {resolved}"
        assert backend._last_model_resolution_reason == "config_model_mapping"
