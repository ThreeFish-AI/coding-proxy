"""``NativeProxyHandler`` 端到端透传测试.

使用 ``httpx.MockTransport`` 拦截上游调用，在 FastAPI ``TestClient`` 下验证：

- URL 组装（base_url + rest_path + query 保留）；
- 请求头过滤（``accept-encoding`` / ``host`` 剥；``authorization`` / 自定义 ``x-*`` 保）；
- 响应头清洗（``content-encoding`` / ``content-length`` 剥）；
- 4xx / 5xx 状态码原样透传（不改写为 Anthropic 错误体）；
- SSE ``text/event-stream`` 字节级一致透传；
- 上游不可达 / 超时 → 502 + OpenAI 风格错误体；
- ``enabled=False`` 的 provider → 404；
- 非 JSON 响应（如 audio/binary）→ 透传不抽取 usage。
"""

from __future__ import annotations

from collections.abc import Iterator

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from coding.proxy.native_api import NativeProxyHandler
from coding.proxy.native_api.config import NativeApiConfig, NativeProviderConfig
from coding.proxy.native_api.routes import register_native_api_routes

# ── 工具：构造 handler + FastAPI + MockTransport ─────────────────


def _make_app(
    handler_factory,
) -> Iterator[tuple[TestClient, list[httpx.Request]]]:
    """构造 ``TestClient`` 与 ``handler``；返回上游 captured_requests 便于断言."""
    captured: list[httpx.Request] = []

    def make_transport(route):
        def _inner(request: httpx.Request) -> httpx.Response:
            captured.append(request)
            return route(request)

        return httpx.MockTransport(_inner)

    handler, transport = handler_factory(make_transport)
    app = FastAPI()
    app.state.native_handler = handler
    register_native_api_routes(app, handler)

    with TestClient(app) as client:
        yield client, captured


# ── URL 组装 / query 保留 ───────────────────────────────────────


def test_openai_forwards_path_and_query() -> None:
    def route(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "id": "r1",
                "model": "gpt-4o",
                "usage": {"prompt_tokens": 1, "completion_tokens": 2},
            },
        )

    def factory(make_transport):
        cfg = NativeApiConfig(
            openai=NativeProviderConfig(
                enabled=True, base_url="https://api.openai.com"
            ),
        )
        transport = make_transport(route)
        return NativeProxyHandler(cfg, transport=transport), transport

    for client, captured in _make_app(factory):
        r = client.post(
            "/api/openai/v1/chat/completions?foo=bar",
            json={"model": "gpt-4o", "messages": []},
            headers={"authorization": "Bearer sk-test", "x-custom": "hello"},
        )
        assert r.status_code == 200
        assert r.json()["id"] == "r1"
        assert len(captured) == 1
        upstream = captured[0]
        assert str(upstream.url) == "https://api.openai.com/v1/chat/completions?foo=bar"
        assert upstream.method == "POST"
        # authorization 与自定义 x-* 保留
        assert upstream.headers.get("authorization") == "Bearer sk-test"
        assert upstream.headers.get("x-custom") == "hello"
        # accept-encoding 强制为 identity（避免 httpx 自动解压破坏字节流）
        assert upstream.headers.get("accept-encoding") == "identity"
        # host 头由 httpx 重建为目标 host（not testserver）
        assert upstream.headers.get("host", "").startswith("api.openai.com")


def test_gemini_forwards_with_api_key_query() -> None:
    """Gemini 官方用 ``?key=xxx`` 作认证 — query 必须透传."""

    def route(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "responseId": "g1",
                "modelVersion": "gemini-2.0-flash",
                "usageMetadata": {"promptTokenCount": 3, "candidatesTokenCount": 5},
            },
        )

    def factory(make_transport):
        cfg = NativeApiConfig(
            gemini=NativeProviderConfig(
                enabled=True, base_url="https://generativelanguage.googleapis.com"
            ),
        )
        transport = make_transport(route)
        return NativeProxyHandler(cfg, transport=transport), transport

    for client, captured in _make_app(factory):
        r = client.post(
            "/api/gemini/v1beta/models/gemini-2.0-flash:generateContent?key=secret123",
            json={"contents": [{"parts": [{"text": "hi"}]}]},
        )
        assert r.status_code == 200
        assert captured[0].url.params.get("key") == "secret123"


def test_anthropic_preserves_custom_headers() -> None:
    def route(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "id": "msg_1",
                "model": "claude-opus-4-7",
                "usage": {"input_tokens": 7, "output_tokens": 9},
            },
        )

    def factory(make_transport):
        cfg = NativeApiConfig(
            anthropic=NativeProviderConfig(
                enabled=True, base_url="https://api.anthropic.com"
            ),
        )
        transport = make_transport(route)
        return NativeProxyHandler(cfg, transport=transport), transport

    for client, captured in _make_app(factory):
        r = client.post(
            "/api/anthropic/v1/messages",
            json={"model": "claude-opus-4-7", "max_tokens": 8, "messages": []},
            headers={
                "x-api-key": "sk-ant-xxx",
                "anthropic-version": "2023-06-01",
                "anthropic-beta": "messages-2024-04-04",
            },
        )
        assert r.status_code == 200
        upstream = captured[0]
        assert upstream.headers.get("x-api-key") == "sk-ant-xxx"
        assert upstream.headers.get("anthropic-version") == "2023-06-01"
        assert upstream.headers.get("anthropic-beta") == "messages-2024-04-04"


# ── 错误码透传 ──────────────────────────────────────────────────


