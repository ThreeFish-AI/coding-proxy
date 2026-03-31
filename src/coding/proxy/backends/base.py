"""后端抽象基类 — 模板方法模式."""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, AsyncIterator

import httpx

from ..config.schema import FailoverConfig

logger = logging.getLogger(__name__)

# 代理转发时应跳过的 hop-by-hop 请求头
PROXY_SKIP_HEADERS = {"host", "content-length", "transfer-encoding", "connection"}


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
    model_served: str | None = None


class BaseBackend(ABC):
    """后端抽象基类，提供 HTTP 客户端管理和请求模板."""

    def __init__(
        self,
        base_url: str,
        timeout_ms: int,
        failover_config: FailoverConfig | None = None,
    ) -> None:
        self._base_url = base_url
        self._timeout_ms = timeout_ms
        self._failover_config = failover_config
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

    def map_model(self, model: str) -> str:
        """将请求模型名映射为后端实际使用的模型名.

        默认实现为恒等映射（无转换）.
        有模型映射需求的后端（如 Zhipu）应覆写此方法.
        """
        return model

    @abstractmethod
    async def _prepare_request(
        self,
        request_body: dict[str, Any],
        headers: dict[str, str],
    ) -> tuple[dict[str, Any], dict[str, str]]:
        """准备请求体和请求头，由子类实现差异化逻辑（支持异步操作）."""

    def _get_endpoint(self) -> str:
        """返回 API 端点路径（默认 /v1/messages）."""
        return "/v1/messages"

    def _on_error_status(self, status_code: int) -> None:
        """响应错误状态码时的钩子（如 token 失效标记）."""

    def should_trigger_failover(self, status_code: int, body: dict[str, Any] | None) -> bool:
        """基于 FailoverConfig 的通用故障转移判断.

        无 failover_config 时返回 False（终端后端默认行为）.
        """
        if self._failover_config is None:
            return False
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
        # 429/503 即使无法解析 body 也触发故障转移
        return status_code in (429, 503)

    async def send_message_stream(
        self,
        request_body: dict[str, Any],
        headers: dict[str, str],
    ) -> AsyncIterator[bytes]:
        """发送消息并返回 SSE 字节流."""
        body, prepared_headers = await self._prepare_request(request_body, headers)
        client = self._get_client()
        endpoint = self._get_endpoint()

        async with client.stream(
            "POST",
            endpoint,
            json=body,
            headers=prepared_headers,
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
            async for chunk in response.aiter_bytes():
                yield chunk

    async def send_message(
        self,
        request_body: dict[str, Any],
        headers: dict[str, str],
    ) -> BackendResponse:
        """发送非流式消息请求."""
        body, prepared_headers = await self._prepare_request(request_body, headers)
        client = self._get_client()
        endpoint = self._get_endpoint()

        response = await client.post(
            endpoint,
            json=body,
            headers=prepared_headers,
        )

        raw_content = response.content
        resp_body = response.json() if response.content else None

        if response.status_code >= 400:
            self._on_error_status(response.status_code)
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
            model_served=resp_body.get("model") if resp_body else None,
        )

    async def close(self) -> None:
        if self._client and not self._client.is_closed:
            await self._client.aclose()
