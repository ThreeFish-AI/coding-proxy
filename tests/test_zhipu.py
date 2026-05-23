"""智谱 GLM 原生端点薄透传代理专项测试.

验证 ZhipuVendor 在官方 Anthropic 兼容端点模式下的行为：
  - 仅做模型名映射和认证头替换
  - 其余请求体/响应原样透传
  - 401 错误归一化
  - 能力声明全部为 NATIVE
  - 429 Rate Limit 重试挽回
"""

import json
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from coding.proxy.compat.canonical import CompatibilityStatus
from coding.proxy.config.schema import ModelMappingRule, ZhipuConfig
from coding.proxy.routing.model_mapper import ModelMapper
from coding.proxy.vendors.native_anthropic import NativeAnthropicVendor
from coding.proxy.vendors.zhipu import ZhipuVendor


def _make_zhipu_vendor(api_key: str = "test-zhipu-key") -> ZhipuVendor:
    """创建使用默认配置的 ZhipuVendor 实例."""
    mapper = ModelMapper(
        [
            ModelMappingRule(
                pattern="claude-sonnet-.*",
                target="glm-5.1",
                is_regex=True,
                vendors=["zhipu"],
            ),
            ModelMappingRule(
                pattern="claude-opus-.*",
                target="glm-5.1",
                is_regex=True,
                vendors=["zhipu"],
            ),
            ModelMappingRule(
                pattern="claude-haiku-.*",
                target="glm-4.5-air",
                is_regex=True,
                vendors=["zhipu"],
            ),
        ]
    )
    return ZhipuVendor(ZhipuConfig(api_key=api_key), mapper)


@pytest.fixture
def zhipu_vendor():
    """创建使用默认配置的 ZhipuVendor 实例."""
    return _make_zhipu_vendor()


# ── 模型映射 ──────────────────────────────────────────────


class TestModelMapping:
    """模型名映射完全委托 ModelMapper."""

    def test_sonnet_maps_to_glm_51(self, zhipu_vendor):
        assert zhipu_vendor.map_model("claude-sonnet-4-20250514") == "glm-5.1"

    def test_opus_maps_to_glm_51(self, zhipu_vendor):
        assert zhipu_vendor.map_model("claude-opus-4-6") == "glm-5.1"

    def test_haiku_maps_to_glm_45_air(self, zhipu_vendor):
        assert zhipu_vendor.map_model("claude-haiku-4-5-20251001") == "glm-4.5-air"

    def test_unknown_model_falls_back_to_default(self, zhipu_vendor):
        """未匹配规则的模型名回退到 ModelMapper 默认值."""
        assert zhipu_vendor.map_model("unknown-model") == "glm-5.1"


# ── 请求透传 ──────────────────────────────────────────────


