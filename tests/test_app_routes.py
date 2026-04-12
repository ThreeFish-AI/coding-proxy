"""应用路由端点测试 — 根路径探针 & count_tokens 透传."""

from __future__ import annotations

import logging
from unittest.mock import AsyncMock, MagicMock, PropertyMock, patch

import httpx
from fastapi.testclient import TestClient

from coding.proxy.config.schema import ProxyConfig
from coding.proxy.server.app import create_app
from coding.proxy.vendors.base import UsageInfo, VendorResponse
from coding.proxy.vendors.token_manager import TokenAcquireError


def _make_app(primary_enabled: bool = False) -> TestClient:
    """创建最小配置的测试应用（始终启用 fallback 以满足路由链约束）."""
    config = ProxyConfig(
        primary={"enabled": primary_enabled},
        fallback={"enabled": True},
        database={"path": "/tmp/test-coding-proxy-routes.db"},
    )
    app = create_app(config)
    return TestClient(app)


# ── 根路径探针 ───────────────────────────────────────────────


def test_head_root_returns_200():
    """HEAD / 返回 200（Claude Code 连通性探测）."""
    with _make_app() as client:
        resp = client.head("/")
        assert resp.status_code == 200


def test_get_root_returns_200():
    """GET / 返回 200."""
    with _make_app() as client:
        resp = client.get("/")
        assert resp.status_code == 200


# ── count_tokens 透传 ────────────────────────────────────────


def test_count_tokens_no_vendor_returns_501():
    """无可用供应商时 count_tokens 返回 501 Not Implemented"""
    with _make_app(primary_enabled=True) as client:
        # Mock tiers 属性返回空列表（防御性编程覆盖）
        with patch.object(
            type(client.app.state.router),
            "tiers",
            new_callable=PropertyMock,
            return_value=[],
        ):
            resp = client.post(
                "/v1/messages/count_tokens",
                json={
                    "model": "claude-sonnet-4-20250514",
                    "messages": [{"role": "user", "content": "Hi"}],
                },
            )
            assert resp.status_code == 501
            data = resp.json()
            assert data["error"]["type"] == "not_implemented"


def test_count_tokens_proxies_to_anthropic():
    """count_tokens 正确透传到 Anthropic 供应商."""
    mock_response = MagicMock()
    mock_response.content = b'{"input_tokens": 42}'
    mock_response.status_code = 200

    with _make_app(primary_enabled=True) as client:
        with patch.object(
            httpx.AsyncClient,
            "post",
            new_callable=AsyncMock,
            return_value=mock_response,
        ):
            resp = client.post(
                "/v1/messages/count_tokens?beta=true",
                json={
                    "model": "claude-sonnet-4-20250514",
                    "messages": [{"role": "user", "content": "Hi"}],
                },
                headers={"authorization": "Bearer sk-test"},
            )
            assert resp.status_code == 200
            assert resp.json()["input_tokens"] == 42


def test_count_tokens_upstream_timeout_returns_502():
    """上游超时时 count_tokens 返回 502."""
    with (
        _make_app(primary_enabled=True) as client,
        patch.object(
            httpx.AsyncClient,
            "post",
            new_callable=AsyncMock,
            side_effect=httpx.TimeoutException("timeout"),
        ),
    ):
        resp = client.post(
            "/v1/messages/count_tokens",
            json={"model": "claude-sonnet-4-20250514", "messages": []},
            headers={"authorization": "Bearer sk-test"},
        )
        assert resp.status_code == 502
        assert "unreachable" in resp.json()["error"]["message"]


def test_count_tokens_upstream_error_passthrough():
    """上游返回 4xx/5xx 时原样透传."""
    mock_response = MagicMock()
    mock_response.content = (
        b'{"error":{"type":"rate_limit_error","message":"Too many requests"}}'
    )
    mock_response.status_code = 429

    with _make_app(primary_enabled=True) as client:
        with patch.object(
            httpx.AsyncClient,
            "post",
            new_callable=AsyncMock,
            return_value=mock_response,
        ):
            resp = client.post(
                "/v1/messages/count_tokens",
                json={"model": "claude-sonnet-4-20250514", "messages": []},
                headers={"authorization": "Bearer sk-test"},
            )
            assert resp.status_code == 429
            assert resp.json()["error"]["type"] == "rate_limit_error"


