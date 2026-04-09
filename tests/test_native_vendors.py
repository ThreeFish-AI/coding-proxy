"""原生 Anthropic 兼容端点供应商参数化测试.

验证 minimax、kimi、doubao、xiaomi、alibaba 五个新供应商的行为，
它们与 zhipu 共享 NativeAnthropicVendor 基类，行为完全一致：
  - 仅做模型名映射和认证头替换
  - 其余请求体/响应原样透传
  - 401 错误归一化
  - 能力声明全部为 NATIVE
"""

import json

import pytest

from coding.proxy.compat.canonical import CompatibilityStatus
from coding.proxy.config.schema import ModelMappingRule
from coding.proxy.config.vendors import (
    AlibabaConfig,
    DoubaoConfig,
    KimiConfig,
    MinimaxConfig,
    XiaomiConfig,
)
from coding.proxy.routing.model_mapper import ModelMapper
from coding.proxy.vendors.alibaba import AlibabaVendor
from coding.proxy.vendors.doubao import DoubaoVendor
from coding.proxy.vendors.kimi import KimiVendor
from coding.proxy.vendors.minimax import MinimaxVendor
from coding.proxy.vendors.xiaomi import XiaomiVendor

# ── 参数化供应商定义 ──────────────────────────────────────

VENDOR_PARAMS = [
    pytest.param(
        ("minimax", MinimaxConfig, MinimaxVendor, "MiniMax-M2.7", "MiniMax"),
        id="minimax",
    ),
    pytest.param(
        ("kimi", KimiConfig, KimiVendor, "kimi-k2.5", "Kimi"),
        id="kimi",
    ),
    pytest.param(
        ("doubao", DoubaoConfig, DoubaoVendor, "doubao-seed-2.0-pro", "Doubao"),
        id="doubao",
    ),
    pytest.param(
        ("xiaomi", XiaomiConfig, XiaomiVendor, "mimo-v2-pro", "Xiaomi"),
        id="xiaomi",
    ),
    pytest.param(
        ("alibaba", AlibabaConfig, AlibabaVendor, "qwen3.6-plus", "Alibaba"),
        id="alibaba",
    ),
]


@pytest.fixture(params=VENDOR_PARAMS)
def vendor_fixture(request):
    """创建参数化的供应商实例."""
    name, config_cls, vendor_cls, target_model, _display = request.param
    mapper = ModelMapper(
        [
            ModelMappingRule(
                pattern="claude-sonnet-.*",
                target=target_model,
                is_regex=True,
                vendors=[name],
            ),
            ModelMappingRule(
                pattern="claude-opus-.*",
                target=target_model,
                is_regex=True,
                vendors=[name],
            ),
            ModelMappingRule(
                pattern="claude-haiku-.*",
                target=target_model,
                is_regex=True,
                vendors=[name],
            ),
        ]
    )
    return vendor_cls(config_cls(api_key=f"test-{name}-key"), mapper)


@pytest.fixture(params=VENDOR_PARAMS)
def vendor_no_key(request):
    """创建没有 API key 的供应商实例（用于快速失败测试）."""
    name, config_cls, vendor_cls, target_model, _display = request.param
    mapper = ModelMapper(
        [
            ModelMappingRule(
                pattern="claude-sonnet-.*",
                target=target_model,
                is_regex=True,
                vendors=[name],
            ),
        ]
    )
    return vendor_cls(config_cls(api_key=""), mapper)


# ── 供应商名称 ──────────────────────────────────────────


class TestVendorName:
    """验证 get_name() 返回正确的供应商名."""

    def test_get_name(self, vendor_fixture):
        expected = {
            MinimaxVendor: "minimax",
            KimiVendor: "kimi",
            DoubaoVendor: "doubao",
            XiaomiVendor: "xiaomi",
            AlibabaVendor: "alibaba",
        }
        assert vendor_fixture.get_name() == expected[type(vendor_fixture)]


# ── 能力声明 ──────────────────────────────────────────────


class TestCapabilities:
    """全部能力声明为 NATIVE."""

    def test_all_capabilities_native(self, vendor_fixture):
        caps = vendor_fixture.get_capabilities()
        assert caps.supports_tools is True
        assert caps.supports_thinking is True
        assert caps.supports_images is True
        assert caps.supports_metadata is True
        assert caps.emits_vendor_tool_events is False

    def test_compatibility_profile_all_native(self, vendor_fixture):
        profile = vendor_fixture.get_compatibility_profile()
        assert profile.thinking is CompatibilityStatus.NATIVE
        assert profile.tool_calling is CompatibilityStatus.NATIVE
        assert profile.tool_streaming is CompatibilityStatus.NATIVE
        assert profile.mcp_tools is CompatibilityStatus.NATIVE
        assert profile.images is CompatibilityStatus.NATIVE
        assert profile.metadata is CompatibilityStatus.NATIVE
        assert profile.json_output is CompatibilityStatus.NATIVE
        assert profile.usage_tokens is CompatibilityStatus.NATIVE


