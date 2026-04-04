"""GitHub Copilot token 交换管理器.

流程: GitHub token → GET copilot_internal/v2/token → Copilot access_token (~30 分钟有效期)
"""

from __future__ import annotations

import logging
import time
from typing import Any

import httpx

from .copilot_models import CopilotExchangeDiagnostics
from .copilot_urls import (
    _EDITOR_PLUGIN_VERSION,
    _EDITOR_VERSION,
    _GITHUB_API_VERSION,
    _USER_AGENT,
)
from .token_manager import BaseTokenManager, TokenAcquireError, TokenErrorKind

logger = logging.getLogger(__name__)

__all__ = ["CopilotTokenManager"]


class CopilotTokenManager(BaseTokenManager):
    """GitHub Copilot token 交换管理.

    流程: GitHub token → GET copilot_internal/v2/token → Copilot access_token (~30 分钟有效期)
    """

    def __init__(self, github_token: str, token_url: str) -> None:
        super().__init__()
        self._github_token = github_token
        self._token_url = token_url
        self._last_exchange = CopilotExchangeDiagnostics()

    @staticmethod
    def _format_body_excerpt(data: Any) -> str:
        if isinstance(data, dict):
            for key in ("error_description", "error", "message"):
                value = data.get(key)
                if value:
                    return str(value)[:200]
        return str(data)[:200]

    @classmethod
    def _build_missing_token_error(
        cls, data: Any, status_code: int,
    ) -> TokenAcquireError:
        detail = cls._format_body_excerpt(data)
        lowered = detail.lower()
        capability_keys = {
            "chat_enabled", "agent_mode_auto_approval", "chat_jetbrains_enabled",
            "annotations_enabled", "code_quote_enabled",
        }
        if isinstance(data, dict) and capability_keys.intersection(data.keys()):
            return TokenAcquireError.with_kind(
                "Copilot 当前登录权限不足，需升级到可交换 chat token 的 GitHub 会话",
                kind=TokenErrorKind.PERMISSION_UPGRADE_REQUIRED,
                needs_reauth=True,
            )
        needs_reauth = status_code == 401 or any(
            pattern in lowered for pattern in ("bad credentials", "invalid token", "unauthorized")
        )
        kind = TokenErrorKind.INVALID_CREDENTIALS if needs_reauth else TokenErrorKind.TEMPORARY
        return TokenAcquireError.with_kind(
            f"Copilot token 交换返回非预期响应: status={status_code}, detail={detail}",
            kind=kind,
            needs_reauth=needs_reauth,
        )

    @staticmethod
    def _extract_capabilities(data: Any) -> dict[str, Any]:
        if not isinstance(data, dict):
            return {}
        capability_keys = (
            "chat_enabled",
            "chat_jetbrains_enabled",
            "agent_mode_auto_approval",
            "code_quote_enabled",
            "annotations_enabled",
        )
        return {key: data[key] for key in capability_keys if key in data}

    def _record_exchange(self, data: dict[str, Any], token_field: str, expires_in: int) -> None:
        expires_at = int(time.time()) + max(expires_in, 0)
        self._last_exchange = CopilotExchangeDiagnostics(
            raw_shape="token_refresh_in" if "token" in data else "access_token_expires_in",
            token_field=token_field,
            expires_in_seconds=expires_in,
            expires_at_unix=expires_at,
            capabilities=self._extract_capabilities(data),
            updated_at_unix=int(time.time()),
        )

    def get_exchange_diagnostics(self) -> dict[str, Any]:
        return self._last_exchange.to_dict()

    async def _acquire(self) -> tuple[str, float]:
        """通过 GitHub token 交换 Copilot token."""
        client = self._get_client()
        try:
            response = await client.get(
                self._token_url,
                headers={
                    "authorization": f"token {self._github_token}",
                    "accept": "application/json",
                    "editor-version": _EDITOR_VERSION,
                    "editor-plugin-version": _EDITOR_PLUGIN_VERSION,
                    "user-agent": _USER_AGENT,
                    "x-github-api-version": _GITHUB_API_VERSION,
                    "x-vscode-user-agent-library-version": "electron-fetch",
                },
            )
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 401:
                raise TokenAcquireError.with_kind(
                    "GitHub token 无效或已过期",
                    kind=TokenErrorKind.INVALID_CREDENTIALS,
                    needs_reauth=True,
                ) from exc
            raise TokenAcquireError.with_kind(
                f"Copilot token 交换失败: {exc}",
                kind=TokenErrorKind.TEMPORARY,
            ) from exc
        except (httpx.TimeoutException, httpx.ConnectError) as exc:
            raise TokenAcquireError.with_kind(
                f"Copilot token 交换网络异常: {exc}",
                kind=TokenErrorKind.TEMPORARY,
            ) from exc

        try:
            data = response.json()
        except ValueError as exc:
            raise TokenAcquireError(
                f"Copilot token 交换返回非 JSON 响应: status={response.status_code}",
            ) from exc

        if response.status_code >= 400:
            if response.status_code == 401:
                raise TokenAcquireError.with_kind(
                    "GitHub token 无效或已过期",
                    kind=TokenErrorKind.INVALID_CREDENTIALS,
                    needs_reauth=True,
                )
            raise self._build_missing_token_error(data, response.status_code)

        token_field = "token" if data.get("token") else "access_token"
        access_token = data.get("token") or data.get("access_token")
        if not access_token:
            raise self._build_missing_token_error(data, response.status_code)

        expires_in = data.get("refresh_in") or data.get("expires_in")
        if expires_in is None and data.get("expires_at"):
            expires_in = max(int(data["expires_at"]) - int(time.time()), 0)
        expires_in = int(expires_in or 1800)
        self._record_exchange(data, token_field, expires_in)
        logger.info("Copilot token exchanged, expires_in=%ds", expires_in)
        return str(access_token), float(expires_in)

    def update_github_token(self, new_token: str) -> None:
        """运行时热更新 GitHub token（重认证后调用）."""
        self._github_token = new_token
        self.invalidate()
