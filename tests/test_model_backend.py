"""coding.proxy.model.backend 数据类型、常量与工具函数单元测试.

覆盖范围:
- UsageInfo / CapabilityLossReason / RequestCapabilities / BackendCapabilities
- BackendResponse / NoCompatibleBackendError
- CopilotMisdirectedRequest / CopilotExchangeDiagnostics / CopilotModelCatalog
- sanitize_headers_for_synthetic_response / decode_json_body / extract_error_message
- PROXY_SKIP_HEADERS / RESPONSE_SANITIZE_SKIP_HEADERS 常量
"""

import httpx
import pytest

from coding.proxy.model.backend import (
    BackendCapabilities,
    BackendResponse,
    CapabilityLossReason,
    CopilotExchangeDiagnostics,
    CopilotMisdirectedRequest,
    CopilotModelCatalog,
    NoCompatibleBackendError,
    RequestCapabilities,
    UsageInfo,
    decode_json_body,
    extract_error_message,
    sanitize_headers_for_synthetic_response,
)
from coding.proxy.model.constants import (
    PROXY_SKIP_HEADERS,
    RESPONSE_SANITIZE_SKIP_HEADERS,
)


# ═══════════════════════════════════════════════════════════════
# 1. UsageInfo — 默认值 & 自定义构造
# ═══════════════════════════════════════════════════════════════


class TestUsageInfo:
    """UsageInfo dataclass 测试."""

    def test_defaults_all_zero_and_empty(self):
        """默认构造: 所有数值字段为 0, request_id 为空字符串."""
        usage = UsageInfo()
        assert usage.input_tokens == 0
        assert usage.output_tokens == 0
        assert usage.cache_creation_tokens == 0
        assert usage.cache_read_tokens == 0
        assert usage.request_id == ""

    def test_custom_values(self):
        """自定义值构造: 各字段正确赋值."""
        usage = UsageInfo(
            input_tokens=100,
            output_tokens=50,
            cache_creation_tokens=10,
            cache_read_tokens=20,
            request_id="req_abc123",
        )
        assert usage.input_tokens == 100
        assert usage.output_tokens == 50
        assert usage.cache_creation_tokens == 10
        assert usage.cache_read_tokens == 20
        assert usage.request_id == "req_abc123"

    def test_partial_construction(self):
        """部分字段构造: 未指定字段取默认值."""
        usage = UsageInfo(input_tokens=42, request_id="req_partial")
        assert usage.input_tokens == 42
        assert usage.request_id == "req_partial"
        assert usage.output_tokens == 0
        assert usage.cache_creation_tokens == 0
        assert usage.cache_read_tokens == 0

    def test_mutable(self):
        """非 frozen dataclass, 字段可修改."""
        usage = UsageInfo()
        usage.input_tokens = 999
        usage.request_id = "modified"
        assert usage.input_tokens == 999
        assert usage.request_id == "modified"

    def test_equality(self):
        """相同字段值的两实例相等."""
        a = UsageInfo(input_tokens=1, output_tokens=2)
        b = UsageInfo(input_tokens=1, output_tokens=2)
        assert a == b

    def test_edge_case_large_values(self):
        """大数值边界: 不溢出或截断."""
        usage = UsageInfo(
            input_tokens=2**31 - 1,
            output_tokens=2**63 - 1,
            cache_creation_tokens=0,
            cache_read_tokens=0,
            request_id="",
        )
        assert usage.input_tokens == 2**31 - 1
        assert usage.output_tokens == 2**63 - 1


# ═══════════════════════════════════════════════════════════════
# 2. CapabilityLossReason 枚举
# ═══════════════════════════════════════════════════════════════


class TestCapabilityLossReason:
    """CapabilityLossReason Enum 测试."""

    def test_all_member_values(self):
        """枚举成员及其 value 与定义一致."""
        assert CapabilityLossReason.TOOLS.value == "tools"
        assert CapabilityLossReason.THINKING.value == "thinking"
        assert CapabilityLossReason.IMAGES.value == "images"
        assert CapabilityLossReason.VENDOR_TOOLS.value == "vendor_tools"
        assert CapabilityLossReason.METADATA.value == "metadata"

    def test_member_count(self):
        """枚举成员数量固定为 5."""
        assert len(CapabilityLossReason) == 5

    def test_can_iterate(self):
        """可遍历所有成员."""
        names = {m.name for m in CapabilityLossReason}
        assert names == {"TOOLS", "THINKING", "IMAGES", "VENDOR_TOOLS", "METADATA"}

    def test_lookup_by_value(self):
        """可通过 value 反查成员."""
        assert CapabilityLossReason("tools") is CapabilityLossReason.TOOLS
        assert CapabilityLossReason("metadata") is CapabilityLossReason.METADATA


