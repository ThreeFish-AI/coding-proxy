"""智谱 GLM 原生端点薄透传代理专项测试.

验证 ZhipuBackend 在官方 Anthropic 兼容端点模式下的行为：
  - 仅做模型名映射和认证头替换
  - 其余请求体/响应原样透传
  - 401 错误归一化
  - 能力声明全部为 NATIVE
"""

import json

import pytest

from coding.proxy.backends.zhipu import ZhipuBackend
from coding.proxy.compat.canonical import CompatibilityStatus
from coding.proxy.config.schema import ModelMappingRule, ZhipuConfig
from coding.proxy.routing.model_mapper import ModelMapper


@pytest.fixture
def zhipu_backend():
    """创建使用默认配置的 ZhipuBackend 实例."""
    mapper = ModelMapper([
        ModelMappingRule(pattern="claude-sonnet-.*", target="glm-5.1", is_regex=True, backends=["zhipu"]),
        ModelMappingRule(pattern="claude-opus-.*", target="glm-5.1", is_regex=True, backends=["zhipu"]),
        ModelMappingRule(pattern="claude-haiku-.*", target="glm-4.5-air", is_regex=True, backends=["zhipu"]),
    ])
    return ZhipuBackend(ZhipuConfig(api_key="test-zhipu-key"), mapper)


# ── 模型映射 ──────────────────────────────────────────────


class TestModelMapping:
    """模型名映射完全委托 ModelMapper."""

    def test_sonnet_maps_to_glm_51(self, zhipu_backend):
        assert zhipu_backend.map_model("claude-sonnet-4-20250514") == "glm-5.1"

    def test_opus_maps_to_glm_51(self, zhipu_backend):
        assert zhipu_backend.map_model("claude-opus-4-6") == "glm-5.1"

    def test_haiku_maps_to_glm_45_air(self, zhipu_backend):
        assert zhipu_backend.map_model("claude-haiku-4-5-20251001") == "glm-4.5-air"

    def test_unknown_model_falls_back_to_default(self, zhipu_backend):
        """未匹配规则的模型名回退到 ModelMapper 默认值."""
        assert zhipu_backend.map_model("unknown-model") == "glm-5.1"


# ── 请求透传 ──────────────────────────────────────────────


class TestRequestPassthrough:
    """验证 _prepare_request 仅修改 model 和 headers."""

    @pytest.mark.asyncio
    async def test_body_passthrough_except_model(self, zhipu_backend):
        body = {
            "model": "claude-sonnet-4-20250514",
            "messages": [{"role": "user", "content": "hello"}],
            "max_tokens": 1024,
            "temperature": 0.7,
            "top_p": 0.9,
            "stream": True,
            "thinking": {"type": "enabled", "budget_tokens": 5000},
            "metadata": {"user_id": "test-user"},
            "system": "You are a helpful assistant.",
            "tools": [
                {"name": "Bash", "input_schema": {"type": "object"}},
                {"name": "Read", "input_schema": {"type": "object"}},
                {"name": "Write", "input_schema": {"type": "object"}},
            ],
            "tool_choice": {"type": "auto"},
        }
        prepared_body, _ = await zhipu_backend._prepare_request(body, {})

        # 仅 model 被映射
        assert prepared_body["model"] == "glm-5.1"
        # 其余字段原样保留
        assert prepared_body["max_tokens"] == 1024
        assert prepared_body["temperature"] == 0.7
        assert prepared_body["top_p"] == 0.9
        assert prepared_body["stream"] is True
        # thinking 不再被剥离
        assert prepared_body["thinking"] == {"type": "enabled", "budget_tokens": 5000}
        # metadata 不再被剥离
        assert prepared_body["metadata"] == {"user_id": "test-user"}
        # system 不被删除
        assert prepared_body["system"] == "You are a helpful assistant."
        # tools 不被截断或过滤
        assert len(prepared_body["tools"]) == 3
        # tool_choice 不被修改
        assert prepared_body["tool_choice"] == {"type": "auto"}
        # 原始 body 未被修改（deep copy）
        assert body["model"] == "claude-sonnet-4-20250514"

    @pytest.mark.asyncio
    async def test_headers_replaces_auth(self, zhipu_backend):
        """验证 x-api-key 被正确设置，authorization 被剥离."""
        _, prepared_headers = await zhipu_backend._prepare_request(
            {"model": "claude-sonnet-4-20250514", "messages": []},
            {
                "authorization": "Bearer sk-old",
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
                "x-custom-header": "keep-me",
            },
        )
        assert prepared_headers["x-api-key"] == "test-zhipu-key"
        assert prepared_headers["anthropic-version"] == "2023-06-01"
        # authorization 必须被剥离（防止 Anthropic Bearer token 泄漏到智谱）
        assert "authorization" not in prepared_headers
        assert prepared_headers["x-custom-header"] == "keep-me"

    @pytest.mark.asyncio
    async def test_headers_strips_authorization(self, zhipu_backend):
        """验证 Claude Code 发来的 authorization: Bearer 头被完全移除.

        这是 401 认证失败的根因修复：智谱 /api/anthropic 端点仅接受
        x-api-key 认证，authorization 中的 Anthropic key 会导致冲突。
        """
        headers_in = {
            "authorization": "Bearer sk-ant-api03-xxxxx",
            "x-api-key": "sk-ant-api03-yyyyy",
            "anthropic-version": "2023-06-01",
            "anthropic-beta": "prompt-caching-2024-07-31",
            "host": "localhost:8046",
            "content-length": "42",
        }
        _, prepared_headers = await zhipu_backend._prepare_request(
            {"model": "claude-haiku-4-5-20251001", "messages": []},
            headers_in,
        )
        # 两个认证头都必须被移除
        assert "authorization" not in prepared_headers
        assert prepared_headers.get("x-api-key") == "test-zhipu-key"
        # hop-by-hop 头被移除
        assert "host" not in prepared_headers
        assert "content-length" not in prepared_headers
        # 业务头保留
        assert prepared_headers["anthropic-version"] == "2023-06-01"
        assert prepared_headers["anthropic-beta"] == "prompt-caching-2024-07-31"

    @pytest.mark.asyncio
    async def test_tools_with_mcp_and_browser_preserved(self, zhipu_backend):
        """MCP 工具和浏览器工具不再被过滤."""
        body = {
            "model": "claude-opus-4-6",
            "messages": [],
            "tools": [
                {"name": "Task", "input_schema": {"type": "object"}},
                {"name": "mcp__playwright__browser_click", "input_schema": {"type": "object"}},
                {"name": "mcp__vibe_kanban__create_issue", "input_schema": {"type": "object"}},
                {"name": "mcp__chrome_devtools__take_screenshot", "input_schema": {"type": "object"}},
            ],
        }
        prepared_body, _ = await zhipu_backend._prepare_request(body, {})
        assert len(prepared_body["tools"]) == 4

    @pytest.mark.asyncio
    async def test_large_tool_set_not_capped(self, zhipu_backend):
        """大量工具列表不被截断."""
        tools = [{"name": f"tool_{i}", "input_schema": {"type": "object"}} for i in range(100)]
        body = {"model": "claude-opus-4-6", "messages": [], "tools": tools}
        prepared_body, _ = await zhipu_backend._prepare_request(body, {})
        assert len(prepared_body["tools"]) == 100


