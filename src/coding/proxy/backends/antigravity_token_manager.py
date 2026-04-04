"""Google OAuth2 token 自动刷新管理器.

流程: refresh_token → POST oauth2.googleapis.com/token → access_token (~1 小时有效期)
"""

from __future__ import annotations

import logging

import httpx

from ..auth.providers.google import GoogleOAuthProvider
from .token_manager import BaseTokenManager, TokenAcquireError, TokenErrorKind

logger = logging.getLogger(__name__)

__all__ = ["GoogleOAuthTokenManager"]


class GoogleOAuthTokenManager(BaseTokenManager):
    """Google OAuth2 token 自动刷新管理.

    流程: refresh_token → POST oauth2.googleapis.com/token → access_token (~1 小时有效期)

    .. note::
        与 ``auth.providers.google.GoogleOAuthProvider.refresh()`` 的关系：
        - ``GoogleOAuthProvider`` = 完整 OAuth 流程（login + refresh + validate），用于 CLI 登录场景
        - ``GoogleOAuthTokenManager`` = 轻量级 token 刷新器，用于后端运行时自动续期
    """

    _REFRESH_MARGIN = 120  # 提前刷新余量（秒）
    _TOKEN_URL = "https://oauth2.googleapis.com/token"

    def __init__(self, client_id: str, client_secret: str, refresh_token: str) -> None:
        super().__init__()
        self._client_id = client_id
        self._client_secret = client_secret
        self._refresh_token = refresh_token

    async def _acquire(self) -> tuple[str, float]:
        """通过 refresh_token 获取新的 access_token."""
        client = self._get_client()
        try:
            response = await client.post(
                self._TOKEN_URL,
                data={
                    "client_id": self._client_id,
                    "client_secret": self._client_secret,
                    "refresh_token": self._refresh_token,
                    "grant_type": "refresh_token",
                },
            )
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            # 400 + invalid_grant 表示 refresh_token 已失效
            if exc.response.status_code == 400:
                try:
                    err = exc.response.json()
                    if err.get("error") == "invalid_grant":
                        raise TokenAcquireError.with_kind(
                            "Google refresh_token 已失效",
                            kind=TokenErrorKind.INVALID_CREDENTIALS,
                            needs_reauth=True,
                        ) from exc
                except (ValueError, KeyError):
                    pass
            raise TokenAcquireError.with_kind(
                f"Google token 刷新失败: {exc}",
                kind=TokenErrorKind.TEMPORARY,
            ) from exc
        except (httpx.TimeoutException, httpx.ConnectError) as exc:
            raise TokenAcquireError.with_kind(
                f"Google token 刷新网络异常: {exc}",
                kind=TokenErrorKind.TEMPORARY,
            ) from exc
        data = response.json()
        scope = data.get("scope", "")
        if scope and not GoogleOAuthProvider.has_required_scopes(scope):
            raise TokenAcquireError.with_kind(
                "Google access_token 缺少 Antigravity 所需 scope",
                kind=TokenErrorKind.INSUFFICIENT_SCOPE,
                needs_reauth=True,
            )
        expires_in = data.get("expires_in", 3600)
        logger.info("Google OAuth token refreshed, expires_in=%ds", expires_in)
        return data["access_token"], float(expires_in)

    def update_refresh_token(self, new_refresh_token: str) -> None:
        """运行时热更新 refresh_token（重认证后调用）."""
        self._refresh_token = new_refresh_token
        self.invalidate()