# ═══════════════════════════════════════════════════════════════
# 3. RequestCapabilities (frozen dataclass)
# ═══════════════════════════════════════════════════════════════


class TestRequestCapabilities:
    """RequestCapabilities frozen dataclass 测试."""

    def test_defaults_all_false(self):
        """默认构造: 所有能力标志均为 False."""
        caps = RequestCapabilities()
        assert caps.has_tools is False
        assert caps.has_thinking is False
        assert caps.has_images is False
        assert caps.has_metadata is False

    def test_custom_true_values(self):
        """自定义构造: 指定 True 的字段正确赋值."""
        caps = RequestCapabilities(has_tools=True, has_images=True)
        assert caps.has_tools is True
        assert caps.has_images is True
        assert caps.has_thinking is False
        assert caps.has_metadata is False

    def test_frozen_immutable(self):
        """frozen dataclass: 赋值操作抛 AttributeError."""
        caps = RequestCapabilities()
        with pytest.raises(AttributeError):
            caps.has_tools = True  # type: ignore[misc]

    def test_hashable(self):
        """frozen dataclass 可哈希, 可放入 set / dict key."""
        a = RequestCapabilities(has_tools=True)
        b = RequestCapabilities(has_tools=True)
        c = RequestCapabilities()
        assert hash(a) == hash(b)
        assert {a, b, c}  # 不抛 TypeError

    def test_all_enabled(self):
        """全量启用场景."""
        caps = RequestCapabilities(
            has_tools=True, has_thinking=True, has_images=True, has_metadata=True,
        )
        assert all([caps.has_tools, caps.has_thinking, caps.has_images, caps.has_metadata])


# ═══════════════════════════════════════════════════════════════
# 4. BackendCapabilities (frozen dataclass)
# ═══════════════════════════════════════════════════════════════


class TestBackendCapabilities:
    """BackendCapabilities frozen dataclass 测试."""

    def test_defaults_mostly_true(self):
        """默认构造: 大部分支持标志为 True, emits_vendor_tool_events 为 False."""
        caps = BackendCapabilities()
        assert caps.supports_tools is True
        assert caps.supports_thinking is True
        assert caps.supports_images is True
        assert caps.emits_vendor_tool_events is False
        assert caps.supports_metadata is True

    def test_custom_minimal_backend(self):
        """最小化后端: 仅保留基础能力."""
        caps = BackendCapabilities(
            supports_tools=False,
            supports_thinking=False,
            supports_images=False,
            supports_metadata=False,
        )
        assert caps.supports_tools is False
        assert caps.supports_thinking is False
        assert caps.supports_images is False
        assert caps.supports_metadata is False
        assert caps.emits_vendor_tool_events is False

    def test_frozen_immutable(self):
        """frozen: 赋值抛 AttributeError."""
        caps = BackendCapabilities()
        with pytest.raises(AttributeError):
            caps.supports_tools = False  # type: ignore[misc]

    def test_emits_vendor_tool_events_true(self):
        """emits_vendor_tool_events 可设为 True."""
        caps = BackendCapabilities(emits_vendor_tool_events=True)
        assert caps.emits_vendor_tool_events is True


# ═══════════════════════════════════════════════════════════════
# 5. BackendResponse
# ═══════════════════════════════════════════════════════════════


