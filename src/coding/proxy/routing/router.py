"""请求路由器 — 带故障转移的路由逻辑."""

from __future__ import annotations

import json
import logging
import time
from typing import Any, AsyncIterator

import httpx

from ..backends.base import BaseBackend, BackendResponse, UsageInfo
from ..logging.db import TokenLogger
from .circuit_breaker import CircuitBreaker
from .quota_guard import QuotaGuard

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
        quota_guard: QuotaGuard | None = None,
    ) -> None:
        self._primary = primary
        self._fallback = fallback
        self._cb = circuit_breaker
        self._token_logger = token_logger
        self._quota_guard = quota_guard

    @property
    def circuit(self) -> CircuitBreaker:
        return self._cb

    def _can_use_primary(self) -> bool:
        """综合判断是否使用主后端（熔断器 + 配额守卫）."""
        if not self._cb.can_execute():
            return False
        if self._quota_guard and not self._quota_guard.can_use_primary():
            return False
        return True

    @staticmethod
    def _build_usage_info(usage: dict[str, Any]) -> UsageInfo:
        """从 SSE 解析的 usage 字典构造 UsageInfo."""
        return UsageInfo(
            input_tokens=usage.get("input_tokens", 0),
            output_tokens=usage.get("output_tokens", 0),
            cache_creation_tokens=usage.get("cache_creation_tokens", 0),
            cache_read_tokens=usage.get("cache_read_tokens", 0),
            request_id=usage.get("request_id", ""),
        )

    def _is_cap_error(self, resp: BackendResponse) -> bool:
        """判断是否为订阅用量上限错误."""
        if resp.status_code not in (429, 403):
            return False
        msg = (resp.error_message or "").lower()
        return any(p in msg for p in ("usage cap", "quota", "limit exceeded"))

    async def route_stream(
        self,
        body: dict[str, Any],
        headers: dict[str, str],
    ) -> AsyncIterator[tuple[bytes, str]]:
        """路由流式请求，yield (chunk, backend_name)."""
        use_primary = self._can_use_primary()
        failover = False
        start = time.monotonic()
        usage: dict[str, Any] = {}

        if use_primary:
            try:
                async for chunk in self._primary.send_message_stream(body, headers):
                    _parse_usage_from_chunk(chunk, usage)
                    yield chunk, self._primary.get_name()
                self._cb.record_success()
                info = self._build_usage_info(usage)
                if self._quota_guard:
                    self._quota_guard.record_primary_success()
                    self._quota_guard.record_usage(info.input_tokens + info.output_tokens)
                duration = int((time.monotonic() - start) * 1000)
                model = body.get("model", "unknown")
                await self._record_usage(
                    self._primary.get_name(), model,
                    usage.get("model_served", model),
                    info, duration, True, False,
                )
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
        model = body.get("model", "unknown")
        await self._record_usage(
            self._fallback.get_name(), model,
            usage.get("model_served", model),
            self._build_usage_info(usage), duration, True, failover,
        )

    async def route_message(
        self,
        body: dict[str, Any],
        headers: dict[str, str],
    ) -> BackendResponse:
        """路由非流式请求."""
        use_primary = self._can_use_primary()
        failover = False
        start = time.monotonic()

        if use_primary:
            try:
                resp = await self._primary.send_message(body, headers)
                if resp.status_code < 400:
                    self._cb.record_success()
                    if self._quota_guard:
                        self._quota_guard.record_primary_success()
                        self._quota_guard.record_usage(
                            resp.usage.input_tokens + resp.usage.output_tokens,
                        )
                    duration = int((time.monotonic() - start) * 1000)
                    model = body.get("model", "unknown")
                    await self._record_usage(
                        self._primary.get_name(), model, model,
                        resp.usage, duration, True, False,
                    )
                    return resp
                if self._primary.should_trigger_failover(resp.status_code, {"error": {"type": resp.error_type, "message": resp.error_message}}):
                    logger.warning("Primary error %d, failing over", resp.status_code)
                    self._cb.record_failure()
                    if self._quota_guard and self._is_cap_error(resp):
                        self._quota_guard.notify_cap_error()
                    failover = True
                else:
                    duration = int((time.monotonic() - start) * 1000)
                    model = body.get("model", "unknown")
                    await self._record_usage(
                        self._primary.get_name(), model, model,
                        resp.usage, duration, resp.status_code < 400, False,
                    )
                    return resp
            except (httpx.TimeoutException, httpx.ConnectError) as exc:
                logger.warning("Primary connection error: %s", exc)
                self._cb.record_failure()
                failover = True

        # Fallback
        resp = await self._fallback.send_message(body, headers)
        duration = int((time.monotonic() - start) * 1000)
        model = body.get("model", "unknown")
        await self._record_usage(
            self._fallback.get_name(), model, model,
            resp.usage, duration, resp.status_code < 400, failover,
        )
        return resp

    async def _record_usage(
        self,
        backend: str,
        model_requested: str,
        model_served: str,
        usage: UsageInfo,
        duration_ms: int,
        success: bool,
        failover: bool,
    ) -> None:
        if not self._token_logger:
            return
        await self._token_logger.log(
            backend=backend,
            model_requested=model_requested,
            model_served=model_served,
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
            cache_creation_tokens=usage.cache_creation_tokens,
            cache_read_tokens=usage.cache_read_tokens,
            duration_ms=duration_ms,
            success=success,
            failover=failover,
            request_id=usage.request_id,
        )

    async def close(self) -> None:
        await self._primary.close()
        await self._fallback.close()