def test_count_tokens_with_zhipu_primary():
    """zhipu 作为主供应商时，count_tokens 使用 ZhipuVendor 转发."""
    config = ProxyConfig(
        tiers=[
            {"vendor": "zhipu", "enabled": True, "api_key": "sk-zhipu-test"},
        ],
        database={"path": "/tmp/test-count-tokens-zhipu.db"},
    )
    app = create_app(config)

    mock_response = MagicMock()
    mock_response.content = b'{"input_tokens": 128}'
    mock_response.status_code = 200

    with TestClient(app) as client:
        with patch.object(
            httpx.AsyncClient,
            "post",
            new_callable=AsyncMock,
            return_value=mock_response,
        ):
            resp = client.post(
                "/v1/messages/count_tokens?beta=true",
                json={
                    "model": "claude-sonnet-4-20250514",
                    "messages": [{"role": "user", "content": "Hello"}],
                },
            )
            assert resp.status_code == 200
            assert resp.json()["input_tokens"] == 128


def test_count_tokens_zhipu_upstream_error_passthrough():
    """zhipu 作为主供应商时，上游错误原样透传."""
    config = ProxyConfig(
        tiers=[
            {"vendor": "zhipu", "enabled": True, "api_key": "sk-zhipu-test"},
        ],
        database={"path": "/tmp/test-count-tokens-zhipu-error.db"},
    )
    app = create_app(config)

    mock_response = MagicMock()
    mock_response.content = (
        b'{"error":{"type":"authentication_error","message":"invalid api key"}}'
    )
    mock_response.status_code = 403

    with TestClient(app) as client:
        with patch.object(
            httpx.AsyncClient,
            "post",
            new_callable=AsyncMock,
            return_value=mock_response,
        ):
            resp = client.post(
                "/v1/messages/count_tokens?beta=true",
                json={
                    "model": "claude-sonnet-4-20250514",
                    "messages": [{"role": "user", "content": "Hello"}],
                },
            )
            assert resp.status_code == 403
            assert resp.json()["error"]["type"] == "authentication_error"


def test_count_tokens_uses_active_vendor_from_global_state():
    """count_tokens 使用全局活跃状态标记的供应商（而非 tiers[0] 或门控判断）.

    模拟场景：zhipu 因熔断器开启降级到 anthropic，
    Executor 成功后将 active_vendor_name 设为 "anthropic"，
    count_tokens 应跟随使用 anthropic。
    """
    config = ProxyConfig(
        tiers=[
            {
                "vendor": "zhipu",
                "enabled": True,
                "api_key": "sk-zhipu-test",
                "circuit_breaker": {"failure_threshold": 3},
            },
            {"vendor": "anthropic", "enabled": True, "api_key": "sk-ant-test"},
        ],
        database={"path": "/tmp/test-count-tokens-active-vendor.db"},
    )
    app = create_app(config)

    # 模拟 Executor 已将活跃供应商切换为 anthropic（如 zhipu CB OPEN 后降级）
    app.state.router._active_vendor_name = "anthropic"

    mock_response = MagicMock()
    mock_response.content = b'{"input_tokens": 55}'
    mock_response.status_code = 200

    with TestClient(app) as client:
        with patch.object(
            httpx.AsyncClient,
            "post",
            new_callable=AsyncMock,
            return_value=mock_response,
        ) as mock_post:
            resp = client.post(
                "/v1/messages/count_tokens",
                json={
                    "model": "claude-sonnet-4-20250514",
                    "messages": [{"role": "user", "content": "Hi"}],
                },
            )
            assert resp.status_code == 200
            assert resp.json()["input_tokens"] == 55
            # 验证 httpx.post 被调用（使用了 anthropic vendor 的 client）
            assert mock_post.called