class TestBackendResponse:
    """BackendResponse dataclass 测试."""

    def test_defaults(self):
        """默认构造: status_code=200, 空错误, 默认 usage, 空 headers."""
        resp = BackendResponse()
        assert resp.status_code == 200
        assert isinstance(resp.usage, UsageInfo)
        assert resp.is_streaming is False
        assert resp.raw_body == b"{}"
        assert resp.error_type is None
        assert resp.error_message is None
        assert resp.model_served is None
        assert resp.response_headers == {}

    def test_error_response(self):
        """错误响应构造: 含 error_type / error_message / 非 200 status_code."""
        resp = BackendResponse(
            status_code=429,
            error_type="rate_limit",
            error_message="Too many requests",
            model_served=None,
        )
        assert resp.status_code == 429
        assert resp.error_type == "rate_limit"
        assert resp.error_message == "Too many requests"
        assert resp.model_served is None

    def test_with_usage_and_headers(self):
        """含自定义 usage 和 response_headers 的完整构造."""
        usage = UsageInfo(input_tokens=10, output_tokens=5, request_id="r1")
        resp = BackendResponse(
            status_code=201,
            usage=usage,
            raw_body=b'{"ok":true}',
            model_served="claude-3-opus",
            response_headers={"x-request-id": "abc"},
        )
        assert resp.usage.input_tokens == 10
        assert resp.raw_body == b'{"ok":true}'
        assert resp.model_served == "claude-3-opus"
        assert resp.response_headers["x-request-id"] == "abc"

    def test_streaming_response(self):
        """流式响应标记."""
        resp = BackendResponse(is_streaming=True, raw_body=b"")
        assert resp.is_streaming is True
        assert resp.raw_body == b""

    def test_mutable_fields(self):
        """非 frozen: 可修改字段."""
        resp = BackendResponse()
        resp.status_code = 500
        resp.error_type = "internal"
        assert resp.status_code == 500
        assert resp.error_type == "internal"


# ═══════════════════════════════════════════════════════════════
# 6. NoCompatibleBackendError
# ═══════════════════════════════════════════════════════════════


class TestNoCompatibleBackendError:
    """NoCompatibleBackendError 异常类测试."""

    def test_with_reasons(self):
        """带 reasons 列表构造: message 与 reasons 均可访问."""
        err = NoCompatibleBackendError("no backend available", reasons=["tools", "thinking"])
        assert str(err) == "no backend available"
        assert err.reasons == ["tools", "thinking"]

    def test_without_reasons_defaults_to_empty_list(self):
        """不传 reasons: 默认为空列表而非 None."""
        err = NoCompatibleBackendError("no backend")
        assert err.reasons == []
        assert isinstance(err.reasons, list)

    def test_is_runtime_error_subclass(self):
        """继承自 RuntimeError, 可用 except RuntimeError 捕获."""
        err = NoCompatibleBackendError("test")
        assert isinstance(err, RuntimeError)
        assert isinstance(err, Exception)

    def test_empty_reasons_explicit_none(self):
        """显式传 reasons=None: 同样归一化为空列表."""
        err = NoCompatibleBackendError("msg", reasons=None)
        assert err.reasons == []


# ═══════════════════════════════════════════════════════════════
# 7. CopilotMisdirectedRequest
# ═══════════════════════════════════════════════════════════════


class TestCopilotMisdirectedRequest:
    """CopilotMisdirectedRequest dataclass 测试."""

    def test_construction(self):
        """基本构造: 所有必填字段均可传入."""
        req = httpx.Request("POST", "https://api.example.com/chat")
        hdrs = httpx.Headers({"content-type": "application/json"})
        diag = CopilotMisdirectedRequest(
            base_url="https://api.example.com",
            status_code=421,
            request=req,
            headers=hdrs,
            body=b'{"error":"misdirected"}',
        )
        assert diag.base_url == "https://api.example.com"
        assert diag.status_code == 421
        assert diag.request is req
        assert diag.headers is hdrs
        assert diag.body == b'{"error":"misdirected"}'

    def test_mutable(self):
        """非 frozen: 可修改字段."""
        diag = CopilotMisdirectedRequest(
            base_url="", status_code=0, request=None, headers=None, body=b"",
        )
        diag.base_url = "https://new.example.com"
        diag.status_code = 502
        assert diag.base_url == "https://new.example.com"
        assert diag.status_code == 502


# ═══════════════════════════════════════════════════════════════
# 8. CopilotExchangeDiagnostics.to_dict()
# ═══════════════════════════════════════════════════════════════


