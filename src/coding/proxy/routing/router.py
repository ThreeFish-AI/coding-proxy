"""请求路由器 — N-tier 链式路由与自动故障转移."""

from __future__ import annotations

import json
import logging
import time
from typing import Any, AsyncIterator

import httpx

from ..backends.base import BackendResponse, UsageInfo
from ..logging.db import TokenLogger
from .tier import BackendTier

logger = logging.getLogger(__name__)


def _set_if_nonzero(usage: dict, key: str, value: int) -> None:
    """仅在 value 非零时设置，避免后续 chunk 的 0 值覆盖已提取的非零值."""
    if value:
        usage[key] = value


def _parse_usage_from_chunk(chunk: bytes, usage: dict) -> None:
    """从 SSE chunk 提取 token 用量.

    同时支持 Anthropic 原生格式和 OpenAI/Zhipu 兼容格式：
    - Anthropic: data.message.usage.input_tokens / data.usage.output_tokens
    - OpenAI/Zhipu: 顶层 data.usage.prompt_tokens / data.usage.completion_tokens
    """
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

        # Anthropic 格式: message_start 事件 (data.message.usage)
        msg = data.get("message", {})
        if isinstance(msg, dict) and "usage" in msg:
            u = msg["usage"]
            _set_if_nonzero(
                usage, "input_tokens",
                u.get("input_tokens", 0) or u.get("prompt_tokens", 0),
            )
            _set_if_nonzero(usage, "cache_creation_tokens", u.get("cache_creation_input_tokens", 0))
            _set_if_nonzero(usage, "cache_read_tokens", u.get("cache_read_input_tokens", 0))
            if "id" in msg:
                usage["request_id"] = msg["id"]
            if "model" in msg:
                usage["model_served"] = msg["model"]

        # Anthropic message_delta / OpenAI 最后一个 chunk (data.usage)
        if "usage" in data:
            u = data["usage"]
            _set_if_nonzero(
                usage, "output_tokens",
                u.get("output_tokens", 0) or u.get("completion_tokens", 0),
            )
            _set_if_nonzero(
                usage, "input_tokens",
                u.get("input_tokens", 0) or u.get("prompt_tokens", 0),
            )

        # request_id fallback (OpenAI 格式下 id 在顶层)
        if "id" in data and not usage.get("request_id"):
            usage["request_id"] = data["id"]


class RequestRouter:
    """路由请求到合适的后端层级，按优先级链式故障转移."""

    def __init__(
        self,
        tiers: list[BackendTier],
        token_logger: TokenLogger | None = None,
    ) -> None:
        if not tiers:
            raise ValueError("至少需要一个后端层级")
        self._tiers = tiers
        self._token_logger = token_logger

    @property
    def tiers(self) -> list[BackendTier]:
        return self._tiers

    @staticmethod
    def _build_usage_info(usage: dict[str, Any]) -> UsageInfo:
        return UsageInfo(
            input_tokens=usage.get("input_tokens", 0),
            output_tokens=usage.get("output_tokens", 0),
            cache_creation_tokens=usage.get("cache_creation_tokens", 0),
            cache_read_tokens=usage.get("cache_read_tokens", 0),
            request_id=usage.get("request_id", ""),
        )

    @staticmethod
    def _is_cap_error(resp: BackendResponse) -> bool:
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
        """路由流式请求，按优先级尝试各层级."""
        last_idx = len(self._tiers) - 1
        last_exc: Exception | None = None
        failed_tier_name: str | None = None

        for i, tier in enumerate(self._tiers):
            is_last = i == last_idx

            if not tier.can_execute() and not is_last:
                continue

            start = time.monotonic()
            usage: dict[str, Any] = {}

            try:
                async for chunk in tier.backend.send_message_stream(body, headers):
                    _parse_usage_from_chunk(chunk, usage)
                    yield chunk, tier.name

                info = self._build_usage_info(usage)
                if info.input_tokens == 0 and info.output_tokens > 0:
                    logger.warning(
                        "Stream completed with input_tokens=0, output_tokens=%d, tier=%s",
                        info.output_tokens, tier.name,
                    )
                tier.record_success(info.input_tokens + info.output_tokens)
                duration = int((time.monotonic() - start) * 1000)
                model = body.get("model", "unknown")
                model_served = usage.get("model_served") or tier.backend.map_model(model)
                await self._record_usage(
                    tier.name, model, model_served,
                    info, duration, True,
                    failed_tier_name is not None, failed_tier_name,
                )
                return
            except (httpx.HTTPStatusError, httpx.TimeoutException, httpx.ConnectError) as exc:
                logger.warning("Tier %s stream failed: %s", tier.name, exc)
                tier.record_failure()
                failed_tier_name = tier.name
                last_exc = exc
                if is_last:
                    raise

        if last_exc:
            raise last_exc

    async def route_message(
        self,
        body: dict[str, Any],
        headers: dict[str, str],
    ) -> BackendResponse:
        """路由非流式请求，按优先级尝试各层级."""
        last_idx = len(self._tiers) - 1
        start = time.monotonic()
        failed_tier_name: str | None = None

        for i, tier in enumerate(self._tiers):
            is_last = i == last_idx

            if not tier.can_execute() and not is_last:
                continue

            try:
                resp = await tier.backend.send_message(body, headers)

                if resp.status_code < 400:
                    tier.record_success(resp.usage.input_tokens + resp.usage.output_tokens)
                    duration = int((time.monotonic() - start) * 1000)
                    model = body.get("model", "unknown")
                    model_served = resp.model_served or model
                    await self._record_usage(
                        tier.name, model, model_served,
                        resp.usage, duration, True,
                        failed_tier_name is not None, failed_tier_name,
                    )
                    return resp

                if not is_last and tier.backend.should_trigger_failover(
                    resp.status_code,
                    {"error": {"type": resp.error_type, "message": resp.error_message}},
                ):
                    logger.warning("Tier %s error %d, failing over", tier.name, resp.status_code)
                    tier.record_failure(is_cap_error=self._is_cap_error(resp))
                    failed_tier_name = tier.name
                    continue

                duration = int((time.monotonic() - start) * 1000)
                model = body.get("model", "unknown")
                model_served = resp.model_served or model
                await self._record_usage(
                    tier.name, model, model_served,
                    resp.usage, duration, resp.status_code < 400,
                    failed_tier_name is not None, failed_tier_name,
                )
                return resp

            except (httpx.TimeoutException, httpx.ConnectError) as exc:
                logger.warning("Tier %s connection error: %s", tier.name, exc)
                tier.record_failure()
                failed_tier_name = tier.name
                if is_last:
                    raise
                continue

        raise RuntimeError("无可用后端层级")

    async def _record_usage(
        self,
        backend: str,
        model_requested: str,
        model_served: str,
        usage: UsageInfo,
        duration_ms: int,
        success: bool,
        failover: bool,
        failover_from: str | None = None,
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
            failover_from=failover_from,
            request_id=usage.request_id,
        )

    async def close(self) -> None:
        for tier in self._tiers:
            await tier.backend.close()