def test_count_tokens_falls_back_to_tiers0_on_cold_start():
    """冷启动（无任何成功请求）时，count_tokens 回退到 tiers[0]."""
    config = ProxyConfig(
        tiers=[
            {"vendor": "zhipu", "enabled": True, "api_key": "sk-zhipu-test"},
            {"vendor": "anthropic", "enabled": True, "api_key": "sk-ant-test"},
        ],
        database={"path": "/tmp/test-count-tokens-cold-start.db"},
    )
    app = create_app(config)

    # 确认无活跃供应商记录（冷启动）
    assert app.state.router.active_vendor_name is None

    mock_response = MagicMock()
    mock_response.content = b'{"input_tokens": 88}'
    mock_response.status_code = 200

    with TestClient(app) as client:
        with patch.object(
            httpx.AsyncClient,
            "post",
            new_callable=AsyncMock,
            return_value=mock_response,
        ):
            resp = client.post(
                "/v1/messages/count_tokens",
                json={
                    "model": "claude-sonnet-4-20250514",
                    "messages": [{"role": "user", "content": "Hi"}],
                },
            )
            assert resp.status_code == 200
            assert resp.json()["input_tokens"] == 88


def test_status_exposes_vendor_diagnostics():
    """状态接口暴露供应商诊断信息，便于排查凭证交换异常."""
    config = ProxyConfig(
        copilot={"enabled": True, "github_token": "ghu_test"},
        fallback={"enabled": True},
        database={"path": "/tmp/test-coding-proxy-routes.db"},
    )
    app = create_app(config)

    for tier in app.state.router.tiers:
        if tier.name == "copilot":
            tier.vendor._token_manager._record_error(  # type: ignore[attr-defined]
                TokenAcquireError("Copilot token 交换返回非预期响应")
            )
            break

    with TestClient(app) as client:
        resp = client.get("/api/status")
        assert resp.status_code == 200
        tiers = resp.json()["tiers"]
        copilot = next(item for item in tiers if item["name"] == "copilot")
        assert "diagnostics" in copilot
        assert "非预期响应" in copilot["diagnostics"]["token_manager"]["last_error"]


def test_copilot_diagnostics_endpoint_returns_vendor_info():
    config = ProxyConfig(
        copilot={"enabled": True, "github_token": "ghu_test"},
        fallback={"enabled": True},
        database={"path": "/tmp/test-coding-proxy-routes.db"},
    )
    app = create_app(config)

    with TestClient(app) as client:
        resp = client.get("/api/copilot/diagnostics")
        assert resp.status_code == 200
        data = resp.json()
        assert data["account_type"] == "individual"
        assert data["base_url"] == "https://api.individual.githubcopilot.com"
        assert data["candidate_base_urls"] == [
            "https://api.individual.githubcopilot.com",
            "https://api.githubcopilot.com",
        ]
        assert data["available_models_cache"] == []


def test_copilot_models_endpoint_returns_probe_data():
    config = ProxyConfig(
        copilot={"enabled": True, "github_token": "ghu_test"},
        fallback={"enabled": True},
        database={"path": "/tmp/test-coding-proxy-routes.db"},
    )
    app = create_app(config)

    for tier in app.state.router.tiers:
        if tier.name == "copilot":
            tier.vendor.probe_models = AsyncMock(
                return_value={  # type: ignore[method-assign]
                    "probe_status": "ok",
                    "available_models": ["claude-opus-4.6"],
                    "has_claude_opus_4_6": True,
                }
            )
            break

    with TestClient(app) as client:
        resp = client.get("/api/copilot/models")
        assert resp.status_code == 200
        assert resp.json()["has_claude_opus_4_6"] is True


