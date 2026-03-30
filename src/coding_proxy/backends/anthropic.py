"""Anthropic 官方后端 — 透传 OAuth token."""

from __future__ import annotations

import json
import logging
from typing import Any, AsyncIterator

import httpx

from ..config.schema import AnthropicConfig, FailoverConfig
from .base import BaseBackend, BackendResponse, UsageInfo

logger = logging.getLogger(__name__)


class AnthropicBackend(BaseBackend):
    """Anthropic 官方 API 后端.

    透传 Claude Code 发来的 OAuth token 和请求体到 Anthropic API.
    """

    def __init__(self, config: AnthropicConfig, failover_config: FailoverConfig) -> None:
        self._config = config
        self._failover_config = failover_config
        self._client: httpx.AsyncClient | None = None

    def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                base_url=self._config.base_url,
                timeout=httpx.Timeout(self._config.timeout_ms / 1000.0),
            )
        return self._client

    def get_name(self) -> str:
        return "anthropic"

    async def send_message_stream(
        self,
        request_body: dict[str, Any],
        headers: dict[str, str],
    ) -> AsyncIterator[bytes]:
        """透传流式请求到 Anthropic API."""
        client = self._get_client()
        filtered_headers = self._filter_headers(headers)

        async with client.stream(
            "POST",
            "/v1/messages",
            json=request_body,
            headers=filtered_headers,
        ) as response:
            if response.status_code >= 400:
                body = await response.aread()
                raise httpx.HTTPStatusError(
                    f"Anthropic API error: {response.status_code}",
                    request=response.request,
                    response=httpx.Response(
                        response.status_code,
                        content=body,
                        headers=response.headers,
                        request=response.request,
                    ),
                )
            async for chunk in response.aiter_bytes():
                yield chunk

    async def send_message(
        self,
        request_body: dict[str, Any],
        headers: dict[str, str],
    ) -> BackendResponse:
        """发送非流式请求到 Anthropic API."""
        client = self._get_client()
        filtered_headers = self._filter_headers(headers)

        response = await client.post(
            "/v1/messages",
            json=request_body,
            headers=filtered_headers,
        )

        raw_content = response.content
        resp_body = response.json() if response.content else None

        if response.status_code >= 400:
            return BackendResponse(
                status_code=response.status_code,
                raw_body=raw_content,
                error_type=resp_body.get("error", {}).get("type") if resp_body else None,
                error_message=resp_body.get("error", {}).get("message") if resp_body else None,
            )

        usage = resp_body.get("usage", {}) if resp_body else {}
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

    def should_trigger_failover(self, status_code: int, body: dict[str, Any] | None) -> bool:
        """判断是否应触发故障转移."""
        if status_code not in self._failover_config.status_codes:
            return False

        if body and "error" in body:
            error = body["error"]
            error_type = error.get("type", "")
            error_message = error.get("message", "").lower()

            if error_type in self._failover_config.error_types:
                return True

            for pattern in self._failover_config.error_message_patterns:
                if pattern.lower() in error_message:
                    return True

        # 对于 429 和 503，即使无法解析 body 也触发故障转移
        return status_code in (429, 503)

    def _filter_headers(self, headers: dict[str, str]) -> dict[str, str]:
        """过滤转发给 Anthropic 的请求头."""
        skip = {"host", "content-length", "transfer-encoding", "connection"}
        return {k: v for k, v in headers.items() if k.lower() not in skip}

    async def close(self) -> None:
        if self._client and not self._client.is_closed:
            await self._client.aclose()