# ── 能力声明 ──────────────────────────────────────────────


class TestCapabilities:
    """全部能力声明为 NATIVE."""

    def test_all_capabilities_native(self, zhipu_backend):
        caps = zhipu_backend.get_capabilities()
        assert caps.supports_tools is True
        assert caps.supports_thinking is True
        assert caps.supports_images is True
        assert caps.supports_metadata is True
        assert caps.emits_vendor_tool_events is False

    def test_compatibility_profile_all_native(self, zhipu_backend):
        profile = zhipu_backend.get_compatibility_profile()
        assert profile.thinking is CompatibilityStatus.NATIVE
        assert profile.tool_calling is CompatibilityStatus.NATIVE
        assert profile.tool_streaming is CompatibilityStatus.NATIVE
        assert profile.mcp_tools is CompatibilityStatus.NATIVE
        assert profile.images is CompatibilityStatus.NATIVE
        assert profile.metadata is CompatibilityStatus.NATIVE
        assert profile.json_output is CompatibilityStatus.NATIVE
        assert profile.usage_tokens is CompatibilityStatus.NATIVE


# ── 认证错误处理 ──────────────────────────────────────────


class TestAuthErrorHandling:

    @pytest.fixture
    def backend(self):
        return ZhipuBackend(ZhipuConfig(api_key="sk-test"), ModelMapper([]))

    @pytest.mark.asyncio
    async def test_missing_api_key_fast_fail_stream(self):
        backend = ZhipuBackend(ZhipuConfig(api_key=""), ModelMapper([]))
        chunks = []
        try:
            async for chunk in backend.send_message_stream(
                {"model": "claude-opus-4-6", "messages": []}, {},
            ):
                chunks.append(chunk)
        except Exception as exc:
            assert "401" in str(exc)
        else:
            pytest.fail("Expected HTTPStatusError for missing API key")

    @pytest.mark.asyncio
    async def test_missing_api_key_fast_fail_nonstream(self):
        backend = ZhipuBackend(ZhipuConfig(api_key=""), ModelMapper([]))
        resp = await backend.send_message(
            {"model": "claude-opus-4-6", "messages": []}, {},
        )
        assert resp.status_code == 401
        assert resp.error_type == "authentication_error"

    def test_normalize_401_error_payload(self, backend):
        payload = {"error": {"type": "401", "message": "令牌已过期"}}
        raw, normalized = backend._normalize_backend_error(401, json.dumps(payload).encode())
        assert normalized["error"]["type"] == "authentication_error"
        assert b'"authentication_error"' in raw

    def test_normalize_401_empty_payload(self, backend):
        raw, normalized = backend._normalize_backend_error(401, b"not json")
        assert normalized is not None
        assert normalized["error"]["type"] == "authentication_error"

    def test_non_401_passthrough(self, backend):
        raw_body = b'{"error":{"type":"rate_limit","message":"too fast"}}'
        raw, payload = backend._normalize_backend_error(429, raw_body)
        assert raw == raw_body  # 非 401 原样返回
        assert payload["error"]["type"] == "rate_limit"


# ── 终端后端行为 ──────────────────────────────────────────


class TestTerminalBackend:
    """Zhipu 作为终端层不触发故障转移."""

    def test_never_triggers_failover(self, zhipu_backend):
        assert not zhipu_backend.should_trigger_failover(429, None)
        assert not zhipu_backend.should_trigger_failover(500, {"error": {"type": "rate_limit_error"}})
        assert not zhipu_backend.should_trigger_failover(503, None)

    @pytest.mark.asyncio
    async def test_health_check_always_true(self, zhipu_backend):
        result = await zhipu_backend.check_health()
        assert result is True