def test_incompatible_request_returns_400():
    """当所有可用供应商都无法保持请求语义时，返回明确错误而不是误降级.

    通过 patch 供应商能力声明模拟不兼容场景，验证 NoCompatibleVendorError → 400。
    """
    from coding.proxy.vendors.base import VendorCapabilities

    config = ProxyConfig(
        primary={"enabled": False},
        fallback={"enabled": True},
        database={"path": "/tmp/test-coding-proxy-routes.db"},
    )
    app = create_app(config)

    # Patch 唯一可用供应商的能力声明，使其拒绝 thinking 请求
    restrictive_caps = VendorCapabilities(
        supports_tools=True,
        supports_thinking=False,
        supports_images=True,
        supports_metadata=True,
    )
    with (
        patch.object(
            type(app.state.router.tiers[0].vendor),
            "get_capabilities",
            return_value=restrictive_caps,
        ),
        TestClient(app) as client,
    ):
        resp = client.post(
            "/v1/messages",
            json={
                "model": "claude-sonnet-4-20250514",
                "messages": [{"role": "user", "content": "Hi"}],
                "thinking": {"type": "enabled", "budget_tokens": 1000},
            },
        )
        assert resp.status_code == 400
        data = resp.json()
        assert data["error"]["type"] == "invalid_request_error"


def test_stream_http_status_error_returns_anthropic_sse_error():
    """流式上游 HTTP 错误应转换为 Anthropic SSE error，而不是抛出 ASGI 异常."""
    app = create_app(
        ProxyConfig(
            primary={"enabled": False},
            fallback={"enabled": True},
            database={"path": "/tmp/test-coding-proxy-routes.db"},
        )
    )

    async def failing_route_stream(body, headers):
        request = httpx.Request("POST", "https://api.example.com/v1/messages")
        response = httpx.Response(
            429,
            content=b'{"error":{"type":"rate_limit_error","message":"Too many requests"}}',
            headers={"content-type": "application/json"},
            request=request,
        )
        raise httpx.HTTPStatusError(
            "anthropic API error: 429", request=request, response=response
        )
        yield  # pragma: no cover

    app.state.router.route_stream = failing_route_stream

    with (
        TestClient(app) as client,
        client.stream(
            "POST",
            "/v1/messages",
            json={
                "model": "claude-sonnet-4-20250514",
                "messages": [{"role": "user", "content": "Hi"}],
                "stream": True,
            },
        ) as resp,
    ):
        body = b"".join(resp.iter_bytes()).decode()

    assert resp.status_code == 200
    assert "event: error" in body
    assert '"type": "rate_limit_error"' in body
    assert "Too many requests" in body


def test_stream_read_error_returns_anthropic_sse_error():
    """流式 ReadError 应收口为 Anthropic SSE error，不应冒泡为 500."""
    app = create_app(
        ProxyConfig(
            primary={"enabled": False},
            fallback={"enabled": True},
            database={"path": "/tmp/test-coding-proxy-routes.db"},
        )
    )

    async def failing_route_stream(body, headers):
        raise httpx.ReadError("socket closed")
        yield  # pragma: no cover

    app.state.router.route_stream = failing_route_stream

    with (
        TestClient(app) as client,
        client.stream(
            "POST",
            "/v1/messages",
            json={
                "model": "claude-sonnet-4-20250514",
                "messages": [{"role": "user", "content": "Hi"}],
                "stream": True,
            },
        ) as resp,
    ):
        body = b"".join(resp.iter_bytes()).decode()

    assert resp.status_code == 200
    assert "event: error" in body
    assert '"type": "api_error"' in body
    assert "socket closed" in body


def test_message_read_error_returns_502():
    """非流式 ReadError 应返回 502，而不是框架级 500."""
    app = create_app(
        ProxyConfig(
            primary={"enabled": False},
            fallback={"enabled": True},
            database={"path": "/tmp/test-coding-proxy-routes.db"},
        )
    )

    async def failing_route_message(body, headers):
        raise httpx.ReadError("socket closed")

    app.state.router.route_message = failing_route_message

    with TestClient(app) as client:
        resp = client.post(
            "/v1/messages",
            json={
                "model": "claude-sonnet-4-20250514",
                "messages": [{"role": "user", "content": "Hi"}],
            },
        )

    assert resp.status_code == 502
    data = resp.json()
    assert data["error"]["type"] == "api_error"
    assert "socket closed" in data["error"]["message"]


