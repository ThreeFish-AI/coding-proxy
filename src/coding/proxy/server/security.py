"""HTTP 安全边界：跨站拒绝（L1）、管理面 Host 白名单（L2）、非 Dashboard 响应加固.

- L1（所有路径，含 ``/v1/*`` 与原生透传）：``Sec-Fetch-Site`` 为 cross-site / same-site，
  或带 ``Origin`` 但其主机不在白名单 / 与请求 Host 不一致 → 403。CLI、Claude Code 等
  非浏览器客户端不发送这两个头，不受影响。
- L2（``/dashboard*``、``/static/*``、除原生透传外的 ``/api/*``）：Host 主机名必须属于
  loopback ∪ ``server.allowed_hosts`` ∪ 本连接的本机 IP（IP 字面量 Host 无法经 DNS
  rebinding 伪造，覆盖 ``start -h <LAN IP>`` 直连）；带 body 的写请求必须为
  ``application/json``（空 body 放行，兼容 Dashboard / CLI 的 ``POST /api/reset``）。
- 除 ``/dashboard*``、``/static/*`` 外的响应在 ``http.response.start`` 追加
  ``CSP: sandbox`` 与 ``nosniff``，透传的上游 HTML 落入 opaque origin；不缓冲响应体，
  SSE / 流式响应不受影响。中间件位于 ServerErrorMiddleware 外层，未处理异常的 500 同样加固。
"""

from __future__ import annotations

import hmac
import ipaddress
import json
import logging
import re
from collections.abc import Iterable
from urllib.parse import urlsplit

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from ..config.home import is_valid_admin_token

logger = logging.getLogger(__name__)

# 管理面恒允许的 Host；测试经 monkeypatch 扩充（运行时按模块属性读取）
LOOPBACK_HOSTS: frozenset[str] = frozenset({"127.0.0.1", "localhost", "::1"})
NATIVE_PASSTHROUGH_PREFIXES: tuple[str, ...] = (
    "/api/openai",
    "/api/gemini",
    "/api/anthropic",
    "/api/antigravity",
)
ADMIN_TOKEN_HEADER = "X-Coding-Proxy-Admin"
SANDBOX_CSP = "sandbox; default-src 'none'; frame-ancestors 'none'"

_REJECTED_FETCH_SITES = frozenset({"cross-site", "same-site"})
_WRITE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
_DEFAULT_PORTS = {"http": 80, "https": 443, "ws": 80, "wss": 443}
_HARDEN_HEADERS: tuple[tuple[bytes, bytes], ...] = (
    (b"content-security-policy", SANDBOX_CSP.encode()),
    (b"x-content-type-options", b"nosniff"),
)
_NO_STORE = (b"cache-control", b"no-store")
_HOSTNAME = re.compile(r"[a-z0-9_](?:[a-z0-9_.-]*[a-z0-9_])?")


# ── 判定工具 ────────────────────────────────────────────────────


def _under(path: str, prefix: str) -> bool:
    return path == prefix or path.startswith(prefix + "/")


def is_dashboard_path(path: str) -> bool:
    """Dashboard 页面与静态资源（自带 CSP，不加 sandbox）."""
    return path.startswith("/dashboard") or _under(path, "/static")


def is_native_passthrough(path: str) -> bool:
    return any(_under(path, prefix) for prefix in NATIVE_PASSTHROUGH_PREFIXES)


def requires_host_check(path: str) -> bool:
    """L2 管理面：Dashboard、静态资源与非透传的 ``/api/*``."""
    if is_dashboard_path(path):
        return True
    return _under(path, "/api") and not is_native_passthrough(path)


def split_host(value: str) -> tuple[str, int | None] | None:
    """解析 Host / authority → ``(主机名, 端口)``；格式非法返回 None.

    主机名小写、去尾点、去 IPv6 方括号（IPv6 规范化为压缩形式）；未加方括号的
    IPv6、非数字端口一律视为非法。
    """
    value = value.strip().lower()
    port_text = ""
    if value.startswith("["):
        end = value.find("]")
        if end < 0:
            return None
        host, rest = value[1:end], value[end + 1 :]
        if rest:
            if not rest.startswith(":"):
                return None
            port_text = rest[1:]
        try:
            host = ipaddress.IPv6Address(host).compressed
        except ValueError:
            return None
    else:
        host, _, port_text = value.partition(":")
        if ":" in port_text:
            return None
    host = host.rstrip(".")
    if not host:
        return None
    if not port_text:
        return host, None
    if not (port_text.isascii() and port_text.isdigit()) or int(port_text) > 65535:
        return None
    return host, int(port_text)