class TestCopilotExchangeDiagnostics:
    """CopilotExchangeDiagnostics 及 to_dict() 方法测试."""

    def test_to_dict_empty_defaults(self):
        """全部默认值 (零/空): to_dict() 返回空字典."""
        diag = CopilotExchangeDiagnostics()
        result = diag.to_dict()
        assert result == {}

    def test_to_dict_populated_all_fields(self):
        """全部字段有值: to_dict() 包含所有键, 含 ttl_seconds."""
        import time as _time

        now = int(_time.time())
        diag = CopilotExchangeDiagnostics(
            raw_shape="Bearer ...",
            token_field="access_token",
            expires_in_seconds=1800,
            expires_at_unix=now + 1800,
            capabilities={"models": ["gpt-4"]},
            updated_at_unix=now,
        )
        result = diag.to_dict()
        assert result["raw_shape"] == "Bearer ..."
        assert result["token_field"] == "access_token"
        assert result["expires_in_seconds"] == 1800
        assert result["expires_at_unix"] == now + 1800
        assert "ttl_seconds" in result
        assert result["capabilities"] == {"models": ["gpt-4"]}
        assert result["updated_at_unix"] == now

    def test_to_dict_ttl_seconds_non_negative(self):
        """ttl_seconds 始终 >= 0 (max(..., 0) 保护)."""
        import time as _time

        # 设置一个已过期的 expires_at_unix
        past_time = int(_time.time()) - 100
        diag = CopilotExchangeDiagnostics(expires_at_unix=past_time)
        result = diag.to_dict()
        assert result["ttl_seconds"] >= 0

    def test_to_dict_selective_fields(self):
        """仅部分字段有值: 只输出非零/非空的字段."""
        diag = CopilotExchangeDiagnostics(raw_shape="test_shape")
        result = diag.to_dict()
        assert set(result.keys()) == {"raw_shape"}

    def test_to_dict_capabilities_preserved_as_is(self):
        """capabilities 字典原样透传, 不做过滤."""
        caps = {"key": [1, 2, 3], "nested": {"a": "b"}}
        diag = CopilotExchangeDiagnostics(capabilities=caps)
        result = diag.to_dict()
        assert result["capabilities"] is caps

    def test_mutable(self):
        """非 frozen: 可修改字段."""
        diag = CopilotExchangeDiagnostics()
        diag.raw_shape = "new shape"
        diag.expires_in_seconds = 3600
        assert diag.raw_shape == "new shape"
        assert diag.expires_in_seconds == 3600


# ═══════════════════════════════════════════════════════════════
# 9. CopilotModelCatalog.age_seconds()
# ═══════════════════════════════════════════════════════════════


class TestCopilotModelCatalog:
    """CopilotModelCatalog 及 age_seconds() 方法测试."""

    def test_age_seconds_when_fetched_at_is_zero(self):
        """fetched_at_unix=0: age_seconds() 返回 None (未获取)."""
        catalog = CopilotModelCatalog(fetched_at_unix=0)
        assert catalog.age_seconds() is None

    def test_age_seconds_when_fetched_at_is_set(self):
        """fetched_at_unix 有值: age_seconds() 返回非负整数."""
        import time as _time

        now = int(_time.time())
        catalog = CopilotModelCatalog(fetched_at_unix=now)
        age = catalog.age_seconds()
        assert isinstance(age, int)
        assert age >= 0

    def test_age_seconds_non_negative(self):
        """age_seconds 始终 >= 0 (max 保护)."""
        import time as _time

        future_time = int(_time.time()) + 10000
        catalog = CopilotModelCatalog(fetched_at_unix=future_time)
        assert catalog.age_seconds() == 0

    def test_default_available_models_is_empty_list(self):
        """默认 available_models 为空列表."""
        catalog = CopilotModelCatalog()
        assert catalog.available_models == []

    def test_with_models(self):
        """含模型列表的构造."""
        catalog = CopilotModelCatalog(
            available_models=["gpt-4o", "claude-sonnet-4"],
            fetched_at_unix=1740000000,
        )
        assert len(catalog.available_models) == 2
        assert "gpt-4o" in catalog.available_models
        assert catalog.fetched_at_unix == 1740000000

    def test_mutable(self):
        """非 frozen: 可修改字段."""
        catalog = CopilotModelCatalog()
        catalog.available_models.append("model-x")
        catalog.fetched_at_unix = 999
        assert "model-x" in catalog.available_models
        assert catalog.fetched_at_unix == 999


# ═══════════════════════════════════════════════════════════════
# 10. 常量 PROXY_SKIP_HEADERS / RESPONSE_SANITIZE_SKIP_HEADERS
# ═══════════════════════════════════════════════════════════════


