"""后端基类与子类单元测试."""

from coding.proxy.backends.anthropic import AnthropicBackend
from coding.proxy.backends.base import BaseBackend, BackendResponse, UsageInfo
from coding.proxy.backends.zhipu import ZhipuBackend
from coding.proxy.config.schema import (
    AnthropicConfig,
    FailoverConfig,
    ModelMappingRule,
    ZhipuConfig,
)
from coding.proxy.routing.model_mapper import ModelMapper


def test_anthropic_prepare_request_filters_headers():
    backend = AnthropicBackend(AnthropicConfig(), FailoverConfig())
    body = {"model": "claude-sonnet-4-20250514", "messages": []}
    headers = {
        "authorization": "Bearer sk-test",
        "host": "localhost",
        "content-length": "100",
        "anthropic-version": "2023-06-01",
    }
    prepared_body, prepared_headers = backend._prepare_request(body, headers)
    assert prepared_body is body  # 不修改原始 body
    assert "host" not in prepared_headers
    assert "content-length" not in prepared_headers
    assert prepared_headers["authorization"] == "Bearer sk-test"
    assert prepared_headers["anthropic-version"] == "2023-06-01"


def test_zhipu_prepare_request_maps_model():
    mapper = ModelMapper([
        ModelMappingRule(pattern="claude-sonnet-.*", target="glm-5.1", is_regex=True),
    ])
    config = ZhipuConfig(api_key="test-key")
    backend = ZhipuBackend(config, mapper)

    body = {"model": "claude-sonnet-4-20250514", "messages": []}
    headers = {"anthropic-version": "2023-06-01"}
    prepared_body, prepared_headers = backend._prepare_request(body, headers)

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
