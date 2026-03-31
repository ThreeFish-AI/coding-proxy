"""GitHub Copilot 后端 — 内置 token 交换与 Anthropic 兼容转发."""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any
from uuid import uuid4

import httpx

from ..config.schema import CopilotConfig, FailoverConfig
from .base import PROXY_SKIP_HEADERS, BaseBackend
from .token_manager import BaseTokenManager, TokenAcquireError, TokenErrorKind

logger = logging.getLogger(__name__)

_COPILOT_VERSION = "0.26.7"
_EDITOR_VERSION = "vscode/1.98.0"
_EDITOR_PLUGIN_VERSION = f"copilot-chat/{_COPILOT_VERSION}"
_USER_AGENT = f"GitHubCopilotChat/{_COPILOT_VERSION}"
_GITHUB_API_VERSION = "2025-04-01"


def resolve_copilot_base_url(account_type: str, configured_base_url: str) -> str:
    """解析 Copilot API 基础地址.

    保留用户显式覆盖；仅当值为空时按账号类型回退到官方推荐域名。
    """
    if configured_base_url:
        return configured_base_url
    normalized = (account_type or "individual").strip().lower()
    if normalized == "individual":
        return "https://api.githubcopilot.com"
    return f"https://api.{normalized}.githubcopilot.com"


@dataclass
class CopilotExchangeDiagnostics:
    """最近一次 Copilot token 交换的运行时诊断."""

    raw_shape: str = ""
    token_field: str = ""
    expires_in_seconds: int = 0
    expires_at_unix: int = 0
    capabilities: dict[str, Any] = field(default_factory=dict)
    updated_at_unix: int = 0

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {}
        if self.raw_shape:
            data["raw_shape"] = self.raw_shape
        if self.token_field:
            data["token_field"] = self.token_field
        if self.expires_in_seconds:
            data["expires_in_seconds"] = self.expires_in_seconds
        if self.expires_at_unix:
            data["expires_at_unix"] = self.expires_at_unix
            data["ttl_seconds"] = max(self.expires_at_unix - int(time.time()), 0)
        if self.capabilities:
            data["capabilities"] = self.capabilities
        if self.updated_at_unix:
            data["updated_at_unix"] = self.updated_at_unix
        return data


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


class CopilotBackend(BaseBackend):
    """GitHub Copilot API 后端.

    通过内置 token 交换访问 GitHub Copilot 的 Anthropic 兼容端点.
    透传请求体（无模型映射），Claude 模型名原生支持.
    """

    def __init__(self, config: CopilotConfig, failover_config: FailoverConfig) -> None:
        self._account_type = (config.account_type or "individual").strip().lower()
        self._configured_base_url = config.base_url
        self._resolved_base_url = resolve_copilot_base_url(self._account_type, config.base_url)
        super().__init__(self._resolved_base_url, config.timeout_ms, failover_config)
        self._token_manager = CopilotTokenManager(config.github_token, config.token_url)

    def get_name(self) -> str:
        return "copilot"

    def _build_copilot_headers(self) -> dict[str, str]:
        return {
            "copilot-integration-id": "vscode-chat",
            "editor-version": _EDITOR_VERSION,
            "editor-plugin-version": _EDITOR_PLUGIN_VERSION,
            "user-agent": _USER_AGENT,
            "openai-intent": "conversation-panel",
            "x-github-api-version": _GITHUB_API_VERSION,
            "x-request-id": str(uuid4()),
            "x-vscode-user-agent-library-version": "electron-fetch",
            "content-type": "application/json",
        }

    async def _prepare_request(
        self,
        request_body: dict[str, Any],
        headers: dict[str, str],
    ) -> tuple[dict[str, Any], dict[str, str]]:
        """透传请求体，过滤 hop-by-hop 头并注入 Copilot token."""
        filtered = {k: v for k, v in headers.items() if k.lower() not in PROXY_SKIP_HEADERS}
        prepared = self._build_copilot_headers()
        for key, value in filtered.items():
            if key.lower() not in {item.lower() for item in prepared}:
                prepared[key] = value
        token = await self._token_manager.get_token()
        prepared["authorization"] = f"Bearer {token}"
        return request_body, prepared

    def _on_error_status(self, status_code: int) -> None:
        """401/403 时标记 token 失效以触发被动刷新."""
        if status_code in (401, 403):
            self._token_manager.invalidate()

    async def check_health(self) -> bool:
        """检查 Copilot token 交换是否有效（免费操作）."""
        try:
            token = await self._token_manager.get_token()
            return bool(token)
        except Exception:
            return False

    def get_diagnostics(self) -> dict[str, Any]:
        diagnostics: dict[str, Any] = {
            "account_type": self._account_type,
            "base_url": self._resolved_base_url,
        }
        token_manager = self._token_manager.get_diagnostics()
        if token_manager:
            diagnostics["token_manager"] = token_manager
        exchange = self._token_manager.get_exchange_diagnostics()
        if exchange:
            diagnostics["exchange"] = exchange
        return diagnostics

    async def probe_models(self) -> dict[str, Any]:
        """探测当前 Copilot 会话可见模型列表."""
        token = await self._token_manager.get_token()
        response = await self._get_client().get(
            "/models",
            headers={
                **self._build_copilot_headers(),
                "authorization": f"Bearer {token}",
            },
        )
        probe: dict[str, Any] = {
            "probe_status": "ok" if response.status_code < 400 else "error",
            "status_code": response.status_code,
            "account_type": self._account_type,
            "base_url": self._resolved_base_url,
        }
        data = response.json() if response.content else {}
        if response.status_code >= 400:
            error = data.get("error", {}) if isinstance(data, dict) else {}
            probe["failure_reason"] = (
                error.get("message")
                or CopilotTokenManager._format_body_excerpt(data)
            )
            return probe

        models = data.get("data", []) if isinstance(data, dict) else []
        available_models = [
            item.get("id")
            for item in models
            if isinstance(item, dict) and item.get("id")
        ]
        probe["available_models"] = available_models
        probe["has_claude_opus_4_6"] = any("opus" in model and "4.6" in model for model in available_models)
        return probe

    async def close(self) -> None:
        await self._token_manager.close()
        await super().close()
