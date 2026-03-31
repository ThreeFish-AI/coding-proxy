"""应用路由端点测试 — 根路径探针 & count_tokens 透传."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from fastapi.testclient import TestClient

from coding.proxy.backends.token_manager import TokenAcquireError
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
        assert data["base_url"] == "https://api.githubcopilot.com"


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
    """当所有可用后端都无法保持工具语义时，返回明确错误而不是误降级."""
    with _make_app(primary_enabled=False) as client:
        resp = client.post(
            "/v1/messages",
            json={
                "model": "claude-sonnet-4-20250514",
                "messages": [{"role": "user", "content": "Hi"}],
                "tools": [{"name": "analyze_image"}],
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
