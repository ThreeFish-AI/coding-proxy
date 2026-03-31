"""Google Antigravity Claude 后端 — OAuth2 认证 + Gemini 格式转换."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any, AsyncIterator

import httpx

from ..config.schema import AntigravityConfig, FailoverConfig
from ..convert.anthropic_to_gemini import convert_request
from ..convert.gemini_to_anthropic import convert_response, extract_usage
from ..convert.gemini_sse_adapter import adapt_sse_stream
from .base import BackendResponse, BaseBackend, UsageInfo

logger = logging.getLogger(__name__)


class GoogleOAuthTokenManager:
    """管理 Google OAuth2 token 的自动刷新.

    流程: refresh_token → POST oauth2.googleapis.com/token → access_token (~1 小时有效期)
    """

    _REFRESH_MARGIN = 120  # 提前刷新余量（秒）
    _TOKEN_URL = "https://oauth2.googleapis.com/token"

    def __init__(self, client_id: str, client_secret: str, refresh_token: str) -> None:
        self._client_id = client_id
        self._client_secret = client_secret
        self._refresh_token = refresh_token
        self._access_token: str | None = None
        self._expires_at: float = 0.0
        self._lock = asyncio.Lock()
        self._client: httpx.AsyncClient | None = None

    def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(timeout=httpx.Timeout(30.0))
        return self._client

    async def get_token(self) -> str:
        """获取有效的 Google access_token（带缓存和自动刷新）."""
        if self._access_token and time.monotonic() < self._expires_at:
            return self._access_token

        async with self._lock:
            if self._access_token and time.monotonic() < self._expires_at:
                return self._access_token
            await self._refresh()
            assert self._access_token is not None
            return self._access_token

    async def _refresh(self) -> None:
        """通过 refresh_token 获取新的 access_token."""
        client = self._get_client()
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
        data = response.json()
        self._access_token = data["access_token"]
        expires_in = data.get("expires_in", 3600)
        self._expires_at = time.monotonic() + expires_in - self._REFRESH_MARGIN
        logger.info("Google OAuth token refreshed, expires_in=%ds", expires_in)

    def invalidate(self) -> None:
        """标记当前 token 失效（触发下次请求时被动刷新）."""
        self._expires_at = 0.0

    async def close(self) -> None:
        if self._client and not self._client.is_closed:
            await self._client.aclose()


class AntigravityBackend(BaseBackend):
    """Google Antigravity Claude API 后端.

    通过 Google OAuth2 认证，将 Anthropic 格式请求转换为 Gemini 格式
    发往 Generative AI 端点，并将响应转回 Anthropic 格式.
    """

    def __init__(self, config: AntigravityConfig, failover_config: FailoverConfig) -> None:
        super().__init__(config.base_url, config.timeout_ms, failover_config)
        self._token_manager = GoogleOAuthTokenManager(
            config.client_id, config.client_secret, config.refresh_token,
        )
        self._model_endpoint = config.model_endpoint

    def get_name(self) -> str:
        return "antigravity"

    async def _prepare_request(
        self,
        request_body: dict[str, Any],
        headers: dict[str, str],
    ) -> tuple[dict[str, Any], dict[str, str]]:
        """转换 Anthropic 请求为 Gemini 格式，注入 Google OAuth token."""
        gemini_body = convert_request(request_body)
        token = await self._token_manager.get_token()
        new_headers = {
            "content-type": "application/json",
            "authorization": f"Bearer {token}",
        }
        return gemini_body, new_headers

    def _on_error_status(self, status_code: int) -> None:
        """401/403 时标记 token 失效以触发被动刷新."""
        if status_code in (401, 403):
            self._token_manager.invalidate()

    async def send_message(
        self,
        request_body: dict[str, Any],
        headers: dict[str, str],
    ) -> BackendResponse:
        """覆写: Gemini 端点 + 响应逆转换."""
        body, prepared_headers = await self._prepare_request(request_body, headers)
        client = self._get_client()
        endpoint = f"/{self._model_endpoint}:generateContent"

        response = await client.post(endpoint, json=body, headers=prepared_headers)

        if response.status_code >= 400:
            self._on_error_status(response.status_code)
            return BackendResponse(
                status_code=response.status_code,
                raw_body=response.content,
                error_type="api_error",
                error_message=response.text[:500],
                response_headers=dict(response.headers),
            )

        gemini_resp = response.json()
        model = request_body.get("model", "unknown")
        anthropic_resp = convert_response(gemini_resp, model=model)
        usage_data = extract_usage(gemini_resp)
        raw_body = json.dumps(anthropic_resp).encode()

        return BackendResponse(
            status_code=200,
            raw_body=raw_body,
            usage=UsageInfo(
                input_tokens=usage_data.get("input_tokens", 0),
                output_tokens=usage_data.get("output_tokens", 0),
            ),
            model_served=model,
        )

    async def send_message_stream(
        self,
        request_body: dict[str, Any],
        headers: dict[str, str],
    ) -> AsyncIterator[bytes]:
        """覆写: Gemini SSE 流 → Anthropic SSE 流适配."""
        body, prepared_headers = await self._prepare_request(request_body, headers)
        client = self._get_client()
        endpoint = f"/{self._model_endpoint}:streamGenerateContent?alt=sse"

        async with client.stream(
            "POST", endpoint, json=body, headers=prepared_headers,
        ) as response:
            if response.status_code >= 400:
                self._on_error_status(response.status_code)
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

            model = request_body.get("model", "unknown")
            async for chunk in adapt_sse_stream(response.aiter_bytes(), model):
                yield chunk

    async def close(self) -> None:
        await self._token_manager.close()
        await super().close()
