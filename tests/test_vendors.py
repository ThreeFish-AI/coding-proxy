"""供应商基类与子类单元测试."""

import httpx
import pytest

from coding.proxy.config.schema import (
    AnthropicConfig,
    AntigravityConfig,
    FailoverConfig,
    ModelMappingRule,
    ZhipuConfig,
)
from coding.proxy.routing.model_mapper import ModelMapper
from coding.proxy.vendors.anthropic import AnthropicVendor
from coding.proxy.vendors.antigravity import AntigravityVendor
from coding.proxy.vendors.base import (
    UsageInfo,
    VendorResponse,
)
from coding.proxy.vendors.base import (
    decode_json_body as _decode_json_body,
)
from coding.proxy.vendors.base import (
    sanitize_headers_for_synthetic_response as _sanitize_headers_for_synthetic_response,
)
from coding.proxy.vendors.zhipu import ZhipuVendor


@pytest.mark.asyncio
async def test_anthropic_prepare_request_filters_headers():
    vendor = AnthropicVendor(AnthropicConfig(), FailoverConfig())
    body = {"model": "claude-sonnet-4-20250514", "messages": []}
    headers = {
        "authorization": "Bearer sk-test",
        "host": "localhost",
        "content-length": "100",
        "anthropic-version": "2023-06-01",
    }
    prepared_body, prepared_headers = await vendor._prepare_request(body, headers)
    assert prepared_body is not body  # deepcopy: 返回新对象
    assert prepared_body == body  # 内容等价
    assert "host" not in prepared_headers
    assert "content-length" not in prepared_headers
    assert prepared_headers["authorization"] == "Bearer sk-test"
    assert prepared_headers["anthropic-version"] == "2023-06-01"


@pytest.mark.asyncio
async def test_anthropic_prepare_request_strips_thinking_blocks():
    """Anthropic vendor 应剥离 assistant messages 中的 thinking blocks."""
    vendor = AnthropicVendor(AnthropicConfig(), FailoverConfig())
    body = {
        "model": "claude-opus-4-6",
        "messages": [
            {
                "role": "assistant",
                "content": [
                    {
                        "type": "thinking",
                        "thinking": "Let me think about this...",
                        "signature": "zhipu-issued-signature",
                    },
                    {"type": "text", "text": "Here is my answer."},
                ],
            },
        ],
    }
    prepared_body, _ = await vendor._prepare_request(body, {})

    # thinking block 被剥离，text block 保留
    content = prepared_body["messages"][0]["content"]
    assert len(content) == 1
    assert content[0]["type"] == "text"
    assert content[0]["text"] == "Here is my answer."


@pytest.mark.asyncio
async def test_anthropic_prepare_request_strips_redacted_thinking_blocks():
    """Anthropic vendor 应剥离 assistant messages 中的 redacted_thinking blocks."""
    vendor = AnthropicVendor(AnthropicConfig(), FailoverConfig())
    body = {
        "model": "claude-opus-4-6",
        "messages": [
            {
                "role": "assistant",
                "content": [
                    {"type": "redacted_thinking", "data": "base64data"},
                    {"type": "text", "text": "response"},
                ],
            },
        ],
    }
    prepared_body, _ = await vendor._prepare_request(body, {})

    content = prepared_body["messages"][0]["content"]
    assert len(content) == 1
    assert content[0]["type"] == "text"


@pytest.mark.asyncio
async def test_anthropic_prepare_request_preserves_thinking_param():
    """body 顶层的 thinking 参数（控制当前请求是否启用 thinking）不应被修改."""
    vendor = AnthropicVendor(AnthropicConfig(), FailoverConfig())
    body = {
        "model": "claude-opus-4-6",
        "thinking": {"type": "enabled", "budget_tokens": 10000},
        "messages": [
            {
                "role": "assistant",
                "content": [
                    {"type": "thinking", "thinking": "old thought", "signature": "sig"},
                    {"type": "text", "text": "old response"},
                ],
            },
            {"role": "user", "content": "follow up"},
        ],
    }
    prepared_body, _ = await vendor._prepare_request(body, {})

    # 顶层 thinking 参数保留
    assert prepared_body["thinking"] == {"type": "enabled", "budget_tokens": 10000}
    # assistant message 的 thinking block 被剥离
    assert len(prepared_body["messages"][0]["content"]) == 1
    assert prepared_body["messages"][0]["content"][0]["type"] == "text"