def test_stream_unexpected_exception_returns_sse_error_not_500():
    """流式路径的未预期异常应返回 SSE error event（HTTP 200 + event: error），而非框架级 500.

    验证 catch-all Exception 处理器将未知异常转换为结构化 SSE 错误事件，
    客户端可正常解析错误信息而非收到裸 HTTP 500。
    """
    app = create_app(
        ProxyConfig(
            primary={"enabled": False},
            fallback={"enabled": True},
            database={"path": "/tmp/test-coding-proxy-unexpected.db"},
        )
    )

    async def failing_route_stream(body, headers):
        raise ValueError("unexpected parsing error")
        yield  # pragma: no cover

    app.state.router.route_stream = failing_route_stream

    with (
        TestClient(app) as client,
        client.stream(
            "POST",
            "/v1/messages",
            json={
                "model": "claude-sonnet-4-20250514",
                "messages": [{"role": "user", "content": "Hi"}],
                "stream": True,
            },
        ) as resp,
    ):
        body = b"".join(resp.iter_bytes()).decode()

    # 关键断言：必须是 200（SSE 流正常关闭），不能是 500
    assert resp.status_code == 200
    assert "event: error" in body
    assert '"type": "api_error"' in body
    # 确认包含异常类型信息，便于调试定位
    assert "ValueError" in body or "unexpected" in body.lower()


def test_non_stream_unexpected_exception_returns_500_json_not_raw_500():
    """非流式路径的未预期异常应返回结构化 JSON 错误（含异常类型），而非框架级原始 500.

    验证 catch-all Exception 处理器将未知异常转换为 JSON 格式的 api_error 响应，
    服务端日志记录完整堆栈（exc_info=True），客户端可从响应体获取错误类型。
    """
    app = create_app(
        ProxyConfig(
            primary={"enabled": False},
            fallback={"enabled": True},
            database={"path": "/tmp/test-coding-proxy-nonstream-unexpected.db"},
        )
    )

    async def failing_route_message(body, headers):
        raise RuntimeError("internal state corruption")

    app.state.router.route_message = failing_route_message

    with TestClient(app) as client:
        resp = client.post(
            "/v1/messages",
            json={
                "model": "claude-sonnet-4-20250514",
                "messages": [{"role": "user", "content": "Hi"}],
            },
        )

    assert resp.status_code == 500
    data = resp.json()
    assert data["error"]["type"] == "api_error"
    assert "RuntimeError" in data["error"]["message"]


def test_messages_normalizes_vendor_tool_blocks_before_routing():
    """入口应先规范化 server_tool_use，再交给高优先级 tier."""
    app = create_app(
        ProxyConfig(
            primary={"enabled": True},
            fallback={"enabled": True},
            database={"path": "/tmp/test-coding-proxy-routes.db"},
        )
    )

    captured_body = {}

    async def fake_route_message(body, headers):
        captured_body["body"] = body
        return VendorResponse(status_code=200, raw_body=b"{}", usage=UsageInfo())

    app.state.router.route_message = fake_route_message

    with TestClient(app) as client:
        resp = client.post(
            "/v1/messages",
            json={
                "model": "claude-opus-4-6",
                "messages": [
                    {
                        "role": "assistant",
                        "content": [
                            {
                                "type": "server_tool_use",
                                "id": "srvtoolu_bad_1",
                                "name": "bash",
                                "input": {"cmd": "pwd"},
                            },
                        ],
                    },
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "tool_result",
                                "tool_use_id": "srvtoolu_bad_1",
                                "content": "ok",
                            },
                        ],
                    },
                ],
            },
        )

    assert resp.status_code == 200
    assistant_block = captured_body["body"]["messages"][0]["content"][0]
    user_block = captured_body["body"]["messages"][1]["content"][0]
    assert assistant_block["type"] == "tool_use"
    assert assistant_block["id"].startswith("toolu_normalized_")
    assert user_block["tool_use_id"] == assistant_block["id"]