def normalize_allowed_hosts(items: Iterable[str]) -> frozenset[str]:
    """``server.allowed_hosts`` → 规范化主机名集合；无法识别的条目告警后忽略.

    接受主机名、IPv4、IPv6（带或不带方括号）及可选端口（端口忽略）。
    """
    hosts: set[str] = set()
    for item in items:
        text = str(item).strip()
        try:
            hosts.add(ipaddress.ip_address(text).compressed)
            continue
        except ValueError:
            pass
        parsed = split_host(text)
        if parsed is None or not (":" in parsed[0] or _HOSTNAME.fullmatch(parsed[0])):
            logger.warning("忽略无效的 server.allowed_hosts 条目: %r", text)
            continue
        hosts.add(parsed[0])
    return frozenset(hosts)


def local_ip(scope: Scope) -> str | None:
    """本连接的本机 IP（uvicorn 的 ``scope["server"]`` 为已接受套接字的本地地址）."""
    server = scope.get("server")
    if not server:
        return None
    try:
        ip = ipaddress.ip_address(str(server[0]))
    except ValueError:
        return None
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    return None if ip.is_unspecified else ip.compressed


def is_json_media_type(value: str | None) -> bool:
    return (value or "").split(";", 1)[0].strip().lower() == "application/json"


def admin_token_matches(provided: str | None, configured: str) -> bool:
    """常量时间比较；配置的 token 无效（空 / 过短）时一律不匹配（管理面锁定）."""
    if not provided or not is_valid_admin_token(configured):
        return False
    return hmac.compare_digest(provided.encode(), configured.encode())


def _header(scope: Scope, name: bytes) -> str | None:
    for key, value in scope.get("headers") or ():
        if key == name:
            return value.decode("latin-1")
    return None


def _has_body(scope: Scope) -> bool:
    length = _header(scope, b"content-length")
    if length is not None:
        try:
            return int(length) > 0
        except ValueError:
            return True
    return _header(scope, b"transfer-encoding") is not None


# ── 中间件 ──────────────────────────────────────────────────────


class SecurityMiddleware:
    """纯 ASGI 安全中间件（不读取请求体、不缓冲响应体）."""

    def __init__(self, app: ASGIApp, *, allowed_hosts: Iterable[str] = ()) -> None:
        self.app = app
        self.extra_hosts = normalize_allowed_hosts(allowed_hosts)

    def host_allowed(self, host: str, scope: Scope | None = None) -> bool:
        if host in LOOPBACK_HOSTS or host in self.extra_hosts:
            return True
        return scope is not None and host == local_ip(scope)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return
        path = scope.get("path", "")
        harden = scope["type"] == "http" and not is_dashboard_path(path)
        extra = _harden_headers(path) if harden else ()
        denial = self._check(scope, path)
        if denial is not None:
            if scope["type"] == "websocket":
                await send({"type": "websocket.close", "code": 1008})
            else:
                await _reject(send, *denial, extra=extra)
            return
        await self.app(scope, receive, _hardening_send(send, extra) if harden else send)

    def _check(self, scope: Scope, path: str) -> tuple[int, str, str] | None:
        site = (_header(scope, b"sec-fetch-site") or "").strip().lower()
        host_header = _header(scope, b"host")
        request_host = split_host(host_header) if host_header is not None else None
        origin = _header(scope, b"origin")
        if site in _REJECTED_FETCH_SITES or (
            origin is not None and not self._origin_ok(origin, request_host, scope)
        ):
            return 403, "forbidden", "跨站请求已拒绝"
        if not requires_host_check(path):
            return None
        if request_host is None or not self.host_allowed(request_host[0], scope):
            return (
                403,
                "forbidden",
                "Host 不在允许列表中；如需经该主机名访问，"
                "请执行 coding-proxy config set server.allowed_hosts 后重启",
            )
        if (
            scope["type"] == "http"
            and scope.get("method") in _WRITE_METHODS
            and _has_body(scope)
            and not is_json_media_type(_header(scope, b"content-type"))
        ):
            return 415, "invalid_request_error", "请求体必须为 application/json"
        return None

    def _origin_ok(
        self, origin: str, request_host: tuple[str, int | None] | None, scope: Scope
    ) -> bool:
        """Origin 主机须在白名单内，且 (主机, 端口) 与请求 Host 一致.

        Host 不带端口时（经默认端口访问，含终止 TLS 的反向代理：``proxy_headers=False``
        下 scheme 恒为 http），Origin 只需使用其 scheme 的默认端口（80 / 443）。
        """
        if request_host is None:
            return False
        try:
            parts = urlsplit(origin.strip())
        except ValueError:
            return False
        if parts.scheme not in ("http", "https") or "@" in parts.netloc:
            return False
        parsed = split_host(parts.netloc)
        if parsed is None or not self.host_allowed(parsed[0], scope):
            return False
        if parsed[0] != request_host[0]:
            return False
        origin_port = parsed[1] or _DEFAULT_PORTS[parts.scheme]
        if request_host[1] is None:
            return origin_port == _DEFAULT_PORTS[parts.scheme]
        return origin_port == request_host[1]


