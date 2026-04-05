"""Google OAuth2 Authorization Code Flow — 本地回调服务器."""

from __future__ import annotations

import asyncio
import logging
import secrets
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any
from urllib.parse import parse_qs, urlencode, urlparse

import httpx

from ..store import ProviderTokens
from .base import OAuthProvider

logger = logging.getLogger(__name__)

# Antigravity Enterprise 公开 OAuth 凭据
# SOT（权威源）: coding.proxy.config.schema.AuthConfig
# 此处默认值仅作 fallback，生产环境应通过 config.yaml 的 auth 段覆盖
_DEFAULT_CLIENT_ID = (
    "1071006060591-tmhssin2h21lcre235vtolojh4g403ep.apps.googleusercontent.com"
)
_DEFAULT_CLIENT_SECRET = "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"

_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
_TOKEN_URL = "https://oauth2.googleapis.com/token"
_SCOPES = [
    "https://www.googleapis.com/auth/cloud-platform",
    "https://www.googleapis.com/auth/userinfo.email",
    "https://www.googleapis.com/auth/userinfo.profile",
    "https://www.googleapis.com/auth/cclog",
    "https://www.googleapis.com/auth/experimentsandconfigs",
]
_REQUIRED_SCOPE_SET = frozenset(_SCOPES)


class _CallbackHandler(BaseHTTPRequestHandler):
    """OAuth 回调 HTTP 处理器.

    使用实例级 result dict 避免类属性在并发场景下的交叉污染.
    """

    def __init__(
        self, *args: Any, result: dict[str, str | None], **kwargs: Any
    ) -> None:
        self._result = result
        super().__init__(*args, **kwargs)

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        params = parse_qs(parsed.query)

        if parsed.path == "/callback":
            if "error" in params:
                self._result["error"] = params["error"][0]
                self._respond("授权失败，请关闭此页面返回终端。")
            elif "code" in params and "state" in params:
                self._result["auth_code"] = params["code"][0]
                self._result["state"] = params["state"][0]
                self._respond("授权成功！请关闭此页面返回终端。")
            else:
                self._respond("无效的回调参数。")
        else:
            self.send_response(404)
            self.end_headers()

    def _respond(self, message: str) -> None:
        self.send_response(200)
        self.send_header("content-type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(f"<html><body><h2>{message}</h2></body></html>".encode())

    def log_message(self, format: str, *args: Any) -> None:
        pass  # 静默 HTTP 日志


class GoogleOAuthProvider(OAuthProvider):
    """Google OAuth2 Authorization Code Flow 实现.

    启动本地 HTTP 回调服务器捕获 authorization code，
    交换为 access_token + refresh_token。
    """

    def __init__(
        self,
        client_id: str = _DEFAULT_CLIENT_ID,
        client_secret: str = _DEFAULT_CLIENT_SECRET,
    ) -> None:
        self._client_id = client_id
        self._client_secret = client_secret
        self._http = httpx.AsyncClient(timeout=httpx.Timeout(30.0))

    def get_name(self) -> str:
        return "google"

    @staticmethod
    def has_required_scopes(scope: str) -> bool:
        granted = {item for item in scope.split() if item}
        return _REQUIRED_SCOPE_SET.issubset(granted)

    async def login(self) -> ProviderTokens:
        """执行 Google OAuth2 Code Flow，返回 Token."""
        state = secrets.token_urlsafe(32)
        result: dict[str, str | None] = {
            "auth_code": None,
            "state": None,
            "error": None,
        }

        def _make_handler(*args: Any, **kwargs: Any) -> _CallbackHandler:
            return _CallbackHandler(*args, result=result, **kwargs)

        # 绑定到 port 0，由 OS 分配可用端口，避免 TOCTOU 竞态
        server = HTTPServer(("127.0.0.1", 0), _make_handler)
        redirect_port = server.server_address[1]
        redirect_uri = f"http://127.0.0.1:{redirect_port}/callback"

        params = urlencode(
            {
                "client_id": self._client_id,
                "redirect_uri": redirect_uri,
                "response_type": "code",
                "scope": " ".join(_SCOPES),
                "state": state,
                "access_type": "offline",
                "prompt": "consent",
            }
        )
        auth_url = f"{_AUTH_URL}?{params}"

        logger.info("请在浏览器中完成 Google 授权")
        print("\n  🔗 请在浏览器中访问以下链接完成授权:\n")
        print(f"  {auth_url}\n")

        # 打开浏览器
        import webbrowser

        webbrowser.open(auth_url)

        # 等待回调
        for _ in range(120):  # 最多等 2 分钟
            server.handle_request()
            if result["auth_code"] or result["error"]:
                break
            await asyncio.sleep(1)

        server.server_close()

        if result["error"]:
            raise RuntimeError(f"Google OAuth 错误: {result['error']}")

        if not result["auth_code"]:
            raise RuntimeError("Google OAuth 超时，请重试")

        if result["state"] != state:
            raise RuntimeError("OAuth state 不匹配，可能遭受 CSRF 攻击")

        # 交换 code → token
        return await self._exchange_code(result["auth_code"], redirect_uri)

    async def _exchange_code(self, code: str, redirect_uri: str) -> ProviderTokens:
        """将 authorization code 交换为 access_token + refresh_token."""
        resp = await self._http.post(
            _TOKEN_URL,
            data={
                "client_id": self._client_id,
                "client_secret": self._client_secret,
                "code": code,
                "redirect_uri": redirect_uri,
                "grant_type": "authorization_code",
            },
            headers={"content-type": "application/x-www-form-urlencoded"},
        )
        resp.raise_for_status()
        data = resp.json()

        expires_at = 0.0
        if "expires_in" in data:
            expires_at = time.time() + data["expires_in"]

        return ProviderTokens(
            access_token=data.get("access_token", ""),
            refresh_token=data.get("refresh_token", ""),
            expires_at=expires_at,
            scope=data.get("scope", ""),
            token_type=data.get("token_type", "bearer"),
        )

    async def refresh(self, tokens: ProviderTokens) -> ProviderTokens:
        """使用 refresh_token 刷新 access_token."""
        if not tokens.refresh_token:
            return await self.login()

        resp = await self._http.post(
            _TOKEN_URL,
            data={
                "client_id": self._client_id,
                "client_secret": self._client_secret,
                "refresh_token": tokens.refresh_token,
                "grant_type": "refresh_token",
            },
            headers={"content-type": "application/x-www-form-urlencoded"},
        )

        if resp.status_code >= 400:
            logger.warning("Google token refresh 失败，需要重新登录")
            return await self.login()

        data = resp.json()
        expires_at = 0.0
        if "expires_in" in data:
            expires_at = time.time() + data["expires_in"]

        return ProviderTokens(
            access_token=data.get("access_token", ""),
            refresh_token=tokens.refresh_token,  # refresh_token 通常不变
            expires_at=expires_at,
            scope=data.get("scope", tokens.scope),
            token_type=data.get("token_type", "bearer"),
        )

    async def validate(self, tokens: ProviderTokens) -> bool:
        """验证 Google token 是否有效."""
        if not tokens.access_token:
            return False
        try:
            resp = await self._http.get(
                "https://www.googleapis.com/oauth2/v1/tokeninfo",
                params={"access_token": tokens.access_token},
            )
            if resp.status_code != 200:
                return False
            data = resp.json()
            return self.has_required_scopes(data.get("scope", tokens.scope))
        except httpx.HTTPError:
            return False

    async def close(self) -> None:
        if not self._http.is_closed:
            await self._http.aclose()
