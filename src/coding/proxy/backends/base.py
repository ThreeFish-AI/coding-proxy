"""后端抽象基类 — 模板方法模式."""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, AsyncIterator

import httpx

logger = logging.getLogger(__name__)


@dataclass
class UsageInfo:
    """一次调用的 Token 用量."""

    input_tokens: int = 0
    output_tokens: int = 0
    cache_creation_tokens: int = 0
    cache_read_tokens: int = 0
    request_id: str = ""


@dataclass
class BackendResponse:
    """后端响应结果."""

    status_code: int = 200
    usage: UsageInfo = field(default_factory=UsageInfo)
    is_streaming: bool = False
    raw_body: bytes = b"{}"
    error_type: str | None = None
    error_message: str | None = None


class BaseBackend(ABC):
    """后端抽象基类，提供 HTTP 客户端管理和请求模板."""

    def __init__(self, base_url: str, timeout_ms: int) -> None:
        self._base_url = base_url
        self._timeout_ms = timeout_ms
        self._client: httpx.AsyncClient | None = None

    def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                base_url=self._base_url,
                timeout=httpx.Timeout(self._timeout_ms / 1000.0),
            )
        return self._client

    @abstractmethod
    def get_name(self) -> str:
        """返回后端名称（用于日志）."""

    @abstractmethod
    def _prepare_request(
        self,
        request_body: dict[str, Any],
        headers: dict[str, str],
    ) -> tuple[dict[str, Any], dict[str, str]]:
        """准备请求体和请求头，由子类实现差异化逻辑."""

    @abstractmethod
    def should_trigger_failover(self, status_code: int, body: dict[str, Any] | None) -> bool:
        """判断响应是否应触发故障转移."""

    async def send_message_stream(
        self,
        request_body: dict[str, Any],
        headers: dict[str, str],
    ) -> AsyncIterator[bytes]:
        """发送消息并返回 SSE 字节流."""
        body, prepared_headers = self._prepare_request(request_body, headers)
        client = self._get_client()

        async with client.stream(
            "POST",
            "/v1/messages",
            json=body,
            headers=prepared_headers,
        ) as response:
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

    async def send_message(
        self,
        request_body: dict[str, Any],
        headers: dict[str, str],
    ) -> BackendResponse:
        """发送非流式消息请求."""
        body, prepared_headers = self._prepare_request(request_body, headers)
        client = self._get_client()

        response = await client.post(
            "/v1/messages",
            json=body,
            headers=prepared_headers,
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

    async def close(self) -> None:
        if self._client and not self._client.is_closed:
            await self._client.aclose()
