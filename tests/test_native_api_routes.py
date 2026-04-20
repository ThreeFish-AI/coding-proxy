"""``register_native_api_routes`` 集成测试.

覆盖：

- 三家 provider 各自在 FastAPI 上按预期路径 + 方法注册；
- 具体管理路由（``/api/status``）与 ``/api/{provider}/*`` catch-all 同 app 共存且互不吞路；
- 未启用的 provider 返回 404（防误配）；
- catch-all 的 method 枚举覆盖 GET/POST/PUT/PATCH/DELETE/HEAD/OPTIONS。
"""

from __future__ import annotations

import httpx
from fastapi import FastAPI
from fastapi.testclient import TestClient

from coding.proxy.native_api import NativeProxyHandler
from coding.proxy.native_api.config import NativeApiConfig, NativeProviderConfig
from coding.proxy.native_api.routes import register_native_api_routes


def _build_handler(*, upstream_body: dict | None = None):
    body = upstream_body or {
        "id": "x",
        "model": "m",
        "usage": {"prompt_tokens": 1, "completion_tokens": 2},
    }

    def route(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={**body, "_seen_path": str(request.url)})

    transport = httpx.MockTransport(route)
    cfg = NativeApiConfig(
        openai=NativeProviderConfig(enabled=True, base_url="https://api.openai.com"),
        gemini=NativeProviderConfig(
            enabled=True, base_url="https://generativelanguage.googleapis.com"
        ),
        anthropic=NativeProviderConfig(
            enabled=True, base_url="https://api.anthropic.com"
        ),
    )
    return NativeProxyHandler(cfg, transport=transport)


def test_three_providers_register_with_expected_paths() -> None:
    app = FastAPI()
    handler = _build_handler()
    register_native_api_routes(app, handler)

    paths = {route.path for route in app.router.routes if hasattr(route, "path")}
    assert "/api/openai/{rest_path:path}" in paths
    assert "/api/gemini/{rest_path:path}" in paths
    assert "/api/anthropic/{rest_path:path}" in paths


def test_three_providers_end_to_end() -> None:
    app = FastAPI()
    handler = _build_handler()
    register_native_api_routes(app, handler)

    with TestClient(app) as client:
        # OpenAI
        r = client.post("/api/openai/v1/chat/completions", json={})
        assert r.status_code == 200
        assert "api.openai.com/v1/chat/completions" in r.json()["_seen_path"]

        # Gemini (含 `:` 冒号方法后缀 — Starlette path converter 应透传)
        r = client.post(
            "/api/gemini/v1beta/models/gemini-2.0-flash:generateContent?key=abc",
            json={},
        )
        assert r.status_code == 200
        assert ":generateContent" in r.json()["_seen_path"]

        # Anthropic
        r = client.post("/api/anthropic/v1/messages", json={})
        assert r.status_code == 200
        assert "api.anthropic.com/v1/messages" in r.json()["_seen_path"]


def test_specific_route_not_shadowed_by_catchall() -> None:
    """``/api/status`` 这类具体路由**必须**先于 catch-all 注册且不被吞路."""
    app = FastAPI()

    @app.get("/api/status")
    def _status() -> dict:
        return {"status": "ok"}

    handler = _build_handler()
    register_native_api_routes(app, handler)

    with TestClient(app) as client:
        r = client.get("/api/status")
        assert r.status_code == 200
        assert r.json() == {"status": "ok"}

        # 同时 catch-all 仍能服务 openai 路径
        r2 = client.post("/api/openai/v1/models", json={})
        assert r2.status_code == 200


def test_unknown_provider_prefix_returns_404() -> None:
    """``/api/cohere/*`` 未注册 → FastAPI 404（与已注册但 disabled 的 provider 不同）."""
    app = FastAPI()
    handler = _build_handler()
    register_native_api_routes(app, handler)

    with TestClient(app) as client:
        r = client.post("/api/cohere/v1/chat", json={})
        assert r.status_code == 404


def test_method_enumeration_covers_all_http_verbs() -> None:
    """catch-all 应覆盖 GET/POST/PUT/PATCH/DELETE/HEAD/OPTIONS."""
    app = FastAPI()
    handler = _build_handler()
    register_native_api_routes(app, handler)

    for route in app.router.routes:
        if getattr(route, "path", "") == "/api/openai/{rest_path:path}":
            methods = set(route.methods or [])
            assert {
                "GET",
                "POST",
                "PUT",
                "PATCH",
                "DELETE",
                "HEAD",
                "OPTIONS",
            } <= methods
            return
    raise AssertionError("openai catch-all route not found")
