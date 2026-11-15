"""SecurityMiddleware：L1 跨站拒绝、L2 管理面 Host 白名单、响应加固、流式兼容."""

from __future__ import annotations

import asyncio
import logging

import pytest
from fastapi import FastAPI, Response
from fastapi.responses import StreamingResponse
from fastapi.testclient import TestClient

from coding.proxy.config.schema import ProxyConfig
from coding.proxy.server.app import create_app
from coding.proxy.server.security import (
    SANDBOX_CSP,
    SecurityMiddleware,
    install_security,
    local_ip,
    normalize_allowed_hosts,
    split_host,
)

BASE = "http://127.0.0.1:3392"
NATIVE_PATH = "/api/openai/v1/models"


def _config(**extra) -> ProxyConfig:
    return ProxyConfig(
        vendors=[{"vendor": "anthropic", "enabled": True}],
        native_api={"openai": {"enabled": True, "base_url": "https://api.openai.com"}},
        **extra,
    )


@pytest.fixture
def app() -> FastAPI:
    app = create_app(_config())
    native = app.state.native_handler

    async def _dispatch(provider, rest_path, request):
        return Response(b'{"ok":true}', media_type="application/json")

    native.dispatch = _dispatch

    async def _route_message(body, headers):
        raise RuntimeError("not reached in guard tests")

    app.state.router.route_message = _route_message
    return app


@pytest.fixture
def client(app, strict_hosts):
    with TestClient(app, base_url=BASE) as c:
        yield c


# ── 解析 ────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("127.0.0.1:3392", ("127.0.0.1", 3392)),
        ("LOCALHOST.", ("localhost", None)),
        ("localhost.:3392", ("localhost", 3392)),
        ("[::1]:3392", ("::1", 3392)),
        ("[0:0:0:0:0:0:0:1]", ("::1", None)),
        ("::1", None),
        ("[::1", None),
        ("[::1]x", None),
        ("localhost:abc", None),
        ("localhost:99999", None),
        ("", None),
    ],
)
def test_split_host(value, expected):
    assert split_host(value) == expected


# ── L1：跨站拒绝（所有路径） ────────────────────────────────────


@pytest.mark.parametrize("site", ["cross-site", "same-site", "CROSS-SITE"])
@pytest.mark.parametrize(
    ("method", "path"), [("POST", "/v1/messages"), ("POST", "/api/reset")]
)
def test_cross_site_fetch_metadata_rejected(client, site, method, path):
    resp = client.request(method, path, headers={"Sec-Fetch-Site": site}, json={"x": 1})
    assert resp.status_code == 403
    assert resp.json()["error"]["type"] == "forbidden"


def test_cross_site_rejected_on_native_passthrough(client):
    resp = client.get(NATIVE_PATH, headers={"Sec-Fetch-Site": "cross-site"})
    assert resp.status_code == 403


@pytest.mark.parametrize(
    "origin",
    [
        "https://evil.example",
        "http://127.0.0.1:9999",
        "http://localhost:3392",  # 主机与请求 Host 不一致
        "null",
        "http://user@127.0.0.1:3392",
    ],
)
def test_foreign_origin_rejected(client, origin):
    for path in ("/v1/messages", "/api/reset", NATIVE_PATH):
        resp = client.post(path, headers={"Origin": origin})
        assert resp.status_code == 403, (path, origin)