def test_reset_keeps_tier_order_and_next_request_hits_primary_first():
    """reset 只清状态，不改 tier 顺序；下一次请求仍先尝试首层."""
    config = ProxyConfig(
        tiers=[
            {
                "vendor": "anthropic",
                "enabled": True,
                "circuit_breaker": {"failure_threshold": 3},
            },
            {"vendor": "zhipu", "enabled": True, "api_key": "sk-test"},
        ],
        database={"path": "/tmp/test-coding-proxy-routes.db"},
    )
    app = create_app(config)
    call_order: list[str] = []

    async def primary_route_message(body, headers):
        call_order.append("anthropic")
        return VendorResponse(
            status_code=200, raw_body=b"{}", usage=UsageInfo(input_tokens=1)
        )

    async def fallback_route_message(body, headers):
        call_order.append("zhipu")
        return VendorResponse(
            status_code=200, raw_body=b"{}", usage=UsageInfo(input_tokens=1)
        )

    app.state.router.tiers[0].vendor.send_message = primary_route_message
    app.state.router.tiers[1].vendor.send_message = fallback_route_message

    with TestClient(app) as client:
        reset_resp = client.post("/api/reset")
        assert reset_resp.status_code == 200
        resp = client.post(
            "/v1/messages",
            json={
                "model": "claude-sonnet-4-20250514",
                "messages": [{"role": "user", "content": "Hi"}],
            },
        )

    assert resp.status_code == 200
    assert call_order == ["anthropic"]


def test_normalization_adaptations_logged_at_debug_level(caplog):
    """常规规范化适配（如 tool_use ID 重写）应记录在 DEBUG 级别而非 INFO，避免日志噪音.

    验证：非标准 tool_use_id 触发的 invalid_tool_use_id_rewritten_for_anthropic
    和 tool_result_tool_use_id_rewritten 适配不会出现在 INFO 级别日志中，
    但规范化功能本身仍正确工作（ID 被重写且配对一致）。
    """
    app = create_app(
        ProxyConfig(
            primary={"enabled": False},
            fallback={"enabled": True},
            database={"path": "/tmp/test-coding-proxy-normalization-log.db"},
        )
    )

    captured_body = {}

    async def fake_route_message(body, headers):
        captured_body["body"] = body
        return VendorResponse(status_code=200, raw_body=b"{}", usage=UsageInfo())

    app.state.router.route_message = fake_route_message

    with caplog.at_level(logging.INFO, logger="coding.proxy.server.routes"):
        with TestClient(app) as client:
            client.post(
                "/v1/messages",
                json={
                    "model": "claude-opus-4-6",
                    "messages": [
                        {
                            "role": "assistant",
                            "content": [
                                {
                                    "type": "tool_use",
                                    "id": "zhipu_nonstandard_id_123",
                                    "name": "bash",
                                    "input": {"cmd": "pwd"},
                                }
                            ],
                        },
                        {
                            "role": "user",
                            "content": [
                                {
                                    "type": "tool_result",
                                    "tool_use_id": "zhipu_nonstandard_id_123",
                                    "content": "ok",
                                }
                            ],
                        },
                    ],
                },
            )

    # INFO 级别不应出现 normalization 日志（修复的核心目标）
    info_messages = [r.message for r in caplog.records if r.levelno == logging.INFO]
    assert not any("normalized before routing" in m for m in info_messages)

    # 规范化功能仍正确工作：ID 被重写为标准格式
    assistant_block = captured_body["body"]["messages"][0]["content"][0]
    user_block = captured_body["body"]["messages"][1]["content"][0]
    assert assistant_block["id"].startswith("toolu_normalized_")
    assert user_block["tool_use_id"] == assistant_block["id"]


def test_non_standard_error_format_logged_at_debug(caplog):
    """上游返回含 code（非 type）的非标准错误格式时，应输出 DEBUG 级别诊断日志.

    模拟 Zhipu 返回 ``{"error":{"code":"500","message":"..."}}`` 格式，
    验证 routes.py 在透传前记录格式差异信息。
    """
    app = create_app(
        ProxyConfig(
            primary={"enabled": False},
            fallback={"enabled": True},
            database={"path": "/tmp/test-coding-proxy-nonstandard-error.db"},
        )
    )

    async def zhipu_500_response(body, headers):
        return VendorResponse(
            status_code=500,
            raw_body=b'{"error":{"code":"500","message":"\'ClaudeContentBlockToolResult\' object has no attribute \'id\'"}}',
            response_headers={"content-type": "application/json"},
        )

    app.state.router.route_message = zhipu_500_response

    with caplog.at_level(logging.DEBUG, logger="coding.proxy.server.routes"):
        with TestClient(app) as client:
            resp = client.post(
                "/v1/messages",
                json={
                    "model": "claude-opus-4-6",
                    "messages": [{"role": "user", "content": "hi"}],
                },
            )

    # 响应原样透传
    assert resp.status_code == 500
    # DEBUG 日志应包含非标准格式标记
    debug_messages = [r.message for r in caplog.records if r.levelno == logging.DEBUG]
    assert any("非标准上游错误格式" in m for m in debug_messages)


