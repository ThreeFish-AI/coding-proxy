"""智谱 GLM 后端 — 使用 API Key 认证."""

from __future__ import annotations

import json
import logging
from typing import Any, AsyncIterator

import httpx

from ..config.schema import FailoverConfig, ZhipuConfig
from ..routing.model_mapper import ModelMapper
from .base import BaseBackend, BackendResponse, UsageInfo

logger = logging.getLogger(__name__)


class ZhipuBackend(BaseBackend):
    """智谱 GLM API 后端.

    使用 Anthropic 兼容接口，将请求转发到智谱 API.
    替换认证头和模型名称.
    """

    def __init__(
        self,
        config: ZhipuConfig,
        failover_config: FailoverConfig,
        model_mapper: ModelMapper,
    ) -> None:
        self._config = config
        self._failover_config = failover_config
        self._model_mapper = model_mapper
        self._client: httpx.AsyncClient | None = None

    def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                base_url=self._config.base_url,
                timeout=httpx.Timeout(self._config.timeout_ms / 1000.0),
            )
        return self._client

    def get_name(self) -> str:
        return "zhipu"

    def _prepare_request(
        self,
        request_body: dict[str, Any],
        headers: dict[str, str],
    ) -> tuple[dict[str, Any], dict[str, str]]:
        """准备转发给智谱的请求：映射模型名、替换认证头."""
        body = {**request_body}

        # 映射模型名称
        if "model" in body:
            body["model"] = self._model_mapper.map(body["model"])

        # 构建智谱认证头
        new_headers = {
            "content-type": "application/json",
            "x-api-key": self._config.api_key,
            "anthropic-version": headers.get("anthropic-version", "2023-06-01"),
        }

        return body, new_headers

    async def send_message_stream(
        self,
        request_body: dict[str, Any],
        headers: dict[str, str],
    ) -> AsyncIterator[bytes]:
        """透传流式请求到智谱 API."""
        body, new_headers = self._prepare_request(request_body, headers)
        client = self._get_client()

        async with client.stream(
            "POST",
            "/v1/messages",
            json=body,
            headers=new_headers,
        ) as response:
            if response.status_code >= 400:
                error_body = await response.aread()
                logger.error(
                    "Zhipu API error: status=%d body=%s",
                    response.status_code,
                    error_body[:500],
                )
                raise httpx.HTTPStatusError(
                    f"Zhipu API error: {response.status_code}",
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
        """发送非流式请求到智谱 API."""
        body, new_headers = self._prepare_request(request_body, headers)
        client = self._get_client()

        response = await client.post(
            "/v1/messages",
            json=body,
            headers=new_headers,
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
        """智谱后端不再触发故障转移（它是最终的 fallback）."""
        return False

    async def close(self) -> None:
        if self._client and not self._client.is_closed:
            await self._client.aclose()