class TestRequestPassthrough:
    """验证 _prepare_request 仅修改 model 和 headers."""

    @pytest.mark.asyncio
    async def test_body_passthrough_except_model(self, zhipu_vendor):
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
        prepared_body, _ = await zhipu_vendor._prepare_request(body, {})

        # 仅 model 被映射
        assert prepared_body["model"] == "glm-5.1"
        # 其余字段原样保留（GLM 原生支持 thinking，静默忽略 cache_control）
        assert prepared_body["max_tokens"] == 1024
        assert prepared_body["temperature"] == 0.7
        assert prepared_body["top_p"] == 0.9
        assert prepared_body["stream"] is True
        assert prepared_body["thinking"] == {"type": "enabled", "budget_tokens": 5000}
        assert prepared_body["metadata"] == {"user_id": "test-user"}
        assert prepared_body["system"] == "You are a helpful assistant."
        assert len(prepared_body["tools"]) == 3
        assert prepared_body["tool_choice"] == {"type": "auto"}
        # 原始 body 未被修改（deep copy）
        assert body["model"] == "claude-sonnet-4-20250514"

    @pytest.mark.asyncio
    async def test_headers_replaces_auth(self, zhipu_vendor):
        """验证 x-api-key 被正确设置，authorization 被剥离."""
        _, prepared_headers = await zhipu_vendor._prepare_request(
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
    async def test_headers_strips_authorization(self, zhipu_vendor):
        """验证 Claude Code 发来的 authorization: Bearer 头被完全移除.

        这是 401 认证失败的根因修复：智谱 /api/anthropic 端点仅接受
        x-api-key 认证，authorization 中的 Anthropic key 会导致冲突。
        """
        headers_in = {
            "authorization": "Bearer sk-ant-api03-xxxxx",
            "x-api-key": "sk-ant-api03-yyyyy",
            "anthropic-version": "2023-06-01",
            "anthropic-beta": "prompt-caching-2024-07-31",
            "host": "localhost:3392",
            "content-length": "42",
        }
        _, prepared_headers = await zhipu_vendor._prepare_request(
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
    async def test_tools_with_mcp_and_browser_preserved(self, zhipu_vendor):
        """MCP 工具和浏览器工具不再被过滤."""
        body = {
            "model": "claude-opus-4-6",
            "messages": [],
            "tools": [
                {"name": "Task", "input_schema": {"type": "object"}},
                {
                    "name": "mcp__playwright__browser_click",
                    "input_schema": {"type": "object"},
                },
                {
                    "name": "mcp__vibe_kanban__create_issue",
                    "input_schema": {"type": "object"},
                },
                {
                    "name": "mcp__chrome_devtools__take_screenshot",
                    "input_schema": {"type": "object"},
                },
            ],
        }
        prepared_body, _ = await zhipu_vendor._prepare_request(body, {})
        assert len(prepared_body["tools"]) == 4

    @pytest.mark.asyncio
    async def test_large_tool_set_not_capped(self, zhipu_vendor):
        """大量工具列表不被截断."""
        tools = [
            {"name": f"tool_{i}", "input_schema": {"type": "object"}}
            for i in range(100)
        ]
        body = {"model": "claude-opus-4-6", "messages": [], "tools": tools}
        prepared_body, _ = await zhipu_vendor._prepare_request(body, {})
        assert len(prepared_body["tools"]) == 100


# ── 能力声明 ──────────────────────────────────────────────


class TestCapabilities:
    """全部能力声明为 NATIVE."""

    def test_all_capabilities_native(self, zhipu_vendor):
        caps = zhipu_vendor.get_capabilities()
        assert caps.supports_tools is True
        assert caps.supports_thinking is True
        assert caps.supports_images is True
        assert caps.supports_metadata is True
        assert caps.emits_vendor_tool_events is False

    def test_compatibility_profile_all_native(self, zhipu_vendor):
        profile = zhipu_vendor.get_compatibility_profile()
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
    def vendor(self):
        return ZhipuVendor(ZhipuConfig(api_key="sk-test"), ModelMapper([]))

    @pytest.mark.asyncio
    async def test_missing_api_key_fast_fail_stream(self):
        vendor = ZhipuVendor(ZhipuConfig(api_key=""), ModelMapper([]))
        chunks = []
        try:
            async for chunk in vendor.send_message_stream(
                {"model": "claude-opus-4-6", "messages": []},
                {},
            ):
                chunks.append(chunk)
        except Exception as exc:
            assert "401" in str(exc)
        else:
            pytest.fail("Expected HTTPStatusError for missing API key")

    @pytest.mark.asyncio
    async def test_missing_api_key_fast_fail_nonstream(self):
        vendor = ZhipuVendor(ZhipuConfig(api_key=""), ModelMapper([]))
        resp = await vendor.send_message(
            {"model": "claude-opus-4-6", "messages": []},
            {},
        )
        assert resp.status_code == 401
        assert resp.error_type == "authentication_error"

    def test_normalize_401_error_payload(self, vendor):
        payload = {"error": {"type": "401", "message": "令牌已过期"}}
        raw, normalized = vendor._normalize_backend_error(
            401, json.dumps(payload).encode()
        )
        assert normalized["error"]["type"] == "authentication_error"
        assert b'"authentication_error"' in raw

    def test_normalize_401_empty_payload(self, vendor):
        raw, normalized = vendor._normalize_backend_error(401, b"not json")
        assert normalized is not None
        assert normalized["error"]["type"] == "authentication_error"

    def test_non_401_passthrough(self, vendor):
        raw_body = b'{"error":{"type":"rate_limit","message":"too fast"}}'
        raw, payload = vendor._normalize_backend_error(429, raw_body)
        assert raw == raw_body  # 非 401 原样返回
        assert payload["error"]["type"] == "rate_limit"


# ── 终端供应商行为 ────────────────────────────────────────


class TestTerminalVendor:
    """Zhipu 作为终端层不触发故障转移."""

    def test_never_triggers_failover(self, zhipu_vendor):
        assert not zhipu_vendor.should_trigger_failover(429, None)
        assert not zhipu_vendor.should_trigger_failover(
            500, {"error": {"type": "rate_limit_error"}}
        )
        assert not zhipu_vendor.should_trigger_failover(503, None)

    @pytest.mark.asyncio
    async def test_health_check_always_true(self, zhipu_vendor):
        result = await zhipu_vendor.check_health()
        assert result is True


# ── 429 Rate Limit 重试挽回 ─────────────────────────────────


def _make_429_response(
    headers: dict[str, str] | None = None,
) -> httpx.Response:
    """构造 429 HTTP 响应."""
    return httpx.Response(
        status_code=429,
        content=b'{"error":{"type":"rate_limit_error","message":"Too many requests"}}',
        headers=headers or {},
        request=httpx.Request(
            "POST", "https://open.bigmodel.cn/api/anthropic/v1/messages"
        ),
    )


def _make_200_response() -> httpx.Response:
    """构造 200 HTTP 响应."""
    body = json.dumps(
        {
            "id": "msg_test",
            "type": "message",
            "role": "assistant",
            "content": [{"type": "text", "text": "hello"}],
            "model": "glm-5.1",
            "usage": {"input_tokens": 10, "output_tokens": 5},
        }
    ).encode()
    return httpx.Response(
        status_code=200,
        content=body,
        headers={"content-type": "application/json"},
        request=httpx.Request(
            "POST", "https://open.bigmodel.cn/api/anthropic/v1/messages"
        ),
    )


class TestRateLimitRetry:
    """429 Rate Limit 重试挽回机制."""

    # ── 非流式 ─────────────────────────────────────────────

    @pytest.mark.asyncio
    async def test_nonstream_429_retries_and_succeeds(self):
        """429 两次后 200，重试成功."""
        vendor = _make_zhipu_vendor()
        call_count = 0

        async def mock_post(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count <= 2:
                return _make_429_response()
            return _make_200_response()

        with patch.object(vendor, "_get_client") as mock_client:
            client = AsyncMock()
            client.post = mock_post
            mock_client.return_value = client

            resp = await vendor.send_message(
                {"model": "claude-sonnet-4-20250514", "messages": []},
                {},
            )

        assert resp.status_code == 200
        assert call_count == 3

    @pytest.mark.asyncio
    async def test_nonstream_429_exhausted_retries(self):
        """连续 5 次 429，耗尽重试后返回 429."""
        vendor = _make_zhipu_vendor()
        call_count = 0

        async def mock_post(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            return _make_429_response()

        with patch.object(vendor, "_get_client") as mock_client:
            client = AsyncMock()
            client.post = mock_post
            mock_client.return_value = client

            with patch("asyncio.sleep", new_callable=AsyncMock):
                resp = await vendor.send_message(
                    {"model": "claude-sonnet-4-20250514", "messages": []},
                    {},
                )

        assert resp.status_code == 429
        assert call_count == 5

    @pytest.mark.asyncio
    async def test_nonstream_non_429_no_retry(self):
        """500 不触发重试."""
        vendor = _make_zhipu_vendor()
        call_count = 0

        async def mock_post(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            return httpx.Response(
                status_code=500,
                content=b'{"error":{"type":"api_error","message":"Internal error"}}',
                request=httpx.Request("POST", "https://example.com"),
            )

        with patch.object(vendor, "_get_client") as mock_client:
            client = AsyncMock()
            client.post = mock_post
            mock_client.return_value = client

            resp = await vendor.send_message(
                {"model": "claude-sonnet-4-20250514", "messages": []},
                {},
            )

        assert resp.status_code == 500
        assert call_count == 1

    # ── 流式 ───────────────────────────────────────────────

    @pytest.mark.asyncio
    async def test_stream_429_retries_and_succeeds(self):
        """流式 429 两次后成功."""
        call_count = 0

        async def fake_stream(self, body, headers):
            nonlocal call_count
            call_count += 1
            if call_count <= 2:
                resp = _make_429_response()
                raise httpx.HTTPStatusError(
                    "429",
                    request=resp.request,
                    response=resp,
                )
            yield b'data: {"type":"content_block_start"}\n\n'
            yield b'data: {"type":"content_block_delta"}\n\n'

        vendor = _make_zhipu_vendor()
        chunks = []
        with (
            patch.object(NativeAnthropicVendor, "send_message_stream", fake_stream),
            patch("asyncio.sleep", new_callable=AsyncMock),
        ):
            async for chunk in vendor.send_message_stream(
                {"model": "claude-sonnet-4-20250514", "messages": []},
                {},
            ):
                chunks.append(chunk)

        assert len(chunks) == 2
        assert call_count == 3

    @pytest.mark.asyncio
    async def test_stream_429_exhausted_retries_raises(self):
        """流式连续 429，耗尽重试后 raise."""
        call_count = 0

        async def fake_stream(self, body, headers):
            nonlocal call_count
            call_count += 1
            resp = _make_429_response()
            raise httpx.HTTPStatusError(
                "429",
                request=resp.request,
                response=resp,
            )
            yield  # 使函数成为 async generator（不可达，仅影响类型）

        vendor = _make_zhipu_vendor()
        with (
            patch.object(NativeAnthropicVendor, "send_message_stream", fake_stream),
            patch("asyncio.sleep", new_callable=AsyncMock),
            pytest.raises(httpx.HTTPStatusError) as exc_info,
        ):
            async for _ in vendor.send_message_stream(
                {"model": "claude-sonnet-4-20250514", "messages": []},
                {},
            ):
                pass

        assert exc_info.value.response.status_code == 429
        assert call_count == 5

    @pytest.mark.asyncio
    async def test_stream_500_no_retry_raises(self):
        """流式 500 不触发重试，直接 raise."""
        call_count = 0

        async def fake_stream(self, body, headers):
            nonlocal call_count
            call_count += 1
            resp = httpx.Response(
                status_code=500,
                content=b'{"error":{"type":"api_error"}}',
                request=httpx.Request("POST", "https://example.com"),
            )
            raise httpx.HTTPStatusError(
                "500",
                request=resp.request,
                response=resp,
            )
            yield  # 使函数成为 async generator

        vendor = _make_zhipu_vendor()
        with (
            patch.object(NativeAnthropicVendor, "send_message_stream", fake_stream),
            pytest.raises(httpx.HTTPStatusError) as exc_info,
        ):
            async for _ in vendor.send_message_stream(
                {"model": "claude-sonnet-4-20250514", "messages": []},
                {},
            ):
                pass

        assert exc_info.value.response.status_code == 500
        assert call_count == 1

    # ── retry-after header ─────────────────────────────────

    @pytest.mark.asyncio
    async def test_respects_retry_after_header(self):
        """响应含 retry-after 时使用 server 建议延迟."""
        vendor = _make_zhipu_vendor()
        call_count = 0
        sleep_delays = []

        async def mock_post(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return _make_429_response(headers={"retry-after": "2"})
            return _make_200_response()

        async def mock_sleep(delay):
            sleep_delays.append(delay)

        with (
            patch.object(vendor, "_get_client") as mock_client,
            patch("asyncio.sleep", side_effect=mock_sleep),
        ):
            client = AsyncMock()
            client.post = mock_post
            mock_client.return_value = client

            resp = await vendor.send_message(
                {"model": "claude-sonnet-4-20250514", "messages": []},
                {},
            )

        assert resp.status_code == 200
        assert len(sleep_delays) == 1
        # retry-after=2 → 2 * 1.1 = 2.2s → 2200ms → sleep(2.2)
        assert 2.0 <= sleep_delays[0] <= 2.2

    # ── 退避延迟增长 ───────────────────────────────────────

    @pytest.mark.asyncio
    async def test_backoff_delays_increase(self):
        """无 retry-after 时延迟按指数增长."""
        vendor = _make_zhipu_vendor()
        sleep_delays = []

        async def mock_sleep(delay):
            sleep_delays.append(delay)

        # 禁用 jitter 以精确验证延迟
        import dataclasses

        original_jitter = vendor._rl_retry.jitter
        vendor._rl_retry = dataclasses.replace(vendor._rl_retry, jitter=False)

        call_count = 0

        async def mock_post(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count <= 4:
                return _make_429_response()
            return _make_200_response()

        try:
            with (
                patch.object(vendor, "_get_client") as mock_client,
                patch("asyncio.sleep", side_effect=mock_sleep),
            ):
                client = AsyncMock()
                client.post = mock_post
                mock_client.return_value = client

                resp = await vendor.send_message(
                    {"model": "claude-sonnet-4-20250514", "messages": []},
                    {},
                )

            assert resp.status_code == 200
            assert len(sleep_delays) == 4
            # initial=1000ms, multiplier=2.0
            # attempt 0: 1000 * 2^0 = 1000ms → sleep(1.0)
            # attempt 1: 1000 * 2^1 = 2000ms → sleep(2.0)
            # attempt 2: 1000 * 2^2 = 4000ms → sleep(4.0)
            # attempt 3: 1000 * 2^3 = 8000ms → sleep(8.0)
            assert sleep_delays[0] == pytest.approx(1.0)
            assert sleep_delays[1] == pytest.approx(2.0)
            assert sleep_delays[2] == pytest.approx(4.0)
            assert sleep_delays[3] == pytest.approx(8.0)
        finally:
            vendor._rl_retry = dataclasses.replace(
                vendor._rl_retry, jitter=original_jitter
            )

    # ── API key 缺失 ──────────────────────────────────────

    @pytest.mark.asyncio
    async def test_missing_api_key_skips_retry(self):
        """API key 缺失时 401 快速失败，不触发 429 重试."""
        vendor = _make_zhipu_vendor(api_key="")
        resp = await vendor.send_message(
            {"model": "claude-sonnet-4-20250514", "messages": []},
            {},
        )
        assert resp.status_code == 401
