"""后端基类与子类单元测试."""

import httpx
import pytest

from coding.proxy.backends.antigravity import AntigravityBackend
from coding.proxy.backends.anthropic import AnthropicBackend
from coding.proxy.backends.base import (
    BaseBackend,
    BackendResponse,
    UsageInfo,
    _decode_json_body,
    _sanitize_headers_for_synthetic_response,
)
from coding.proxy.backends.zhipu import ZhipuBackend
from coding.proxy.config.schema import (
    AnthropicConfig,
    AntigravityConfig,
    FailoverConfig,
    ModelMappingRule,
    ZhipuConfig,
)
from coding.proxy.routing.model_mapper import ModelMapper


@pytest.mark.asyncio
async def test_anthropic_prepare_request_filters_headers():
    backend = AnthropicBackend(AnthropicConfig(), FailoverConfig())
    body = {"model": "claude-sonnet-4-20250514", "messages": []}
    headers = {
        "authorization": "Bearer sk-test",
        "host": "localhost",
        "content-length": "100",
        "anthropic-version": "2023-06-01",
    }
    prepared_body, prepared_headers = await backend._prepare_request(body, headers)
    assert prepared_body is body  # 不修改原始 body
    assert "host" not in prepared_headers
    assert "content-length" not in prepared_headers
    assert prepared_headers["authorization"] == "Bearer sk-test"
    assert prepared_headers["anthropic-version"] == "2023-06-01"


@pytest.mark.asyncio
async def test_zhipu_prepare_request_maps_model():
    mapper = ModelMapper([
        ModelMappingRule(pattern="claude-sonnet-.*", target="glm-5.1", is_regex=True),
    ])
    config = ZhipuConfig(api_key="test-key")
    backend = ZhipuBackend(config, mapper)

    body = {"model": "claude-sonnet-4-20250514", "messages": []}
    headers = {"anthropic-version": "2023-06-01"}
    prepared_body, prepared_headers = await backend._prepare_request(body, headers)

    assert prepared_body["model"] == "glm-5.1"
    assert prepared_headers["x-api-key"] == "test-key"
    assert "model" in body  # 原始 body 未被修改
    assert body["model"] == "claude-sonnet-4-20250514"


def test_anthropic_should_trigger_failover():
    failover = FailoverConfig(
        status_codes=[429, 503],
        error_types=["rate_limit_error"],
        error_message_patterns=["quota"],
    )
    backend = AnthropicBackend(AnthropicConfig(), failover)

    # 429 + rate_limit_error → True
    assert backend.should_trigger_failover(429, {
        "error": {"type": "rate_limit_error", "message": "Rate limited"}
    })

    # 429 without body → True (429/503 always trigger)
    assert backend.should_trigger_failover(429, None)

    # 200 → False
    assert not backend.should_trigger_failover(200, None)

    # 500 not in status_codes → False
    assert not backend.should_trigger_failover(500, None)

    # error message pattern match
    assert backend.should_trigger_failover(429, {
        "error": {"type": "unknown", "message": "Quota exceeded"}
    })


def test_zhipu_never_triggers_failover():
    mapper = ModelMapper([])
    backend = ZhipuBackend(ZhipuConfig(), mapper)
    assert not backend.should_trigger_failover(429, None)
    assert not backend.should_trigger_failover(500, {"error": {"type": "rate_limit_error"}})


def test_backend_response_defaults():
    resp = BackendResponse()
    assert resp.status_code == 200
    assert resp.usage.input_tokens == 0
    assert resp.raw_body == b"{}"
    assert resp.error_type is None


def test_usage_info_defaults():
    usage = UsageInfo()
    assert usage.input_tokens == 0
    assert usage.output_tokens == 0
    assert usage.cache_creation_tokens == 0
    assert usage.cache_read_tokens == 0
    assert usage.request_id == ""


# --- 基类 failover 默认实现 ---


def test_base_failover_with_config():
    """基类 should_trigger_failover 在有 FailoverConfig 时正确判断."""
    backend = AnthropicBackend(AnthropicConfig(), FailoverConfig(
        status_codes=[429],
        error_types=["rate_limit_error"],
        error_message_patterns=["quota"],
    ))
    # 匹配 status_code + error_type
    assert backend.should_trigger_failover(429, {
        "error": {"type": "rate_limit_error", "message": "test"}
    })
    # 匹配 status_code + error_message
    assert backend.should_trigger_failover(429, {
        "error": {"type": "unknown", "message": "Quota exceeded"}
    })
    # 429 无 body → 仍触发
    assert backend.should_trigger_failover(429, None)
    # status_code 不匹配
    assert not backend.should_trigger_failover(200, None)