@pytest.mark.asyncio
async def test_anthropic_prepare_request_does_not_mutate_original_body():
    """deepcopy 验证：原始 body 不应被修改."""
    vendor = AnthropicVendor(AnthropicConfig(), FailoverConfig())
    body = {
        "model": "claude-opus-4-6",
        "messages": [
            {
                "role": "assistant",
                "content": [
                    {"type": "thinking", "thinking": "thought", "signature": "sig"},
                    {"type": "text", "text": "response"},
                ],
            },
        ],
    }
    await vendor._prepare_request(body, {})

    # 原始 body 的 thinking block 应仍然存在
    assert len(body["messages"][0]["content"]) == 2
    assert body["messages"][0]["content"][0]["type"] == "thinking"


@pytest.mark.asyncio
async def test_anthropic_prepare_request_preserves_user_messages():
    """user messages 不应被修改（thinking blocks 仅出现在 assistant messages 中）."""
    vendor = AnthropicVendor(AnthropicConfig(), FailoverConfig())
    body = {
        "model": "claude-opus-4-6",
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "hello"},
                ],
            },
        ],
    }
    prepared_body, _ = await vendor._prepare_request(body, {})

    content = prepared_body["messages"][0]["content"]
    assert len(content) == 1
    assert content[0]["type"] == "text"


@pytest.mark.asyncio
async def test_anthropic_prepare_request_handles_string_content():
    """assistant message content 为字符串时应原样保留（不触发剥离逻辑）."""
    vendor = AnthropicVendor(AnthropicConfig(), FailoverConfig())
    body = {
        "model": "claude-opus-4-6",
        "messages": [
            {"role": "assistant", "content": "plain text response"},
        ],
    }
    prepared_body, _ = await vendor._prepare_request(body, {})
    assert prepared_body["messages"][0]["content"] == "plain text response"


@pytest.mark.asyncio
async def test_anthropic_prepare_request_thinking_only_gets_placeholder():
    """assistant message 仅含 thinking blocks 时，剥离后应插入占位 text block."""
    vendor = AnthropicVendor(AnthropicConfig(), FailoverConfig())
    body = {
        "model": "claude-opus-4-6",
        "messages": [
            {"role": "user", "content": "hello"},
            {
                "role": "assistant",
                "content": [
                    {
                        "type": "thinking",
                        "thinking": "Let me think...",
                        "signature": "zhipu-sig",
                    },
                ],
            },
            {"role": "user", "content": "follow up"},
        ],
    }
    prepared_body, _ = await vendor._prepare_request(body, {})

    content = prepared_body["messages"][1]["content"]
    assert len(content) == 1
    assert content[0]["type"] == "text"
    assert content[0]["text"] == "[thinking]"


@pytest.mark.asyncio
async def test_anthropic_prepare_request_thinking_only_with_tool_result_context():
    """多轮对话：thinking-only assistant + 后续 user tool_result 不应触发结构错误."""
    vendor = AnthropicVendor(AnthropicConfig(), FailoverConfig())
    body = {
        "model": "claude-opus-4-6",
        "messages": [
            {"role": "user", "content": "hello"},
            {
                "role": "assistant",
                "content": [
                    {
                        "type": "thinking",
                        "thinking": "thought 1",
                        "signature": "sig-1",
                    },
                    {"type": "redacted_thinking", "data": "base64data"},
                ],
            },
            {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "toolu_123",
                        "content": "result data",
                    },
                ],
            },
        ],
    }
    prepared_body, _ = await vendor._prepare_request(body, {})

    # assistant message 应包含占位 text block
    content = prepared_body["messages"][1]["content"]
    assert len(content) == 1
    assert content[0]["type"] == "text"
    assert content[0]["text"] == "[thinking]"

    # user message 的 tool_result 应完整保留
    user_content = prepared_body["messages"][2]["content"]
    assert user_content[0]["type"] == "tool_result"