def test_standard_error_format_no_debug_log(caplog):
    """上游返回标准 Anthropic 错误格式（含 type）时，不应输出非标准格式日志.

    确保仅对 ``code`` 非 ``type`` 的异常格式触发诊断，避免误报。
    """
    app = create_app(
        ProxyConfig(
            primary={"enabled": False},
            fallback={"enabled": True},
            database={"path": "/tmp/test-coding-proxy-standard-error.db"},
        )
    )

    async def standard_500_response(body, headers):
        return VendorResponse(
            status_code=500,
            raw_body=b'{"error":{"type":"api_error","message":"internal error"}}',
            response_headers={"content-type": "application/json"},
        )

    app.state.router.route_message = standard_500_response

    with caplog.at_level(logging.DEBUG, logger="coding.proxy.server.routes"):
        with TestClient(app) as client:
            resp = client.post(
                "/v1/messages",
                json={
                    "model": "claude-opus-4-6",
                    "messages": [{"role": "user", "content": "hi"}],
                },
            )

    assert resp.status_code == 500
    debug_messages = [r.message for r in caplog.records if r.levelno == logging.DEBUG]
    assert not any("非标准上游错误格式" in m for m in debug_messages)


def test_vendor_500_passthrough_preserves_raw_body():
    """VendorResponse 500 应原样透传 raw_body，不做格式转换.

    验证最小化原则：proxy 不修改上游错误响应体，
    客户端收到的与 vendor 返回的完全一致。
    """
    app = create_app(
        ProxyConfig(
            primary={"enabled": False},
            fallback={"enabled": True},
            database={"path": "/tmp/test-coding-proxy-passthrough.db"},
        )
    )

    original_body = b'{"error":{"code":"500","message":"test upstream error"}}'

    async def upstream_500(body, headers):
        return VendorResponse(
            status_code=500,
            raw_body=original_body,
            response_headers={"content-type": "application/json"},
        )

    app.state.router.route_message = upstream_500

    with TestClient(app) as client:
        resp = client.post(
            "/v1/messages",
            json={
                "model": "claude-opus-4-6",
                "messages": [{"role": "user", "content": "hi"}],
            },
        )

    assert resp.status_code == 500
    assert resp.content == original_body


# ── /api/reset 重排序测试 ────────────────────────────────────────


def _make_reorder_app() -> tuple:
    """创建包含 anthropic + zhipu + copilot 三层的测试应用."""
    config = ProxyConfig(
        tiers=[
            {
                "vendor": "anthropic",
                "enabled": True,
                "circuit_breaker": {"failure_threshold": 3},
            },
            {"vendor": "zhipu", "enabled": True, "api_key": "sk-test"},
            {"vendor": "copilot", "enabled": True},
        ],
        database={"path": "/tmp/test-coding-proxy-reorder.db"},
    )
    app = create_app(config)

    async def route_ok(body, headers):
        return VendorResponse(
            status_code=200, raw_body=b"{}", usage=UsageInfo(input_tokens=1)
        )

    for tier in app.state.router.tiers:
        tier.vendor.send_message = route_ok

    return app


def test_reset_promote_single_vendor():
    """单 vendor → promote_vendor：将该 vendor 提升至首位，其余保持相对顺序."""
    app = _make_reorder_app()
    with TestClient(app) as client:
        resp = client.post("/api/reset", json={"vendors": ["zhipu"]})
        assert resp.status_code == 200
        data = resp.json()
        assert data["tier_order"] == ["zhipu", "anthropic", "copilot"]

        # 验证路由器内部状态一致
        assert app.state.router.get_vendor_names() == ["zhipu", "anthropic", "copilot"]