def test_base_failover_without_config_returns_false():
    """无 FailoverConfig 时始终返回 False（终端后端行为）."""
    mapper = ModelMapper([])
    backend = ZhipuBackend(ZhipuConfig(), mapper)
    assert not backend.should_trigger_failover(429, None)
    assert not backend.should_trigger_failover(429, {
        "error": {"type": "rate_limit_error", "message": "limited"}
    })
    assert not backend.should_trigger_failover(503, None)


# --- _sanitize_headers_for_synthetic_response ---


def test_sanitize_headers_removes_encoding():
    """移除 content-encoding/content-length/transfer-encoding."""
    raw = httpx.Headers({
        "content-type": "application/json",
        "content-encoding": "gzip",
        "content-length": "123",
        "transfer-encoding": "chunked",
        "x-request-id": "abc",
    })
    result = _sanitize_headers_for_synthetic_response(raw)
    assert "content-type" in result
    assert "x-request-id" in result
    assert "content-encoding" not in result
    assert "content-length" not in result
    assert "transfer-encoding" not in result


def test_sanitize_headers_preserves_other():
    """非跳过头部全部保留."""
    raw = httpx.Headers({
        "retry-after": "60",
        "x-ratelimit-remaining": "0",
    })
    result = _sanitize_headers_for_synthetic_response(raw)
    assert result["retry-after"] == "60"
    assert result["x-ratelimit-remaining"] == "0"


def test_synthetic_response_no_decompression_error():
    """验证清洗后的头部构造 httpx.Response 不触发 zlib 解压错误."""
    # 这是原始 bug 的精确复现: 已解压的 content + gzip header → zlib error
    raw_headers = httpx.Headers({
        "content-type": "application/json",
        "content-encoding": "gzip",
    })
    clean_headers = _sanitize_headers_for_synthetic_response(raw_headers)
    # 使用已解压的 JSON 文本构造 Response — 不应抛异常
    resp = httpx.Response(
        429,
        content=b'{"error": "rate limit"}',
        headers=clean_headers,
        request=httpx.Request("POST", "https://api.example.com/v1/messages"),
    )
    assert resp.status_code == 429
    assert b"rate limit" in resp.content


def test_decode_json_body_returns_none_for_html():
    resp = httpx.Response(
        200,
        content=b"<html>not json</html>",
        headers={"content-type": "text/html"},
    )
    assert _decode_json_body(resp) is None


# --- check_health 测试 ---


@pytest.mark.asyncio
async def test_anthropic_check_health_returns_true():
    """Anthropic 透明代理策略：check_health 始终返回 True."""
    backend = AnthropicBackend(AnthropicConfig(), FailoverConfig())
    result = await backend.check_health()
    assert result is True


@pytest.mark.asyncio
async def test_antigravity_check_health_token_success():
    """Antigravity 健康检查：token 刷新成功 → True."""
    from unittest.mock import AsyncMock

    config = AntigravityConfig(
        client_id="cid", client_secret="csecret", refresh_token="rtoken",
    )
    backend = AntigravityBackend(config, FailoverConfig(), ModelMapper([]))
    # Mock token manager 返回有效 token
    backend._token_manager.get_token = AsyncMock(return_value="valid-token")

    result = await backend.check_health()
    assert result is True
    backend._token_manager.get_token.assert_awaited_once()


@pytest.mark.asyncio
async def test_antigravity_check_health_token_failure():
    """Antigravity 健康检查：token 刷新失败 → False."""
    from unittest.mock import AsyncMock

    config = AntigravityConfig(
        client_id="cid", client_secret="csecret", refresh_token="rtoken",
    )
    backend = AntigravityBackend(config, FailoverConfig(), ModelMapper([]))
    # Mock token manager 抛出异常
    backend._token_manager.get_token = AsyncMock(side_effect=Exception("refresh failed"))

    result = await backend.check_health()
    assert result is False