@pytest.mark.asyncio
async def test_anthropic_prepare_request_multi_turn_strips_all_thinking():
    """多轮对话中所有 assistant thinking blocks 均应被剥离."""
    vendor = AnthropicVendor(AnthropicConfig(), FailoverConfig())
    body = {
        "model": "claude-opus-4-6",
        "messages": [
            {
                "role": "assistant",
                "content": [
                    {"type": "thinking", "thinking": "thought 1", "signature": "s1"},
                    {"type": "text", "text": "response 1"},
                ],
            },
            {"role": "user", "content": "follow up"},
            {
                "role": "assistant",
                "content": [
                    {"type": "thinking", "thinking": "thought 2", "signature": "s2"},
                    {"type": "text", "text": "response 2"},
                    {
                        "type": "tool_use",
                        "id": "toolu_123",
                        "name": "bash",
                        "input": {"command": "pwd"},
                    },
                ],
            },
        ],
    }
    prepared_body, _ = await vendor._prepare_request(body, {})

    # 第一条 assistant message: thinking 被剥离，text 保留
    content_0 = prepared_body["messages"][0]["content"]
    assert len(content_0) == 1
    assert content_0[0]["type"] == "text"

    # user message 不变
    assert prepared_body["messages"][1]["content"] == "follow up"

    # 第三条 assistant message: thinking 被剥离，text + tool_use 保留
    content_2 = prepared_body["messages"][2]["content"]
    assert len(content_2) == 2
    assert content_2[0]["type"] == "text"
    assert content_2[1]["type"] == "tool_use"


@pytest.mark.asyncio
async def test_zhipu_prepare_request_maps_model():
    mapper = ModelMapper(
        [
            ModelMappingRule(
                pattern="claude-sonnet-.*", target="glm-5.1", is_regex=True
            ),
        ]
    )
    config = ZhipuConfig(api_key="test-key")
    zhipu_vendor = ZhipuVendor(config, mapper)

    body = {"model": "claude-sonnet-4-20250514", "messages": []}
    headers = {"anthropic-version": "2023-06-01"}
    prepared_body, prepared_headers = await zhipu_vendor._prepare_request(body, headers)

    assert prepared_body["model"] == "glm-5.1"
    assert prepared_headers["x-api-key"] == "test-key"
    assert "model" in body  # 原始 body 未被修改
    assert body["model"] == "claude-sonnet-4-20250514"


@pytest.mark.asyncio
async def test_zhipu_prepare_request_uses_default_family_mapping():
    """空映射规则时回退到 ModelMapper 默认目标（glm-5.1）."""
    zhipu_vendor = ZhipuVendor(ZhipuConfig(api_key="test-key"), ModelMapper([]))

    sonnet_body = {"model": "claude-sonnet-4-20250514", "messages": []}
    haiku_body = {"model": "claude-haiku-4-5-20251001", "messages": []}

    prepared_sonnet, _ = await zhipu_vendor._prepare_request(sonnet_body, {})
    prepared_haiku, _ = await zhipu_vendor._prepare_request(haiku_body, {})

    # 无显式规则时，全部回退到 _DEFAULT_TARGET（glm-5.1）
    assert prepared_sonnet["model"] == "glm-5.1"
    assert prepared_haiku["model"] == "glm-5.1"


def test_anthropic_should_trigger_failover():
    failover = FailoverConfig(
        status_codes=[429, 503],
        error_types=["rate_limit_error"],
        error_message_patterns=["quota"],
    )
    anthropic_vendor = AnthropicVendor(AnthropicConfig(), failover)

    # 429 + rate_limit_error → True
    assert anthropic_vendor.should_trigger_failover(
        429, {"error": {"type": "rate_limit_error", "message": "Rate limited"}}
    )

    # 429 without body → True (429/503 always trigger)
    assert anthropic_vendor.should_trigger_failover(429, None)

    # 200 → False
    assert not anthropic_vendor.should_trigger_failover(200, None)

    # 500 not in status_codes → False
    assert not anthropic_vendor.should_trigger_failover(500, None)

    # error message pattern match
    assert anthropic_vendor.should_trigger_failover(
        429, {"error": {"type": "unknown", "message": "Quota exceeded"}}
    )


def test_zhipu_never_triggers_failover():
    mapper = ModelMapper([])
    zhipu_vendor = ZhipuVendor(ZhipuConfig(), mapper)
    assert not zhipu_vendor.should_trigger_failover(429, None)
    assert not zhipu_vendor.should_trigger_failover(
        500, {"error": {"type": "rate_limit_error"}}
    )


