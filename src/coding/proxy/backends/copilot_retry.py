"""Copilot 421 Misdirected 重试策略 — 同步请求与流式请求共用."""

from __future__ import annotations

from typing import Any, AsyncIterator

import httpx

from .copilot_urls import _normalize_base_url


class Copilot421RetryHandler:
    """封装 Copilot 421 Misdirected 重试策略.

    GitHub Copilot API 在某些情况下返回 421 Misdirected Request，
    表示当前端点不可用，需尝试其他候选 URL。此处理器统一了
    同步请求和流式请求的 421 重试逻辑。
    """

    def __init__(self, backend: Any) -> None:
        self._backend = backend

    async def execute_request_with_retry(
        self,
        method: str,
        endpoint: str,
        *,
        headers: dict[str, str],
        json_body: dict[str, Any] | None = None,
    ) -> httpx.Response:
        """同步请求的 421 重试."""
        current_base_url = self._backend._resolved_base_url
        self._backend._begin_request(current_base_url)

        response = await self._backend._get_client().request(
            method, endpoint, json=json_body, headers=headers,
        )
        if response.status_code != 421:
            return response

        self._backend._last_421_base_url = current_base_url
        last_response = response

        for retry_base_url in self._backend._retry_base_urls(current_base_url):
            self._backend._last_retry_base_url = retry_base_url
            async with self._backend._create_fresh_client(retry_base_url) as retry_client:
                retry_response = await retry_client.request(
                    method, endpoint, json=json_body, headers=headers,
                )
            last_response = retry_response
            if retry_response.status_code != 421:
                await self._backend._activate_base_url(retry_base_url)
                return retry_response
            self._backend._last_421_base_url = retry_base_url

        return last_response

    async def execute_stream_with_retry(
        self,
        stream_fn: Any,
    ) -> AsyncIterator[bytes]:
        """流式请求的 421 重试（异步生成器）.

        Args:
            stream_fn: 接受 httpx.AsyncClient 并返回 AsyncIterator[bytes] 的可调用对象
        """
        current_base_url = self._backend._resolved_base_url
        self._backend._begin_request(current_base_url)
        last_exc: httpx.HTTPStatusError | None = None

        try:
            async for chunk in stream_fn(self._backend._get_client()):
                yield chunk
            return
        except httpx.HTTPStatusError as exc:
            if exc.response is None or exc.response.status_code != 421:
                raise
            self._backend._last_421_base_url = _normalize_base_url(current_base_url)
            last_exc = exc

        for retry_base_url in self._backend._retry_base_urls(current_base_url):
            self._backend._last_retry_base_url = retry_base_url
            async with self._backend._create_fresh_client(retry_base_url) as retry_client:
                try:
                    async for chunk in stream_fn(retry_client):
                        yield chunk
                    await self._backend._activate_base_url(retry_base_url)
                    return
                except httpx.HTTPStatusError as retry_exc:
                    last_exc = retry_exc
                    if retry_exc.response is None or retry_exc.response.status_code != 421:
                        raise
                    self._backend._last_421_base_url = retry_base_url

        if last_exc:
            raise last_exc