class TestConstants:
    """跨模块共享常量测试."""

    def test_proxy_skip_headers_is_frozenset(self):
        """PROXY_SKIP_HEADERS 类型为 frozenset."""
        assert isinstance(PROXY_SKIP_HEADERS, frozenset)

    def test_proxy_skip_headers_members(self):
        """包含 hop-by-hop 头部."""
        assert PROXY_SKIP_HEADERS == frozenset({
            "host", "content-length", "transfer-encoding", "connection",
        })

    def test_response_sanitize_skip_headers_is_frozenset(self):
        """RESPONSE_SANITIZE_SKIP_HEADERS 类型为 frozenset."""
        assert isinstance(RESPONSE_SANITIZE_SKIP_HEADERS, frozenset)

    def test_response_sanitize_skip_headers_members(self):
        """包含需移除的合成响应头部."""
        assert RESPONSE_SANITIZE_SKIP_HEADERS == frozenset({
            "content-encoding", "content-length", "transfer-encoding",
        })

    def test_frozenset_immutability(self):
        """frozenset 不可变: add 抛 AttributeError."""
        with pytest.raises(AttributeError):
            PROXY_SKIP_HEADERS.add("x-custom")  # type: ignore[func-returns-value]


# ═══════════════════════════════════════════════════════════════
# 11. sanitize_headers_for_synthetic_response
# ═══════════════════════════════════════════════════════════════


class TestSanitizeHeadersForSyntheticResponse:
    """sanitize_headers_for_synthetic_response 工具函数测试."""

    def test_removes_content_encoding_and_length(self):
        """移除 content-encoding / content-length / transfer-encoding."""
        raw = httpx.Headers({
            "content-type": "application/json",
            "content-encoding": "gzip",
            "content-length": "123",
            "transfer-encoding": "chunked",
            "x-request-id": "abc",
        })
        result = sanitize_headers_for_synthetic_response(raw)
        assert "content-type" in result
        assert "x-request-id" in result
        assert "content-encoding" not in result
        assert "content-length" not in result
        assert "transfer-encoding" not in result

    def test_preserves_other_headers(self):
        """不匹配跳过集合的头部原样保留."""
        raw = httpx.Headers({
            "retry-after": "60",
            "x-ratelimit-remaining": "0",
            "content-type": "text/event-stream",
        })
        result = sanitize_headers_for_synthetic_response(raw)
        assert result["retry-after"] == "60"
        assert result["x-ratelimit-remaining"] == "0"
        assert result["content-type"] == "text/event-stream"

    def test_empty_headers(self):
        """空 httpx.Headers 输入返回空字典."""
        raw = httpx.Headers({})
        result = sanitize_headers_for_synthetic_response(raw)
        assert result == {}

    def test_returns_plain_dict_not_httpx_headers(self):
        """返回值为普通 dict, 非 httpx.Headers."""
        raw = httpx.Headers({"x-key": "val"})
        result = sanitize_headers_for_synthetic_response(raw)
        assert isinstance(result, dict)
        assert not isinstance(result, httpx.Headers)

    def test_case_insensitive_matching(self):
        """头部名匹配大小写不敏感 (httpx.Headers 内部统一小写)."""
        raw = httpx.Headers({
            "Content-Encoding": "gzip",
            "Content-Length": "42",
            "Transfer-Encoding": "chunked",
        })
        result = sanitize_headers_for_synthetic_response(raw)
        assert "content-encoding" not in result
        assert "content-length" not in result
        assert "transfer-encoding" not in result

    def test_no_decompression_error_on_synthetic_response(self):
        """清洗后头部可用于构造 httpx.Response 且不触发解压错误."""
        raw_headers = httpx.Headers({
            "content-type": "application/json",
            "content-encoding": "gzip",
        })
        clean = sanitize_headers_for_synthetic_response(raw_headers)
        resp = httpx.Response(
            429,
            content=b'{"error": "rate limit"}',
            headers=clean,
            request=httpx.Request("POST", "https://api.example.com/v1/messages"),
        )
        assert resp.status_code == 429
        assert b"rate limit" in resp.content


# ═══════════════════════════════════════════════════════════════
# 12. decode_json_body
# ═══════════════════════════════════════════════════════════════