def test_zhipu_supports_tools_and_thinking():
    """ZhipuVendor 应声明全部能力为 NATIVE（原生 Anthropic 兼容端点）."""
    from coding.proxy.compat.canonical import CompatibilityStatus
    from coding.proxy.vendors.base import RequestCapabilities

    mapper = ModelMapper([])
    zhipu_vendor = ZhipuVendor(ZhipuConfig(), mapper)
    caps = zhipu_vendor.get_capabilities()
    assert caps.supports_tools is True
    assert caps.supports_thinking is True
    assert caps.emits_vendor_tool_events is False
    # 含工具的请求应被接受
    supported, reasons = zhipu_vendor.supports_request(
        RequestCapabilities(has_tools=True)
    )
    assert supported is True
    assert reasons == []
    # 含 thinking 的请求应被接受
    supported, reasons = zhipu_vendor.supports_request(
        RequestCapabilities(has_thinking=True)
    )
    assert supported is True
    assert reasons == []
    # 兼容性画像应全部为 NATIVE
    profile = zhipu_vendor.get_compatibility_profile()
    assert profile.thinking is CompatibilityStatus.NATIVE
    assert profile.tool_calling is CompatibilityStatus.NATIVE
    assert profile.tool_streaming is CompatibilityStatus.NATIVE
    assert profile.images is CompatibilityStatus.NATIVE
    assert profile.metadata is CompatibilityStatus.NATIVE


@pytest.mark.asyncio
async def test_zhipu_prepare_request_preserves_metadata():
    """ZhipuVendor._prepare_request 应原样保留 metadata 字段（原生端点支持）."""
    mapper = ModelMapper([])
    zhipu_vendor = ZhipuVendor(ZhipuConfig(api_key="sk-test"), mapper)
    body = {
        "model": "claude-sonnet-4-20250514",
        "messages": [],
        "metadata": {"user_id": "u123"},
    }
    prepared_body, _ = await zhipu_vendor._prepare_request(body, {})
    # metadata 原样透传，不再剥离或投影
    assert "metadata" in prepared_body
    assert prepared_body["metadata"] == {"user_id": "u123"}
    # 原始 body 不应被修改
    assert body["metadata"] == {"user_id": "u123"}


@pytest.mark.asyncio
async def test_zhipu_prepare_request_preserves_thinking():
    """ZhipuVendor._prepare_request 应原样保留 thinking 字段（原生端点支持）."""
    mapper = ModelMapper([])
    zhipu_vendor = ZhipuVendor(ZhipuConfig(api_key="sk-test"), mapper)
    body = {
        "model": "claude-sonnet-4-20250514",
        "messages": [],
        "thinking": {"type": "enabled", "budget_tokens": 10000},
    }
    prepared_body, _ = await zhipu_vendor._prepare_request(body, {})
    # thinking 原样透传，不再剥离任何字段
    assert prepared_body["thinking"] == {"type": "enabled", "budget_tokens": 10000}
    # 原始 body 不应被修改
    assert body["thinking"]["budget_tokens"] == 10000


@pytest.mark.asyncio
async def test_zhipu_prepare_request_preserves_anthropic_beta_header():
    zhipu_vendor = ZhipuVendor(ZhipuConfig(api_key="sk-test"), ModelMapper([]))
    body = {"model": "claude-opus-4-6", "messages": []}
    _, prepared_headers = await zhipu_vendor._prepare_request(
        body,
        {
            "anthropic-version": "2023-06-01",
            "anthropic-beta": "computer-use-2025-01-24",
            "x-request-id": "req-1",
        },
    )
    assert prepared_headers["anthropic-beta"] == "computer-use-2025-01-24"
    assert prepared_headers["x-request-id"] == "req-1"


@pytest.mark.parametrize(
    ("tool_name", "input_payload"),
    [
        (
            "Task",
            {"description": "sub task", "prompt": "do it", "subagent_type": "general"},
        ),
        ("Bash", {"command": "pwd", "description": "check cwd"}),
        ("Grep", {"pattern": "TODO", "path": "."}),
        ("Glob", {"pattern": "**/*.py", "path": "."}),
        ("Edit", {"file_path": "a.py", "old_string": "x", "new_string": "y"}),
        ("Write", {"file_path": "a.py", "content": "print('x')\n"}),
        ("Read", {"file_path": "a.py", "offset": 1, "limit": 10}),
        (
            "TodoWrite",
            {"todos": [{"content": "task-1", "status": "pending", "priority": "high"}]},
        ),
    ],
)
@pytest.mark.asyncio
async def test_zhipu_prepare_request_preserves_claude_code_tool_shapes(
    tool_name, input_payload
):
    zhipu_vendor = ZhipuVendor(ZhipuConfig(api_key="sk-test"), ModelMapper([]))
    body = {
        "model": "claude-opus-4-6",
        "messages": [
            {
                "role": "assistant",
                "content": [
                    {
                        "type": "tool_use",
                        "id": "toolu_1",
                        "name": tool_name,
                        "input": input_payload,
                    }
                ],
            }
        ],
        "tools": [
            {
                "name": tool_name,
                "input_schema": {
                    "type": "object",
                    "properties": {"payload": {"type": "string"}},
                },
            }
        ],
        "tool_choice": {"type": "any"},
    }

    prepared_body, prepared_headers = await zhipu_vendor._prepare_request(
        body, {"anthropic-beta": "code-tools-1"}
    )

    assert prepared_body["tools"][0]["name"] == tool_name
    assert prepared_body["messages"][0]["content"][0]["name"] == tool_name
    assert prepared_body["messages"][0]["content"][0]["input"] == input_payload
    assert prepared_headers["anthropic-beta"] == "code-tools-1"
    # 工具列表原样透传，不做任何截断或修改
    assert len(prepared_body["tools"]) == 1


