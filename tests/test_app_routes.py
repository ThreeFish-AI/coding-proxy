"""应用路由端点测试 — 根路径探针 & count_tokens 透传."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from fastapi.testclient import TestClient

from coding.proxy.backends.token_manager import TokenAcquireError
from coding.proxy.backends.base import BackendResponse, UsageInfo
from coding.proxy.config.schema import ProxyConfig
from coding.proxy.server.app import create_app


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


def test_count_tokens_no_anthropic_returns_404():
    """Anthropic 后端未启用时 count_tokens 返回 404."""
    with _make_app(primary_enabled=False) as client:
        resp = client.post(
            "/v1/messages/count_tokens",
            json={"model": "claude-sonnet-4-20250514", "messages": [{"role": "user", "content": "Hi"}]},
        )
        assert resp.status_code == 404
        data = resp.json()
        assert data["error"]["type"] == "not_found"


def test_count_tokens_proxies_to_anthropic():
    """count_tokens 正确透传到 Anthropic 后端."""
    mock_response = MagicMock()
    mock_response.content = b'{"input_tokens": 42}'
    mock_response.status_code = 200

    with _make_app(primary_enabled=True) as client:
        with patch.object(httpx.AsyncClient, "post", new_callable=AsyncMock, return_value=mock_response):
            resp = client.post(
                "/v1/messages/count_tokens?beta=true",
                json={"model": "claude-sonnet-4-20250514", "messages": [{"role": "user", "content": "Hi"}]},
                headers={"authorization": "Bearer sk-test"},
            )
            assert resp.status_code == 200
            assert resp.json()["input_tokens"] == 42


def test_count_tokens_upstream_timeout_returns_502():
    """上游超时时 count_tokens 返回 502."""
    with _make_app(primary_enabled=True) as client:
        with patch.object(
            httpx.AsyncClient, "post",
            new_callable=AsyncMock,
            side_effect=httpx.TimeoutException("timeout"),
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
    mock_response.content = b'{"error":{"type":"rate_limit_error","message":"Too many requests"}}'
    mock_response.status_code = 429

    with _make_app(primary_enabled=True) as client:
        with patch.object(httpx.AsyncClient, "post", new_callable=AsyncMock, return_value=mock_response):
            resp = client.post(
                "/v1/messages/count_tokens",
                json={"model": "claude-sonnet-4-20250514", "messages": []},
                headers={"authorization": "Bearer sk-test"},
            )
            assert resp.status_code == 429
            assert resp.json()["error"]["type"] == "rate_limit_error"


def test_status_exposes_backend_diagnostics():
    """状态接口暴露后端诊断信息，便于排查凭证交换异常."""
    config = ProxyConfig(
        copilot={"enabled": True, "github_token": "ghu_test"},
        fallback={"enabled": True},
        database={"path": "/tmp/test-coding-proxy-routes.db"},
    )
    app = create_app(config)

    for tier in app.state.router.tiers:
        if tier.name == "copilot":
            tier.backend._token_manager._record_error(  # type: ignore[attr-defined]
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


def test_copilot_diagnostics_endpoint_returns_backend_info():
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
            tier.backend.probe_models = AsyncMock(return_value={  # type: ignore[method-assign]
                "probe_status": "ok",
                "available_models": ["claude-opus-4.6"],
                "has_claude_opus_4_6": True,
            })
            break

    with TestClient(app) as client:
        resp = client.get("/api/copilot/models")
        assert resp.status_code == 200
        assert resp.json()["has_claude_opus_4_6"] is True


def test_incompatible_request_returns_400():
    """当所有可用后端都无法保持请求语义时，返回明确错误而不是误降级.

    通过 patch 后端能力声明模拟不兼容场景，验证 NoCompatibleBackendError → 400。
    """
    from coding.proxy.backends.base import BackendCapabilities

    config = ProxyConfig(
        primary={"enabled": False},
        fallback={"enabled": True},
        database={"path": "/tmp/test-coding-proxy-routes.db"},
    )
    app = create_app(config)

    # Patch 唯一可用后端的能力声明，使其拒绝 thinking 请求
    restrictive_caps = BackendCapabilities(
        supports_tools=True,
        supports_thinking=False,
        supports_images=True,
        supports_metadata=True,
    )
    with patch.object(
        type(app.state.router.tiers[0].backend),
        "get_capabilities",
        return_value=restrictive_caps,
    ):
        with TestClient(app) as client:
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
    app = create_app(ProxyConfig(
        primary={"enabled": False},
        fallback={"enabled": True},
        database={"path": "/tmp/test-coding-proxy-routes.db"},
    ))

    async def failing_route_stream(body, headers):
        request = httpx.Request("POST", "https://api.example.com/v1/messages")
        response = httpx.Response(
            429,
            content=b'{"error":{"type":"rate_limit_error","message":"Too many requests"}}',
            headers={"content-type": "application/json"},
            request=request,
        )
        raise httpx.HTTPStatusError("anthropic API error: 429", request=request, response=response)
        yield  # pragma: no cover

    app.state.router.route_stream = failing_route_stream

    with TestClient(app) as client:
        with client.stream(
            "POST",
            "/v1/messages",
            json={
                "model": "claude-sonnet-4-20250514",
                "messages": [{"role": "user", "content": "Hi"}],
                "stream": True,
            },
        ) as resp:
            body = b"".join(resp.iter_bytes()).decode()

    assert resp.status_code == 200
    assert "event: error" in body
    assert '"type": "rate_limit_error"' in body
    assert "Too many requests" in body


def test_messages_normalizes_vendor_tool_blocks_before_routing():
    """入口应先规范化 server_tool_use，再交给高优先级 tier."""
    app = create_app(ProxyConfig(
        primary={"enabled": True},
        fallback={"enabled": True},
        database={"path": "/tmp/test-coding-proxy-routes.db"},
    ))

    captured_body = {}

    async def fake_route_message(body, headers):
        captured_body["body"] = body
        return BackendResponse(status_code=200, raw_body=b"{}", usage=UsageInfo())

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
            {"backend": "anthropic", "enabled": True, "circuit_breaker": {"failure_threshold": 3}},
            {"backend": "zhipu", "enabled": True, "api_key": "sk-test"},
        ],
        database={"path": "/tmp/test-coding-proxy-routes.db"},
    )
    app = create_app(config)
    call_order: list[str] = []

    async def primary_route_message(body, headers):
        call_order.append("anthropic")
        return BackendResponse(status_code=200, raw_body=b"{}", usage=UsageInfo(input_tokens=1))

    async def fallback_route_message(body, headers):
        call_order.append("zhipu")
        return BackendResponse(status_code=200, raw_body=b"{}", usage=UsageInfo(input_tokens=1))

    app.state.router.tiers[0].backend.send_message = primary_route_message
    app.state.router.tiers[1].backend.send_message = fallback_route_message

    with TestClient(app) as client:
        reset_resp = client.post("/api/reset")
        assert reset_resp.status_code == 200
        resp = client.post(
            "/v1/messages",
            json={"model": "claude-sonnet-4-20250514", "messages": [{"role": "user", "content": "Hi"}]},
        )

    assert resp.status_code == 200
    assert call_order == ["anthropic"]
