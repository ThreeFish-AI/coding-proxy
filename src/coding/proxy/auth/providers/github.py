"""GitHub Device Authorization Flow — 浏览器免回调的 OAuth 登录."""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

import webbrowser

import httpx

from ..store import ProviderTokens
from .base import OAuthProvider

logger = logging.getLogger(__name__)

# GitHub Copilot VS Code 扩展的公开 client_id
_COPILOT_CLIENT_ID = "Iv1.b507a08c87ecfe98"

_DEVICE_CODE_URL = "https://github.com/login/device/code"
_ACCESS_TOKEN_URL = "https://github.com/login/oauth/access_token"
_POLL_INTERVAL = 5  # seconds
_MAX_POLL_ATTEMPTS = 60  # 5 minutes total
_COPILOT_PERMISSIVE_SCOPES = "read:user user:email repo workflow"


class GitHubDeviceFlowProvider(OAuthProvider):
    """GitHub Device Authorization Flow 实现.

    无需本地 HTTP 服务器，用户在浏览器中输入 user_code 即可完成授权。
    获取的 GitHub OAuth token 可用于 Copilot token 交换。
    """

    def __init__(self, client_id: str = _COPILOT_CLIENT_ID) -> None:
        self._client_id = client_id
        self._http = httpx.AsyncClient(timeout=httpx.Timeout(30.0))

    def get_name(self) -> str:
        return "github"

    async def login(self) -> ProviderTokens:
        """执行 GitHub Device Flow，返回 OAuth token."""
        # Step 1: 请求 device code
        resp = await self._http.post(
            _DEVICE_CODE_URL,
            data={"client_id": self._client_id, "scope": _COPILOT_PERMISSIVE_SCOPES},
            headers={"accept": "application/json"},
        )
        resp.raise_for_status()
        device_data: dict[str, Any] = resp.json()

        user_code = device_data["user_code"]
        verification_uri = device_data["verification_uri"]
        device_code = device_data["device_code"]
        interval = device_data.get("interval", _POLL_INTERVAL)

        # 优先使用预填充 user_code 的完整链接
        verification_url = device_data.get(
            "verification_uri_complete", verification_uri
        )

        # Step 2: 引导用户在浏览器中授权
        logger.info("请在浏览器中访问 %s 并输入代码: %s", verification_uri, user_code)
        print(f"\n  🔗 请在浏览器中访问: {verification_uri}")
        print(f"  📋 并输入代码: {user_code}\n")

        webbrowser.open(verification_url)

        # Step 3: 轮询等待用户完成授权
        for attempt in range(_MAX_POLL_ATTEMPTS):
            await asyncio.sleep(interval)

            token_resp = await self._http.post(
                _ACCESS_TOKEN_URL,
                data={
                    "client_id": self._client_id,
                    "device_code": device_code,
                    "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
                },
                headers={"accept": "application/json"},
            )
            token_data = token_resp.json()

            if "access_token" in token_data:
                logger.info("GitHub OAuth 授权成功")
                return ProviderTokens(
                    access_token=token_data["access_token"],
                    token_type=token_data.get("token_type", "bearer"),
                    scope=token_data.get("scope", ""),
                )

            error = token_data.get("error", "")
            if error == "authorization_pending":
                continue
            elif error == "slow_down":
                interval += 5
                continue
            elif error == "expired_token":
                raise RuntimeError("Device code 已过期，请重新登录")
            elif error == "access_denied":
                raise RuntimeError("用户拒绝了授权")
            else:
                raise RuntimeError(f"GitHub OAuth 错误: {error}")

        raise RuntimeError("GitHub Device Flow 超时，请重试")

    async def refresh(self, tokens: ProviderTokens) -> ProviderTokens:
        """GitHub Device Flow 不支持 refresh_token，需要重新登录."""
        return await self.login()

    async def validate(self, tokens: ProviderTokens) -> bool:
        """验证 GitHub token 是否有效."""
        if not tokens.access_token:
            return False
        try:
            resp = await self._http.get(
                "https://api.github.com/user",
                headers={
                    "authorization": f"token {tokens.access_token}",
                    "accept": "application/json",
                },
            )
            return resp.status_code == 200
        except httpx.HTTPError:
            return False

    async def close(self) -> None:
        if not self._http.is_closed:
            await self._http.aclose()
