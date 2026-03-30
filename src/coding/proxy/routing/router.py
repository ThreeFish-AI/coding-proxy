"""请求路由器 — 带故障转移的路由逻辑."""

from __future__ import annotations

import json
import logging
import time
from typing import Any, AsyncIterator

import httpx

from ..backends.base import BaseBackend, BackendResponse
from ..logging.db import TokenLogger
from .circuit_breaker import CircuitBreaker

logger = logging.getLogger(__name__)


def _parse_usage_from_chunk(chunk: bytes, usage: dict) -> None:
    """从 SSE chunk 提取 token 用量."""
    text = chunk.decode("utf-8", errors="ignore")
    for line in text.split("\n"):
        if not line.startswith("data: "):
            continue
        payload = line[6:].strip()
        if not payload or payload == "[DONE]":
            continue
        try:
            data = json.loads(payload)
        except json.JSONDecodeError:
            continue
        # message_start: {"type":"message_start","message":{"usage":{...}}}
        msg = data.get("message", {})
        if isinstance(msg, dict) and "usage" in msg:
            u = msg["usage"]
            usage["input_tokens"] = u.get("input_tokens", 0)
            usage["cache_creation_tokens"] = u.get("cache_creation_input_tokens", 0)
            usage["cache_read_tokens"] = u.get("cache_read_input_tokens", 0)
            if "id" in msg:
                usage["request_id"] = msg["id"]
            if "model" in msg:
                usage["model_served"] = msg["model"]
        # message_delta: {"type":"message_delta","usage":{"output_tokens":N}}
        if "usage" in data and "message" not in data:
            usage["output_tokens"] = data["usage"].get("output_tokens", 0)


class RequestRouter:
    """路由请求到合适的后端，支持自动故障转移."""

    def __init__(
        self,
        primary: BaseBackend,
        fallback: BaseBackend,
        circuit_breaker: CircuitBreaker,
        token_logger: TokenLogger | None = None,
    ) -> None:
        self._primary = primary
        self._fallback = fallback
        self._cb = circuit_breaker
        self._token_logger = token_logger

    @property
    def circuit(self) -> CircuitBreaker:
        return self._cb

    async def route_stream(
        self,
        body: dict[str, Any],
        headers: dict[str, str],
    ) -> AsyncIterator[tuple[bytes, str]]:
        """路由流式请求，yield (chunk, backend_name)."""
        use_primary = self._cb.can_execute()
        failover = False
        start = time.monotonic()
        usage: dict[str, Any] = {}

        if use_primary:
            try:
                async for chunk in self._primary.send_message_stream(body, headers):
                    _parse_usage_from_chunk(chunk, usage)
                    yield chunk, self._primary.get_name()
                self._cb.record_success()
                duration = int((time.monotonic() - start) * 1000)
                await self._record(usage, body, self._primary.get_name(), duration, False)
                return
            except (httpx.HTTPStatusError, httpx.TimeoutException, httpx.ConnectError) as exc:
                logger.warning("Primary failed: %s", exc)
                self._cb.record_failure()
                failover = True
                usage.clear()

        # Fallback
        start = time.monotonic()
        async for chunk in self._fallback.send_message_stream(body, headers):
            _parse_usage_from_chunk(chunk, usage)
            yield chunk, self._fallback.get_name()
        duration = int((time.monotonic() - start) * 1000)
        await self._record(usage, body, self._fallback.get_name(), duration, failover)

    async def route_message(
        self,
        body: dict[str, Any],
        headers: dict[str, str],
    ) -> BackendResponse:
        """路由非流式请求."""
        use_primary = self._cb.can_execute()
        failover = False
        start = time.monotonic()

        if use_primary:
            try:
                resp = await self._primary.send_message(body, headers)
                if resp.status_code < 400:
                    self._cb.record_success()
                    duration = int((time.monotonic() - start) * 1000)
                    await self._record_response(resp, body, self._primary.get_name(), duration, False)
                    return resp
                if self._primary.should_trigger_failover(resp.status_code, {"error": {"type": resp.error_type, "message": resp.error_message}}):
                    logger.warning("Primary error %d, failing over", resp.status_code)
                    self._cb.record_failure()
                    failover = True
                else:
                    duration = int((time.monotonic() - start) * 1000)
                    await self._record_response(resp, body, self._primary.get_name(), duration, False)
                    return resp
            except (httpx.TimeoutException, httpx.ConnectError) as exc:
                logger.warning("Primary connection error: %s", exc)
                self._cb.record_failure()
                failover = True

        # Fallback
        resp = await self._fallback.send_message(body, headers)
        duration = int((time.monotonic() - start) * 1000)
        await self._record_response(resp, body, self._fallback.get_name(), duration, failover)
        return resp

    async def _record(
        self,
        usage: dict,
        body: dict,
        backend: str,
        duration_ms: int,
        failover: bool,
    ) -> None:
        if not self._token_logger:
            return
        await self._token_logger.log(
            backend=backend,
            model_requested=body.get("model", "unknown"),
            model_served=usage.get("model_served", body.get("model", "unknown")),
            input_tokens=usage.get("input_tokens", 0),
            output_tokens=usage.get("output_tokens", 0),
            cache_creation_tokens=usage.get("cache_creation_tokens", 0),
            cache_read_tokens=usage.get("cache_read_tokens", 0),
            duration_ms=duration_ms,
            success=True,
            failover=failover,
            request_id=usage.get("request_id", ""),
        )

    async def _record_response(
        self,
        resp: BackendResponse,
        body: dict,
        backend: str,
        duration_ms: int,
        failover: bool,
    ) -> None:
        if not self._token_logger:
            return
        await self._token_logger.log(
            backend=backend,
            model_requested=body.get("model", "unknown"),
            model_served=body.get("model", "unknown"),
            input_tokens=resp.usage.input_tokens,
            output_tokens=resp.usage.output_tokens,
            cache_creation_tokens=resp.usage.cache_creation_tokens,
            cache_read_tokens=resp.usage.cache_read_tokens,
            duration_ms=duration_ms,
            success=resp.status_code < 400,
            failover=failover,
            request_id=resp.usage.request_id,
        )

    async def close(self) -> None:
        await self._primary.close()
        await self._fallback.close()