@pytest.mark.asyncio
async def test_zhipu_send_message_normalizes_401_auth_error():
    """ZhipuVendor._normalize_error_response 钩子将 401 错误类型归一化为 authentication_error."""
    zhipu_vendor = ZhipuVendor(ZhipuConfig(api_key="sk-test"), ModelMapper([]))

    raw_body = '{"error":{"type":"401","message":"令牌已过期或验证不正确"}}'.encode()
    input_resp = VendorResponse(
        status_code=401,
        raw_body=raw_body,
        error_type="401",
        error_message="令牌已过期或验证不正确",
        response_headers={"content-type": "application/json"},
    )

    # 直接测试 _normalize_error_response 钩子（无需 mock 基类 send_message）
    result = zhipu_vendor._normalize_error_response(
        401, httpx.Response(401, content=raw_body), input_resp
    )

    assert result.status_code == 401
    assert result.error_type == "authentication_error"
    assert result.error_message == "令牌已过期或验证不正确"
    assert b'"authentication_error"' in result.raw_body


def test_zhipu_normalize_error_response_passthrough_non_401():
    """非 401 状态码应透传原始响应，不做归一化."""
    zhipu_vendor = ZhipuVendor(ZhipuConfig(api_key="sk-test"), ModelMapper([]))

    input_resp = VendorResponse(
        status_code=429,
        raw_body=b'{"error":{"type":"rate_limit_error","message":"Too many requests"}}',
        error_type="rate_limit_error",
        error_message="Too many requests",
    )

    result = zhipu_vendor._normalize_error_response(
        429, httpx.Response(429, content=input_resp.raw_body), input_resp
    )

    assert result.error_type == "rate_limit_error"
    assert result is input_resp  # 透传：返回同一对象


@pytest.mark.asyncio
async def test_zhipu_send_message_without_api_key_fails_fast():
    zhipu_vendor = ZhipuVendor(ZhipuConfig(api_key=""), ModelMapper([]))

    resp = await zhipu_vendor.send_message(
        {"model": "claude-opus-4-6", "messages": {}}, {}
    )

    assert resp.status_code == 401
    assert resp.error_type == "authentication_error"
    assert "API key 未配置" in (resp.error_message or "")


def test_zhipu_normalize_backend_error_accepts_raw_bytes():
    zhipu_vendor = ZhipuVendor(ZhipuConfig(api_key="sk-test"), ModelMapper([]))

    raw_body, payload = zhipu_vendor._normalize_backend_error(
        401,
        '{"error":{"type":"401","message":"令牌已过期或验证不正确"}}'.encode(),
    )

    assert payload is not None
    assert payload["error"]["type"] == "authentication_error"
    assert payload["error"]["message"] == "令牌已过期或验证不正确"
    assert b'"authentication_error"' in raw_body


def test_vendor_response_defaults():
    resp = VendorResponse()
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
    anthropic_vendor = AnthropicVendor(
        AnthropicConfig(),
        FailoverConfig(
            status_codes=[429],
            error_types=["rate_limit_error"],
            error_message_patterns=["quota"],
        ),
    )
    # 匹配 status_code + error_type
    assert anthropic_vendor.should_trigger_failover(
        429, {"error": {"type": "rate_limit_error", "message": "test"}}
    )
    # 匹配 status_code + error_message
    assert anthropic_vendor.should_trigger_failover(
        429, {"error": {"type": "unknown", "message": "Quota exceeded"}}
    )
    # 429 无 body → 仍触发
    assert anthropic_vendor.should_trigger_failover(429, None)
    # status_code 不匹配
    assert not anthropic_vendor.should_trigger_failover(200, None)


