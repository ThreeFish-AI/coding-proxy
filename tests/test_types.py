"""types.py 数据类型与工具函数单元测试."""

import httpx
import pytest

from coding.proxy.vendors.base import (
    PROXY_SKIP_HEADERS,
    RESPONSE_SANITIZE_SKIP_HEADERS,
    CapabilityLossReason,
    NoCompatibleVendorError,
    RequestCapabilities,
    UsageInfo,
    VendorCapabilities,
    VendorResponse,
)
from coding.proxy.vendors.base import (
    decode_json_body as _decode_json_body,
)
from coding.proxy.vendors.base import (
    extract_error_message as _extract_error_message,
)
from coding.proxy.vendors.base import (
    sanitize_headers_for_synthetic_response as _sanitize_headers_for_synthetic_response,
)

# ── UsageInfo ────────────────────────────────────────────


def test_usage_info_defaults():
    usage = UsageInfo()
    assert usage.input_tokens == 0
    assert usage.output_tokens == 0
    assert usage.cache_creation_tokens == 0
    assert usage.cache_read_tokens == 0
    assert usage.request_id == ""


def test_usage_info_with_values():
    usage = UsageInfo(
        input_tokens=100,
        output_tokens=50,
        cache_creation_tokens=10,
        cache_read_tokens=20,
        request_id="req_1",
    )
    assert usage.input_tokens == 100
    assert usage.output_tokens == 50


# ── CapabilityLossReason 枚举 ────────────────────────────


def test_capability_loss_reason_values():
    assert CapabilityLossReason.TOOLS.value == "tools"
    assert CapabilityLossReason.THINKING.value == "thinking"
    assert CapabilityLossReason.IMAGES.value == "images"
    assert CapabilityLossReason.VENDOR_TOOLS.value == "vendor_tools"
    assert CapabilityLossReason.METADATA.value == "metadata"


# ── RequestCapabilities (frozen) ──────────────────────────


def test_request_capabilities_defaults():
    caps = RequestCapabilities()
    assert caps.has_tools is False
    assert caps.has_thinking is False
    assert caps.has_images is False
    assert caps.has_metadata is False
    assert caps.has_tool_results is False


def test_request_capabilities_immutable():
    caps = RequestCapabilities(has_tools=True)
    with pytest.raises(AttributeError):
        caps.has_tools = False  # type: ignore[misc]


# ── VendorCapabilities (frozen) ─────────────────────────


def test_vendor_capabilities_defaults():
    caps = VendorCapabilities()
    assert caps.supports_tools is True
    assert caps.supports_thinking is True
    assert caps.supports_images is True
    assert caps.emits_vendor_tool_events is False
    assert caps.supports_metadata is True


# ── VendorResponse ──────────────────────────────────────


def test_vendor_response_defaults():
    resp = VendorResponse()
    assert resp.status_code == 200
    assert resp.usage.input_tokens == 0
    assert resp.raw_body == b"{}"
    assert resp.error_type is None
    assert resp.error_message is None
    assert resp.model_served is None


# ── NoCompatibleVendorError ──────────────────────────────


def test_no_compatible_vendor_error():
    err = NoCompatibleVendorError("no vendor", reasons=["tools", "thinking"])
    assert str(err) == "no vendor"
    assert err.reasons == ["tools", "thinking"]


# ── 常量 ────────────────────────────────────────────────


def test_proxy_skip_headers_contains_expected():
    assert "host" in PROXY_SKIP_HEADERS
    assert "content-length" in PROXY_SKIP_HEADERS
    assert "transfer-encoding" in PROXY_SKIP_HEADERS
    assert "connection" in PROXY_SKIP_HEADERS


def test_response_sanitize_skip_headers_contains_expected():
    assert "content-encoding" in RESPONSE_SANITIZE_SKIP_HEADERS
    assert "content-length" in RESPONSE_SANITIZE_SKIP_HEADERS
    assert "transfer-encoding" in RESPONSE_SANITIZE_SKIP_HEADERS


# ── _sanitize_headers_for_synthetic_response ─────────────


def test_sanitize_headers_removes_encoding():
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
    raw_headers = httpx.Headers(
        {
            "content-type": "application/json",
            "content-encoding": "gzip",
        }
    )
    clean_headers = _sanitize_headers_for_synthetic_response(raw_headers)
    resp = httpx.Response(
        429,
        content=b'{"error": "rate limit"}',
        headers=clean_headers,
        request=httpx.Request("POST", "https://api.example.com/v1/messages"),
    )
    assert resp.status_code == 429
    assert b"rate limit" in resp.content


# ── _decode_json_body ────────────────────────────────────


def test_decode_json_body_valid_json():
    resp = httpx.Response(
        200, content=b'{"key":"value"}', headers={"content-type": "application/json"}
    )
    result = _decode_json_body(resp)
    assert result == {"key": "value"}


def test_decode_json_body_returns_none_for_html():
    resp = httpx.Response(
        200, content=b"<html>not json</html>", headers={"content-type": "text/html"}
    )
    # HTML 但内容是有效 JSON → 应返回解析结果
    result = _decode_json_body(resp)
    assert result is None


def test_decode_json_body_empty_content():
    resp = httpx.Response(
        200, content=b"", headers={"content-type": "application/json"}
    )
    assert _decode_json_body(resp) is None


def test_decode_json_body_invalid_json():
    resp = httpx.Response(
        200, content=b"{invalid", headers={"content-type": "application/json"}
    )
    assert _decode_json_body(resp) is None


def test_decode_json_body_non_json_content_type_with_valid_json():
    """非 JSON content-type 但内容为合法 JSON → 尝试解析."""
    resp = httpx.Response(
        200, content=b'{"ok":true}', headers={"content-type": "text/plain"}
    )
    result = _decode_json_body(resp)
    assert result == {"ok": True}


# ── _extract_error_message ────────────────────────────────


def test_extract_error_nested_dict():
    resp = httpx.Response(
        401, content=b'{"error":{"type":"auth","message":"bad token"}}'
    )
    msg = _extract_error_message(
        resp, {"error": {"type": "auth", "message": "bad token"}}
    )
    assert msg == "bad token"


def test_extract_error_string_value():
    resp = httpx.Response(400, content=b'{"error":"something wrong"}')
    msg = _extract_error_message(resp, {"error": "something wrong"})
    assert msg == "something wrong"


def test_extract_error_message_field():
    resp = httpx.Response(500, content=b'{"message":"internal error"}')
    msg = _extract_error_message(resp, {"message": "internal error"})
    assert msg == "internal error"


def test_extract_error_empty_content():
    resp = httpx.Response(400, content=b"")
    msg = _extract_error_message(resp, None)
    assert msg is None
