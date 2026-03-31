"""GitHub Copilot 后端 — 内置 token 交换与 Anthropic 兼容转发."""

from __future__ import annotations

import logging
from typing import Any

import httpx

from ..config.schema import CopilotConfig, FailoverConfig
from .base import PROXY_SKIP_HEADERS, BaseBackend
from .token_manager import BaseTokenManager, TokenAcquireError

logger = logging.getLogger(__name__)


class CopilotTokenManager(BaseTokenManager):
    """GitHub Copilot token 交换管理.

    流程: GitHub token → GET copilot_internal/v2/token → Copilot access_token (~30 分钟有效期)
    """

    def __init__(self, github_token: str, token_url: str) -> None:
        super().__init__()
        self._github_token = github_token
        self._token_url = token_url

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
        needs_reauth = status_code == 401 or any(
            pattern in lowered for pattern in ("bad credentials", "invalid token", "unauthorized")
        )
        return TokenAcquireError(
            f"Copilot token 交换返回非预期响应: status={status_code}, detail={detail}",
            needs_reauth=needs_reauth,
        )

    async def _acquire(self) -> tuple[str, float]:
        """通过 GitHub token 交换 Copilot token."""
        client = self._get_client()
        try:
            response = await client.get(
                self._token_url,
                headers={
                    "authorization": f"token {self._github_token}",
                    "accept": "application/json",
                    "editor-version": "vscode/1.95.0",
                    "editor-plugin-version": "copilot/1.0.0",
                },
            )
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 401:
                raise TokenAcquireError(
                    "GitHub token 无效或已过期", needs_reauth=True,
                ) from exc
            raise TokenAcquireError(f"Copilot token 交换失败: {exc}") from exc
        except (httpx.TimeoutException, httpx.ConnectError) as exc:
            raise TokenAcquireError(f"Copilot token 交换网络异常: {exc}") from exc

        try:
            data = response.json()
        except ValueError as exc:
            raise TokenAcquireError(
                f"Copilot token 交换返回非 JSON 响应: status={response.status_code}",
            ) from exc

        if response.status_code >= 400:
            if response.status_code == 401:
                raise TokenAcquireError("GitHub token 无效或已过期", needs_reauth=True)
            raise self._build_missing_token_error(data, response.status_code)

        access_token = data.get("access_token")
        if not access_token:
            raise self._build_missing_token_error(data, response.status_code)

        expires_in = data.get("expires_in", 1800)
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
        super().__init__(config.base_url, config.timeout_ms, failover_config)
        self._token_manager = CopilotTokenManager(config.github_token, config.token_url)

    def get_name(self) -> str:
        return "copilot"

    async def _prepare_request(
        self,
        request_body: dict[str, Any],
        headers: dict[str, str],
    ) -> tuple[dict[str, Any], dict[str, str]]:
        """透传请求体，过滤 hop-by-hop 头并注入 Copilot token."""
        filtered = {k: v for k, v in headers.items() if k.lower() not in PROXY_SKIP_HEADERS}
        token = await self._token_manager.get_token()
        filtered["authorization"] = f"Bearer {token}"
        return request_body, filtered

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
        diagnostics = self._token_manager.get_diagnostics()
        if not diagnostics:
            return {}
        return {"token_manager": diagnostics}

    async def close(self) -> None:
        await self._token_manager.close()
        await super().close()