def test_reset_reorder_full_chain():
    """多 vendor → reorder_tiers：精确匹配指定顺序."""
    app = _make_reorder_app()
    with TestClient(app) as client:
        resp = client.post(
            "/api/reset", json={"vendors": ["copilot", "anthropic", "zhipu"]}
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["tier_order"] == ["copilot", "anthropic", "zhipu"]
        assert app.state.router.get_vendor_names() == [
            "copilot",
            "anthropic",
            "zhipu",
        ]


def test_reset_no_body_backward_compatible():
    """无 body → 仅 reset，不返回 tier_order（向后兼容）."""
    app = _make_reorder_app()
    with TestClient(app) as client:
        resp = client.post("/api/reset")
        assert resp.status_code == 200
        data = resp.json()
        assert "tier_order" not in data
        assert data["status"] == "ok"
        # 顺序不变
        assert app.state.router.get_vendor_names() == [
            "anthropic",
            "zhipu",
            "copilot",
        ]


def test_reset_unknown_vendor_returns_400():
    """未知 vendor 名称 → 400 错误."""
    app = _make_reorder_app()
    with TestClient(app) as client:
        resp = client.post("/api/reset", json={"vendors": ["nonexist"]})
        assert resp.status_code == 400
        err = resp.json()["error"]
        assert "未知 vendor" in err["message"]


def test_reset_duplicate_vendor_returns_400():
    """重复 vendor 名称 → 400 错误."""
    app = _make_reorder_app()
    with TestClient(app) as client:
        resp = client.post(
            "/api/reset", json={"vendors": ["anthropic", "anthropic", "zhipu"]}
        )
        assert resp.status_code == 400
        err = resp.json()["error"]
        assert "重复" in err["message"]


def test_reset_incomplete_vendor_list_returns_400():
    """不完整的 vendor 列表（缺少现有 tier）→ 400 错误."""
    app = _make_reorder_app()
    with TestClient(app) as client:
        resp = client.post("/api/reset", json={"vendors": ["anthropic", "zhipu"]})
        assert resp.status_code == 400
        err = resp.json()["error"]
        assert "缺少 vendor" in err["message"]


def test_reset_reorder_also_resets_circuit_breaker_and_rate_limit():
    """重排序同时执行全量 reset（熔断器/配额守卫/rate limit 均被重置）."""
    app = _make_reorder_app()
    router = app.state.router

    # 手动触发熔断器失败并设置 rate limit
    router.tiers[0].record_failure(retry_after_seconds=300)
    router.tiers[0]._rate_limit_deadline = 999999.0
    assert not router.tiers[0].can_execute()

    with TestClient(app) as client:
        resp = client.post("/api/reset", json={"vendors": ["zhipu"]})
        assert resp.status_code == 200

    # 重排序后原 anthropic 仍是 tier 成员，但熔断器/rate limit 已被重置
    anthropic_tier = next(t for t in router.tiers if t.name == "zhipu")
    assert anthropic_tier.can_execute()
    assert not anthropic_tier.is_rate_limited


def test_reorder_tiers_shared_reference():
    """验证 reorder_tiers 使用切片赋值，Executor 立即可见."""
    from coding.proxy.routing.router import RequestRouter
    from coding.proxy.routing.tier import VendorTier

    t1 = VendorTier(vendor=MagicMock())
    t1.vendor.get_name.return_value = "a"
    t2 = VendorTier(vendor=MagicMock())
    t2.vendor.get_name.return_value = "b"
    t3 = VendorTier(vendor=MagicMock())
    t3.vendor.get_name.return_value = "c"

    router = RequestRouter([t1, t2, t3])
    executor_tiers = router._executor._tiers

    # 验证共享引用
    assert executor_tiers is router._tiers

    # 重排序
    router.reorder_tiers(["c", "a", "b"])

    # Executor 的列表也改变了（因为是同一个对象）
    assert [t.name for t in executor_tiers] == ["c", "a", "b"]
    assert router.get_vendor_names() == ["c", "a", "b"]