def _harden_headers(path: str) -> tuple[tuple[bytes, bytes], ...]:
    """加固头；管理 API ``/api/config*`` 额外 ``no-store``（含拒绝与 500 响应）."""
    if _under(path, "/api/config"):
        return (*_HARDEN_HEADERS, _NO_STORE)
    return _HARDEN_HEADERS


def _hardening_send(send: Send, extra: tuple[tuple[bytes, bytes], ...]) -> Send:
    names = {name for name, _ in extra}

    async def wrapped(message: Message) -> None:
        if message["type"] == "http.response.start":
            headers = [
                (k, v)
                for k, v in message.get("headers") or ()
                if k.lower() not in names
            ]
            message = {**message, "headers": [*headers, *extra]}
        await send(message)

    return wrapped


async def _reject(
    send: Send,
    status: int,
    error_type: str,
    message: str,
    *,
    extra: tuple[tuple[bytes, bytes], ...],
) -> None:
    body = json.dumps(
        {"error": {"type": error_type, "message": message}}, ensure_ascii=False
    ).encode()
    headers = [
        (b"content-type", b"application/json"),
        (b"content-length", str(len(body)).encode()),
        *extra,
    ]
    await send({"type": "http.response.start", "status": status, "headers": headers})
    await send({"type": "http.response.body", "body": body})


# ── 应用装配 ────────────────────────────────────────────────────


async def request_validation_handler(
    request: Request, exc: RequestValidationError
) -> JSONResponse:
    """422 只返回 ``[{loc, msg}]``，不回显 input."""
    errors = [
        {"loc": list(err.get("loc", ())), "msg": str(err.get("msg", ""))}
        for err in exc.errors()
    ]
    return JSONResponse(status_code=422, content={"errors": errors})


def install_security(app: FastAPI, *, allowed_hosts: Iterable[str] = ()) -> None:
    """安装 :class:`SecurityMiddleware` 与脱敏的 422 handler（create_app / degraded app 共用）.

    中间件包在整个 middleware stack 外层（``add_middleware`` 会落在 ServerErrorMiddleware
    内层，未处理异常的 500 将缺少加固头）。
    """
    hosts = normalize_allowed_hosts(allowed_hosts)
    build_stack = app.build_middleware_stack

    def _build() -> ASGIApp:
        return SecurityMiddleware(build_stack(), allowed_hosts=hosts)

    app.build_middleware_stack = _build  # type: ignore[method-assign]
    app.add_exception_handler(RequestValidationError, request_validation_handler)


__all__ = [
    "LOOPBACK_HOSTS",
    "NATIVE_PASSTHROUGH_PREFIXES",
    "ADMIN_TOKEN_HEADER",
    "SANDBOX_CSP",
    "SecurityMiddleware",
    "split_host",
    "normalize_allowed_hosts",
    "local_ip",
    "is_dashboard_path",
    "is_native_passthrough",
    "requires_host_check",
    "is_json_media_type",
    "admin_token_matches",
    "request_validation_handler",
    "install_security",
]
