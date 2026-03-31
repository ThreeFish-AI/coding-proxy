"""GitHub Copilot 后端 — 内置 token 交换与 Anthropic 兼容转发."""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

import httpx

from ..config.schema import CopilotConfig, FailoverConfig
from .base import PROXY_SKIP_HEADERS, BaseBackend

logger = logging.getLogger(__name__)


class CopilotTokenManager:
    """管理 GitHub Copilot token 的交换与自动刷新.

    流程: GitHub PAT → POST token_url → Copilot access_token (~30 分钟有效期)
    """

    # 提前刷新的余量（秒）
    _REFRESH_MARGIN = 60

    def __init__(self, github_token: str, token_url: str) -> None:
        self._github_token = github_token
        self._token_url = token_url
        self._access_token: str | None = None
        self._expires_at: float = 0.0
        self._lock = asyncio.Lock()
        self._client: httpx.AsyncClient | None = None

    def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(timeout=httpx.Timeout(30.0))
        return self._client

    async def get_token(self) -> str:
        """获取有效的 Copilot access_token（带缓存和自动刷新）."""
        if self._access_token and time.monotonic() < self._expires_at:
            return self._access_token

        async with self._lock:
            # Double-check after acquiring lock
            if self._access_token and time.monotonic() < self._expires_at:
                return self._access_token
            await self._exchange()
            assert self._access_token is not None
            return self._access_token

    async def _exchange(self) -> None:
        """通过 GitHub PAT 交换 Copilot token."""
        client = self._get_client()
        response = await client.post(
            self._token_url,
            headers={
                "authorization": f"token {self._github_token}",
                "accept": "application/json",
                "content-type": "application/json",
            },
        )
        response.raise_for_status()
        data = response.json()
        self._access_token = data["access_token"]
        expires_in = data.get("expires_in", 1800)
        self._expires_at = time.monotonic() + expires_in - self._REFRESH_MARGIN
        logger.info("Copilot token exchanged, expires_in=%ds", expires_in)

    def invalidate(self) -> None:
        """标记当前 token 失效（触发下次请求时被动刷新）."""
        self._expires_at = 0.0

    async def close(self) -> None:
        if self._client and not self._client.is_closed:
            await self._client.aclose()


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

    async def close(self) -> None:
        await self._token_manager.close()
        await super().close()