# ── 模型映射 ──────────────────────────────────────────────


class TestModelMapping:
    """模型名映射完全委托 ModelMapper."""

    def test_sonnet_maps_correctly(self, vendor_fixture):
        result = vendor_fixture.map_model("claude-sonnet-4-20250514")
        # 确保映射发生（不再返回原始 Claude 名称）
        assert result != "claude-sonnet-4-20250514"

    def test_opus_maps_correctly(self, vendor_fixture):
        result = vendor_fixture.map_model("claude-opus-4-6")
        assert result != "claude-opus-4-6"

    def test_haiku_maps_correctly(self, vendor_fixture):
        result = vendor_fixture.map_model("claude-haiku-4-5-20251001")
        assert result != "claude-haiku-4-5-20251001"


# ── 请求透传 ──────────────────────────────────────────────


class TestRequestPassthrough:
    """验证 _prepare_request 仅修改 model 和 headers."""

    @pytest.mark.asyncio
    async def test_body_passthrough_except_model(self, vendor_fixture):
        body = {
            "model": "claude-sonnet-4-20250514",
            "messages": [{"role": "user", "content": "hello"}],
            "max_tokens": 1024,
            "temperature": 0.7,
            "stream": True,
            "thinking": {"type": "enabled", "budget_tokens": 5000},
            "metadata": {"user_id": "test-user"},
            "system": "You are a helpful assistant.",
            "tools": [
                {"name": "Bash", "input_schema": {"type": "object"}},
                {"name": "Read", "input_schema": {"type": "object"}},
            ],
        }
        prepared_body, _ = await vendor_fixture._prepare_request(body, {})

        # 仅 model 被映射
        assert prepared_body["model"] != "claude-sonnet-4-20250514"
        # 其余字段原样保留
        assert prepared_body["max_tokens"] == 1024
        assert prepared_body["temperature"] == 0.7
        assert prepared_body["stream"] is True
        assert prepared_body["thinking"] == {"type": "enabled", "budget_tokens": 5000}
        assert prepared_body["metadata"] == {"user_id": "test-user"}
        assert prepared_body["system"] == "You are a helpful assistant."
        assert len(prepared_body["tools"]) == 2
        # 原始 body 未被修改（deep copy）
        assert body["model"] == "claude-sonnet-4-20250514"

    @pytest.mark.asyncio
    async def test_headers_replaces_auth(self, vendor_fixture):
        """验证 x-api-key 被正确设置，authorization 被剥离."""
        _, prepared_headers = await vendor_fixture._prepare_request(
            {"model": "claude-sonnet-4-20250514", "messages": []},
            {
                "authorization": "Bearer sk-old",
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
                "x-custom-header": "keep-me",
            },
        )
        name = vendor_fixture.get_name()
        assert prepared_headers["x-api-key"] == f"test-{name}-key"
        assert prepared_headers["anthropic-version"] == "2023-06-01"
        assert "authorization" not in prepared_headers
        assert prepared_headers["x-custom-header"] == "keep-me"


# ── 认证错误处理 ──────────────────────────────────────────


class TestAuthErrorHandling:
    @pytest.mark.asyncio
    async def test_missing_api_key_fast_fail_stream(self, vendor_no_key):
        """API key 缺失时流式请求立即失败."""
        try:
            async for _ in vendor_no_key.send_message_stream(
                {"model": "claude-opus-4-6", "messages": []},
                {},
            ):
                pass
        except Exception as exc:
            assert "401" in str(exc)
        else:
            pytest.fail("Expected HTTPStatusError for missing API key")

    @pytest.mark.asyncio
    async def test_missing_api_key_fast_fail_nonstream(self, vendor_no_key):
        """API key 缺失时非流式请求立即返回 401."""
        resp = await vendor_no_key.send_message(
            {"model": "claude-opus-4-6", "messages": []},
            {},
        )
        assert resp.status_code == 401
        assert resp.error_type == "authentication_error"

    @pytest.mark.asyncio
    async def test_missing_key_error_contains_vendor_name(self, vendor_no_key):
        """确认错误消息中包含正确的供应商名称."""
        resp = await vendor_no_key.send_message(
            {"model": "claude-opus-4-6", "messages": []},
            {},
        )
        body = json.loads(resp.raw_body)
        display_name = vendor_no_key._display_name
        assert display_name in body["error"]["message"]


# ── 终端供应商行为 ────────────────────────────────────────


class TestTerminalVendor:
    """无 failover_config 时不触发故障转移."""

    def test_never_triggers_failover_without_config(self, vendor_fixture):
        """默认不传 failover_config → should_trigger_failover 始终 False."""
        assert not vendor_fixture.should_trigger_failover(429, None)
        assert not vendor_fixture.should_trigger_failover(
            500, {"error": {"type": "rate_limit_error"}}
        )
        assert not vendor_fixture.should_trigger_failover(503, None)

    @pytest.mark.asyncio
    async def test_health_check_always_true(self, vendor_fixture):
        result = await vendor_fixture.check_health()
        assert result is True