class TestDecodeJsonBody:
    """decode_json_body 安全 JSON 解析工具函数测试."""

    def test_valid_json_with_json_content_type(self):
        """标准 JSON content-type + 合法 JSON → 解析成功."""
        resp = httpx.Response(200, content=b'{"key":"value"}', headers={"content-type": "application/json"})
        assert decode_json_body(resp) == {"key": "value"}

    def test_valid_json_array(self):
        """合法 JSON 数组 → 返回 list."""
        resp = httpx.Response(200, content=b'[1,2,3]', headers={"content-type": "application/json"})
        assert decode_json_body(resp) == [1, 2, 3]

    def test_empty_content_returns_none(self):
        """空 body → None."""
        resp = httpx.Response(200, content=b"", headers={"content-type": "application/json"})
        assert decode_json_body(resp) is None

    def test_invalid_json_returns_none(self):
        """非法 JSON 内容 → None (安全降级)."""
        resp = httpx.Response(200, content=b"{invalid json", headers={"content-type": "application/json"})
        assert decode_json_body(resp) is None

    def test_html_content_type_returns_none(self):
        """text/html content-type + HTML 内容 → 无法解析为 JSON → None."""
        resp = httpx.Response(200, content=b"<html>not json</html>", headers={"content-type": "text/html"})
        assert decode_json_body(resp) is None

    def test_non_json_content_type_with_valid_json_body(self):
        """text/plain 但内容是合法 JSON → 尝试解析并返回结果."""
        resp = httpx.Response(200, content=b'{"ok":true}', headers={"content-type": "text/plain"})
        assert decode_json_body(resp) == {"ok": True}

    def test_non_json_content_type_with_invalid_body(self):
        """text/plain + 非法内容 → None."""
        resp = httpx.Response(200, content=b"just plain text", headers={"content-type": "text/plain"})
        assert decode_json_body(resp) is None

    def test_no_content_type_header_valid_json(self):
        """无 content-type header 但内容合法 JSON → 尝试解析."""
        resp = httpx.Response(200, content=b'{"naked": true}')
        assert decode_json_body(resp) == {"naked": True}


# ═══════════════════════════════════════════════════════════════
# 13. extract_error_message
# ═══════════════════════════════════════════════════════════════


class TestExtractErrorMessage:
    """extract_error_message 错误消息提取工具函数测试."""

    def test_nested_error_dict(self):
        """{"error": {"message": "..."}} → 提取内层 message."""
        resp = httpx.Response(401, content=b'{"error":{"type":"auth","message":"bad token"}}')
        msg = extract_error_message(resp, {"error": {"type": "auth", "message": "bad token"}})
        assert msg == "bad token"

    def test_error_string_value(self):
        """{"error": "..."} → 直接返回字符串."""
        resp = httpx.Response(400, content=b'{"error":"something wrong"}')
        msg = extract_error_message(resp, {"error": "something wrong"})
        assert msg == "something wrong"

    def test_top_level_message_field(self):
        """{"message": "..."} → 提取顶层 message."""
        resp = httpx.Response(500, content=b'{"message":"internal error"}')
        msg = extract_error_message(resp, {"message": "internal error"})
        assert msg == "internal error"

    def test_empty_content_returns_none(self):
        """空 body + None parsed body → None."""
        resp = httpx.Response(400, content=b"")
        msg = extract_error_message(resp, None)
        assert msg is None

    def test_fallback_to_raw_text_truncated(self):
        """非 dict body + 有原始文本 → 截断至 500 字符."""
        long_msg = "x" * 1000
        resp = httpx.Response(500, content=long_msg.encode())
        msg = extract_error_message(resp, long_msg)  # pass non-dict body
        assert msg is not None
        assert len(msg) <= 500

    def test_whitespace_only_content_returns_none(self):
        """纯空白内容 → None."""
        resp = httpx.Response(400, content=b"   \n\t  ")
        msg = extract_error_message(resp, None)
        assert msg is None

    def test_error_dict_without_message_key_returns_none(self):
        """error 是 dict 但无 message 键 → 返回 None (直接返回, 不回退到顶层 message 也不走 raw text)."""
        # 当 error 为 dict 时, 函数直接 return error.get("message"), 无 message 键则返回 None.
        resp = httpx.Response(400, content=b'some raw error text')
        msg = extract_error_message(resp, {"error": {"code": 123}})
        assert msg is None

    def test_error_dict_with_message_takes_priority(self):
        """优先级: error.message 最先匹配, 即使顶层也有 message 字段."""
        resp = httpx.Response(400, content=b'{"error":{"message":"from_error"},"message":"from_top"}')
        msg = extract_error_message(resp, {"error": {"message": "from_error"}, "message": "from_top"})
        assert msg == "from_error"