def test_base_failover_without_config_returns_false():
    """无 FailoverConfig 时始终返回 False（终端供应商行为）."""
    mapper = ModelMapper([])
    zhipu_vendor = ZhipuVendor(ZhipuConfig(), mapper)
    assert not zhipu_vendor.should_trigger_failover(429, None)
    assert not zhipu_vendor.should_trigger_failover(
        429, {"error": {"type": "rate_limit_error", "message": "limited"}}
    )
    assert not zhipu_vendor.should_trigger_failover(503, None)


# --- 529 overloaded_error 降级测试 ---


def test_529_overloaded_triggers_failover():
    """529 + overloaded_error 应触发降级（FailoverConfig 默认包含 529）."""
    anthropic_vendor = AnthropicVendor(AnthropicConfig(), FailoverConfig())

    assert anthropic_vendor.should_trigger_failover(
        529, {"error": {"type": "overloaded_error", "message": "Overloaded"}}
    )


def test_529_without_body_triggers_failover():
    """529 无 body 也应触发降级（备用安全网逻辑）."""
    anthropic_vendor = AnthropicVendor(AnthropicConfig(), FailoverConfig())

    assert anthropic_vendor.should_trigger_failover(529, None)


def test_529_in_failover_config_default():
    """验证 FailoverConfig 默认 status_codes 包含 529."""
    config = FailoverConfig()
    assert 529 in config.status_codes


# --- _sanitize_headers_for_synthetic_response ---


def test_sanitize_headers_removes_encoding():
    """移除 content-encoding/content-length/transfer-encoding."""
    raw = httpx.Headers(
        {
            "content-type": "application/json",
            "content-encoding": "gzip",
            "content-length": "123",
            "transfer-encoding": "chunked",
            "x-request-id": "abc",
        }
    )
    result = _sanitize_headers_for_synthetic_response(raw)
    assert "content-type" in result
    assert "x-request-id" in result
    assert "content-encoding" not in result
    assert "content-length" not in result
    assert "transfer-encoding" not in result


def test_sanitize_headers_preserves_other():
    """非跳过头部全部保留."""
    raw = httpx.Headers(
        {
            "retry-after": "60",
            "x-ratelimit-remaining": "0",
        }
    )
    result = _sanitize_headers_for_synthetic_response(raw)
    assert result["retry-after"] == "60"
    assert result["x-ratelimit-remaining"] == "0"


def test_synthetic_response_no_decompression_error():
    """验证清洗后的头部构造 httpx.Response 不触发 zlib 解压错误."""
    # 这是原始 bug 的精确复现: 已解压的 content + gzip header → zlib error
    raw_headers = httpx.Headers(
        {
            "content-type": "application/json",
            "content-encoding": "gzip",
        }
    )
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
    anthropic_vendor = AnthropicVendor(AnthropicConfig(), FailoverConfig())
    result = await anthropic_vendor.check_health()
    assert result is True


@pytest.mark.asyncio
async def test_antigravity_check_health_token_success():
    """Antigravity 健康检查：token 刷新成功 → True."""
    from unittest.mock import AsyncMock

    config = AntigravityConfig(
        client_id="cid",
        client_secret="csecret",
        refresh_token="rtoken",
    )
    antigravity_vendor = AntigravityVendor(config, FailoverConfig(), ModelMapper([]))
    # Mock token manager 返回有效 token
    antigravity_vendor._token_manager.get_token = AsyncMock(return_value="valid-token")

    result = await antigravity_vendor.check_health()
    assert result is True
    antigravity_vendor._token_manager.get_token.assert_awaited_once()


@pytest.mark.asyncio
async def test_antigravity_check_health_token_failure():
    """Antigravity 健康检查：token 刷新失败 → False."""
    from unittest.mock import AsyncMock

    config = AntigravityConfig(
        client_id="cid",
        client_secret="csecret",
        refresh_token="rtoken",
    )
    antigravity_vendor = AntigravityVendor(config, FailoverConfig(), ModelMapper([]))
    # Mock token manager 抛出异常
    antigravity_vendor._token_manager.get_token = AsyncMock(
        side_effect=Exception("refresh failed")
    )

    result = await antigravity_vendor.check_health()
    assert result is False