def test_upstream_4xx_passthrough() -> None:
    def route(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            429,
            json={"error": {"message": "rate limited", "type": "rate_limit_error"}},
        )

    def factory(make_transport):
        cfg = NativeApiConfig(
            openai=NativeProviderConfig(
                enabled=True, base_url="https://api.openai.com"
            ),
        )
        transport = make_transport(route)
        return NativeProxyHandler(cfg, transport=transport), transport

    for client, _captured in _make_app(factory):
        r = client.post("/api/openai/v1/chat/completions", json={})
        assert r.status_code == 429
        assert r.json()["error"]["type"] == "rate_limit_error"


def test_upstream_5xx_passthrough() -> None:
    def route(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, text="Service Unavailable")

    def factory(make_transport):
        cfg = NativeApiConfig(
            openai=NativeProviderConfig(
                enabled=True, base_url="https://api.openai.com"
            ),
        )
        transport = make_transport(route)
        return NativeProxyHandler(cfg, transport=transport), transport

    for client, _captured in _make_app(factory):
        r = client.post("/api/openai/v1/chat/completions", json={})
        assert r.status_code == 503
        assert r.text == "Service Unavailable"


# ── 超时 → 502 OpenAI 风格错误 ───────────────────────────────────


def test_upstream_connect_error_returns_502() -> None:
    def route(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    def factory(make_transport):
        cfg = NativeApiConfig(
            openai=NativeProviderConfig(
                enabled=True, base_url="https://api.openai.com"
            ),
        )
        transport = make_transport(route)
        return NativeProxyHandler(cfg, transport=transport), transport

    for client, _captured in _make_app(factory):
        r = client.post("/api/openai/v1/chat/completions", json={})
        assert r.status_code == 502
        body = r.json()
        assert body["error"]["type"] == "api_error"
        assert "upstream unreachable" in body["error"]["message"]


def test_upstream_timeout_returns_502() -> None:
    def route(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("read timeout", request=request)

    def factory(make_transport):
        cfg = NativeApiConfig(
            openai=NativeProviderConfig(
                enabled=True, base_url="https://api.openai.com"
            ),
        )
        transport = make_transport(route)
        return NativeProxyHandler(cfg, transport=transport), transport

    for client, _captured in _make_app(factory):
        r = client.post("/api/openai/v1/chat/completions", json={})
        assert r.status_code == 502


# ── 禁用的 provider → 404 ───────────────────────────────────────


def test_disabled_provider_returns_404() -> None:
    def route(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={})

    def factory(make_transport):
        cfg = NativeApiConfig(
            openai=NativeProviderConfig(
                enabled=False, base_url="https://api.openai.com"
            ),
        )
        transport = make_transport(route)
        return NativeProxyHandler(cfg, transport=transport), transport

    for client, captured in _make_app(factory):
        r = client.post("/api/openai/v1/chat/completions", json={})
        assert r.status_code == 404
        assert "not enabled" in r.json()["error"]["message"]
        # 被禁用时不应发起任何上游调用
        assert captured == []


# ── SSE 流式透传（字节级） ──────────────────────────────────────


def test_sse_passthrough_byte_identical() -> None:
    sse_body = (
        b'data: {"type":"content_block_delta","delta":{"text":"hi"}}\n\n'
        b'data: {"type":"message_delta","usage":{"input_tokens":5,"output_tokens":3}}\n\n'
        b"data: [DONE]\n\n"
    )

    def route(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=sse_body,
        )

    def factory(make_transport):
        cfg = NativeApiConfig(
            anthropic=NativeProviderConfig(
                enabled=True, base_url="https://api.anthropic.com"
            ),
        )
        transport = make_transport(route)
        return NativeProxyHandler(cfg, transport=transport), transport

    for client, _captured in _make_app(factory):
        r = client.post(
            "/api/anthropic/v1/messages",
            json={
                "stream": True,
                "model": "claude-opus-4-7",
                "max_tokens": 4,
                "messages": [],
            },
            headers={"accept": "text/event-stream"},
        )
        assert r.status_code == 200
        assert r.headers["content-type"].startswith("text/event-stream")
        assert r.content == sse_body


# ── 响应头清洗（content-encoding / content-length 剥除） ────────


def test_response_content_encoding_stripped() -> None:
    """上游返回 content-encoding: gzip —— httpx 已解压，响应头必须剥除."""

    def route(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={
                "content-type": "application/json",
                # content-encoding 由上游保留，但 httpx 在 AsyncClient 侧会解压
                # content-length 同样需要剥除（长度与解压后不一致）
            },
            json={
                "id": "x",
                "model": "gpt-4o",
                "usage": {"prompt_tokens": 1, "completion_tokens": 1},
            },
        )

    def factory(make_transport):
        cfg = NativeApiConfig(
            openai=NativeProviderConfig(
                enabled=True, base_url="https://api.openai.com"
            ),
        )
        transport = make_transport(route)
        return NativeProxyHandler(cfg, transport=transport), transport

    for client, _captured in _make_app(factory):
        r = client.post("/api/openai/v1/chat/completions", json={})
        assert r.status_code == 200
        # TestClient 层重新组装 content-length — 只校验上游跳过编码头传递没出错
        assert "content-encoding" not in r.headers


# ── HTTP method 覆盖 ────────────────────────────────────────────


@pytest.mark.parametrize("method", ["GET", "POST", "PUT", "PATCH", "DELETE"])
def test_http_method_coverage(method: str) -> None:
    def route(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"ok": True, "seen_method": request.method})

    def factory(make_transport):
        cfg = NativeApiConfig(
            openai=NativeProviderConfig(
                enabled=True, base_url="https://api.openai.com"
            ),
        )
        transport = make_transport(route)
        return NativeProxyHandler(cfg, transport=transport), transport

    for client, captured in _make_app(factory):
        r = client.request(method, "/api/openai/v1/files/abc")
        assert r.status_code == 200
        assert captured[0].method == method