def test_same_origin_allowed(client):
    resp = client.post(
        "/api/reset",
        headers={"Origin": BASE, "Sec-Fetch-Site": "same-origin"},
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"


def test_non_browser_clients_unaffected(client):
    """CLI / Claude Code 不发 Origin / Sec-Fetch-Site：空 body 的 reset 与 JSON 请求均放行."""
    assert client.post("/api/reset").status_code == 200
    assert client.post("/api/reset", json={"vendors": ["anthropic"]}).status_code == 200
    assert client.get(NATIVE_PATH).status_code == 200
    assert client.get("/health").status_code == 200


def test_sec_fetch_site_none_allowed(client):
    """地址栏直接打开（Sec-Fetch-Site: none）放行."""
    resp = client.get("/dashboard", headers={"Sec-Fetch-Site": "none"})
    assert resp.status_code == 200


# ── L2：管理面 Host 白名单 ──────────────────────────────────────


@pytest.mark.parametrize(
    "host", ["evil.example", "evil.example:3392", "127.0.0.1.evil.example", "[::2]"]
)
def test_bad_host_rejected_on_admin_surface(client, host):
    for path in ("/api/status", "/dashboard", "/api/config", "/static/x.js"):
        resp = client.get(path, headers={"Host": host})
        assert resp.status_code == 403, (path, host)


@pytest.mark.parametrize(
    "host", ["LOCALHOST.", "localhost:3392", "[::1]:3392", "127.0.0.1"]
)
def test_loopback_hosts_allowed(client, host):
    resp = client.get("/api/status", headers={"Host": host})
    assert resp.status_code == 200


def test_testserver_host_rejected_without_fixture(strict_hosts, app):
    with TestClient(app) as c:  # 默认 Host: testserver
        assert c.get("/api/status").status_code == 403


def test_native_passthrough_and_v1_not_host_checked(client):
    resp = client.get(NATIVE_PATH, headers={"Host": "evil.example"})
    assert resp.status_code == 200
    resp = client.get("/health", headers={"Host": "evil.example"})
    assert resp.status_code == 200


def test_allowed_hosts_extend_admin_surface(strict_hosts):
    app = create_app(_config(server={"allowed_hosts": ["Proxy.LAN."]}))
    with TestClient(app, base_url="http://proxy.lan:3392") as c:
        assert c.get("/api/status").status_code == 200
        assert c.get("/api/status", headers={"Host": "other.lan"}).status_code == 403
        resp = c.post("/api/reset", headers={"Origin": "http://proxy.lan:3392"})
        assert resp.status_code == 200


def test_allowed_hosts_normalized(caplog):
    entries = [
        "fd00::5",
        "FD00:0:0:0:0:0:0:5",
        "[fd00::6]:3392",
        "Proxy.LAN.:8080",
        "192.168.1.9",
        "evil example",
        "http://x.lan",
        "a/b",
        "",
    ]
    with caplog.at_level(logging.WARNING, logger="coding.proxy.server.security"):
        hosts = normalize_allowed_hosts(entries)
    assert hosts == {"fd00::5", "fd00::6", "proxy.lan", "192.168.1.9"}
    assert sum("server.allowed_hosts" in r.message for r in caplog.records) == 4


def test_unbracketed_ipv6_allowed_host(strict_hosts):
    app = create_app(_config(server={"allowed_hosts": ["FD00:0:0:0:0:0:0:5"]}))
    with TestClient(app, base_url=BASE) as c:
        assert (
            c.get("/api/status", headers={"Host": "[fd00::5]:3392"}).status_code == 200
        )
        assert c.get("/api/status", headers={"Host": "[fd00::6]"}).status_code == 403


def test_origin_default_ports_behind_tls_proxy(strict_hosts):
    """Host 无端口（经默认端口 / 终止 TLS 的反代）时 Origin 可用其 scheme 的默认端口."""
    app = create_app(_config(server={"allowed_hosts": ["proxy.lan"]}))
    with TestClient(app, base_url="http://proxy.lan") as c:
        for origin in (
            "https://proxy.lan",
            "http://proxy.lan",
            "https://proxy.lan:443",
        ):
            resp = c.post("/api/reset", headers={"Origin": origin})
            assert resp.status_code == 200, origin
        for origin in (
            "https://proxy.lan:8443",
            "http://proxy.lan:3392",
            "https://other.lan",
            "https://localhost",
        ):
            resp = c.post("/api/reset", headers={"Origin": origin})
            assert resp.status_code == 403, origin
    with TestClient(app, base_url="http://proxy.lan:3392") as c:
        assert (
            c.post("/api/reset", headers={"Origin": "https://proxy.lan"}).status_code
            == 403
        )


def test_direct_local_ip_host_allowed(strict_hosts):
    """``start -h <LAN IP>``：Host 为本连接的本机 IP 时放行（IP 字面量无法经 rebinding 伪造）."""
    app = create_app(_config())
    lan = "http://192.168.1.5:3392"
    with TestClient(app, base_url=lan) as c:
        assert c.get("/dashboard").status_code == 200
        assert c.get("/api/status").status_code == 200
        assert c.post("/api/reset", headers={"Origin": lan}).status_code == 200
        for host in ("192.168.1.6:3392", "evil.example:3392", "[::ffff:c0a8:105]"):
            assert c.get("/api/status", headers={"Host": host}).status_code == 403
    with TestClient(app, base_url="http://0.0.0.0:3392") as c:
        assert c.get("/api/status").status_code == 403


@pytest.mark.parametrize(
    ("server", "expected"),
    [
        (("192.168.1.5", 3392), "192.168.1.5"),
        (("::ffff:192.168.1.5", 3392), "192.168.1.5"),
        (("FD00::5", 3392), "fd00::5"),
        (("0.0.0.0", 3392), None),
        (("::", 3392), None),
        (("testserver", 80), None),
        (None, None),
    ],
)
def test_local_ip(server, expected):
    assert local_ip({"server": server}) == expected


def test_host_rejection_hints_cli():
    resp_body = SecurityMiddleware(None)._check(
        {"type": "http", "headers": [(b"host", b"evil.example")]}, "/api/status"
    )
    assert resp_body is not None and "server.allowed_hosts" in resp_body[2]


def test_body_write_requires_json(client):
    resp = client.post(
        "/api/reset", content=b"vendors=x", headers={"Content-Type": "text/plain"}
    )
    assert resp.status_code == 415
    resp = client.put(
        "/api/tier-order",
        content=b'{"vendors": ["anthropic"]}',
        headers={"Content-Type": "text/plain;charset=UTF-8"},
    )
    assert resp.status_code == 415


# ── 响应加固 ────────────────────────────────────────────────────


def test_hardening_headers(client):
    resp = client.get("/api/status")
    assert resp.headers["content-security-policy"] == SANDBOX_CSP
    assert resp.headers["x-content-type-options"] == "nosniff"
    resp = client.get(NATIVE_PATH)
    assert resp.headers["content-security-policy"] == SANDBOX_CSP
    rejected = client.get("/api/status", headers={"Host": "evil.example"})
    assert rejected.headers["content-security-policy"] == SANDBOX_CSP


def test_config_api_rejections_not_cached(client):
    resp = client.get("/api/config", headers={"Host": "evil.example"})
    assert resp.status_code == 403
    assert resp.headers["cache-control"] == "no-store"
    assert "cache-control" not in client.get("/api/status").headers


def test_unhandled_exception_500_is_hardened():
    """中间件位于 ServerErrorMiddleware 外层：未处理异常的 500 同样带加固头."""
    inner = FastAPI()

    @inner.get("/api/boom")
    async def boom() -> dict:
        raise KeyError("boom")

    @inner.get("/api/config")
    async def config_boom() -> dict:
        raise KeyError("boom")

    install_security(inner)
    with TestClient(inner, base_url=BASE, raise_server_exceptions=False) as c:
        for path in ("/api/boom", "/api/config"):
            resp = c.get(path)
            assert resp.status_code == 500
            assert resp.headers.get_list("content-security-policy") == [SANDBOX_CSP]
            assert resp.headers["x-content-type-options"] == "nosniff"
        assert resp.headers["cache-control"] == "no-store"


def test_dashboard_not_sandboxed(client):
    resp = client.get("/dashboard")
    assert resp.status_code == 200
    assert "sandbox" not in resp.headers.get("content-security-policy", "")


def test_hardening_replaces_upstream_headers():
    inner = FastAPI()

    @inner.get("/x")
    async def x() -> Response:
        return Response(
            b"<html></html>",
            media_type="text/html",
            headers={"Content-Security-Policy": "default-src *"},
        )

    install_security(inner)
    with TestClient(inner, base_url=BASE) as c:
        resp = c.get("/x")
    assert resp.headers.get_list("content-security-policy") == [SANDBOX_CSP]


def test_docs_disabled(client):
    for path in ("/docs", "/redoc", "/openapi.json", "/docs/oauth2-redirect"):
        assert client.get(path).status_code == 404


def test_request_validation_error_has_no_input():
    inner = FastAPI()

    @inner.get("/n")
    async def n(count: int) -> dict:
        return {"count": count}

    install_security(inner)
    with TestClient(inner, base_url=BASE) as c:
        resp = c.get("/n", params={"count": "PLANTED-not-an-int"})
    assert resp.status_code == 422
    assert "PLANTED" not in resp.text
    assert resp.json()["errors"][0]["loc"] == ["query", "count"]


# ── 流式响应 ────────────────────────────────────────────────────


async def test_streaming_passes_through_incrementally():
    """中间件只改 http.response.start，首个分块在生成器继续前即已下发（不缓冲）."""
    release = asyncio.Event()

    async def _gen():
        yield b"data: one\n\n"
        await release.wait()
        yield b"data: two\n\n"

    inner = FastAPI()

    @inner.get("/v1/stream")
    async def stream() -> StreamingResponse:
        return StreamingResponse(_gen(), media_type="text/event-stream")

    sent: list[dict] = []
    first_chunk = asyncio.Event()

    async def send(message: dict) -> None:
        sent.append(message)
        if message["type"] == "http.response.body" and message.get("body"):
            first_chunk.set()

    async def receive() -> dict:
        await asyncio.Event().wait()  # 客户端保持连接
        return {"type": "http.disconnect"}  # pragma: no cover

    scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.4"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": "/v1/stream",
        "raw_path": b"/v1/stream",
        "query_string": b"",
        "root_path": "",
        "headers": [(b"host", b"127.0.0.1:3392")],
        "client": ("127.0.0.1", 50000),
        "server": ("127.0.0.1", 3392),
    }
    task = asyncio.create_task(SecurityMiddleware(inner)(scope, receive, send))
    await asyncio.wait_for(first_chunk.wait(), timeout=2)
    start = sent[0]
    assert start["type"] == "http.response.start"
    assert (b"content-security-policy", SANDBOX_CSP.encode()) in start["headers"]
    assert [m.get("body") for m in sent[1:]] == [b"data: one\n\n"]
    release.set()
    await asyncio.wait_for(task, timeout=2)
    bodies = b"".join(m.get("body", b"") for m in sent[1:])
    assert bodies == b"data: one\n\ndata: two\n\n"


def test_sse_messages_stream_through_create_app(app, strict_hosts):
    async def _route_stream(body, headers, **kwargs):
        for i in range(3):
            yield f"event: ping\ndata: {i}\n\n".encode(), "anthropic"

    app.state.router.route_stream = _route_stream
    with (
        TestClient(app, base_url=BASE) as c,
        c.stream(
            "POST",
            "/v1/messages",
            json={"model": "m", "messages": [], "stream": True},
        ) as resp,
    ):
        body = b"".join(resp.iter_bytes()).decode()
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/event-stream")
    assert resp.headers["x-content-type-options"] == "nosniff"
    assert body.count("event: ping") == 3
