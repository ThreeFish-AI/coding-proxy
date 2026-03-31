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

    def _prepare_request(
        self,
        request_body: dict[str, Any],
        headers: dict[str, str],
    ) -> tuple[dict[str, Any], dict[str, str]]:
        """透传请求体，注入 Copilot token 认证头.

        注: token 注入在 _prepare_request_async 中完成.
        此方法为同步占位，实际使用 send_message / send_message_stream 的异步覆写.
        """
        filtered = {k: v for k, v in headers.items() if k.lower() not in PROXY_SKIP_HEADERS}
        return request_body, filtered

    async def _prepare_request_async(
        self,
        request_body: dict[str, Any],
        headers: dict[str, str],
    ) -> tuple[dict[str, Any], dict[str, str]]:
        """异步准备请求：获取 Copilot token 并注入认证头."""
        body, filtered = self._prepare_request(request_body, headers)
        token = await self._token_manager.get_token()
        filtered["authorization"] = f"Bearer {token}"
        return body, filtered

    async def send_message(
        self,
        request_body: dict[str, Any],
        headers: dict[str, str],
    ) -> Any:
        """发送非流式消息请求（覆写以使用异步 token 获取）."""
        body, prepared_headers = await self._prepare_request_async(request_body, headers)
        client = self._get_client()

        response = await client.post(
            "/v1/messages",
            json=body,
            headers=prepared_headers,
        )

        # 401/403 时标记 token 失效以触发被动刷新
        if response.status_code in (401, 403):
            self._token_manager.invalidate()

        raw_content = response.content
        resp_body = response.json() if response.content else None

        if response.status_code >= 400:
            from .base import BackendResponse

            return BackendResponse(
                status_code=response.status_code,
                raw_body=raw_content,
                error_type=resp_body.get("error", {}).get("type") if resp_body else None,
                error_message=resp_body.get("error", {}).get("message") if resp_body else None,
            )

        usage = resp_body.get("usage", {}) if resp_body else {}
        from .base import BackendResponse, UsageInfo

        return BackendResponse(
            status_code=response.status_code,
            raw_body=raw_content,
            usage=UsageInfo(
                input_tokens=usage.get("input_tokens", 0),
                output_tokens=usage.get("output_tokens", 0),
                cache_creation_tokens=usage.get("cache_creation_input_tokens", 0),
                cache_read_tokens=usage.get("cache_read_input_tokens", 0),
                request_id=resp_body.get("id", "") if resp_body else "",
            ),
        )

    async def send_message_stream(
        self,
        request_body: dict[str, Any],
        headers: dict[str, str],
    ):
        """发送流式消息请求（覆写以使用异步 token 获取）."""
        body, prepared_headers = await self._prepare_request_async(request_body, headers)
        client = self._get_client()

        async with client.stream(
            "POST",
            "/v1/messages",
            json=body,
            headers=prepared_headers,
        ) as response:
            if response.status_code in (401, 403):
                self._token_manager.invalidate()
            if response.status_code >= 400:
                error_body = await response.aread()
                logger.warning(
                    "%s stream error: status=%d body=%s",
                    self.get_name(), response.status_code, error_body[:500],
                )
                raise httpx.HTTPStatusError(
                    f"{self.get_name()} API error: {response.status_code}",
                    request=response.request,
                    response=httpx.Response(
                        response.status_code,
                        content=error_body,
                        headers=response.headers,
                        request=response.request,
                    ),
                )
            async for chunk in response.aiter_bytes():
                yield chunk

    async def close(self) -> None:
        await self._token_manager.close()
        await super().close()
