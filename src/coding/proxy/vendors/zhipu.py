"""智谱 GLM 供应商 — 原生 Anthropic 兼容端点薄透传代理.

官方端点 (https://open.bigmodel.cn/api/anthropic) 已完整支持
Anthropic Messages API 协议，本模块仅做两项最小适配：
  1. 模型名映射（Claude -> GLM）
  2. 认证头替换（x-api-key）

额外提供 429 Rate Limit 专用重试挽回机制：
  - max_attempt = 5（1 初始 + 4 重试）
  - 指数退避 + Full Jitter（1s → 2s → 4s → 8s）
  - 优先尊重 server retry-after header
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from typing import Any

import httpx

from ..config.schema import FailoverConfig, ZhipuConfig
from ..routing.model_mapper import ModelMapper
from ..routing.rate_limit import (
    compute_effective_retry_seconds,
    parse_rate_limit_headers,
)
from ..routing.retry import RetryConfig, calculate_delay
from .base import VendorResponse
from .native_anthropic import NativeAnthropicVendor

logger = logging.getLogger(__name__)

# 429 Rate Limit 重试默认配置
_RATE_LIMIT_RETRY = RetryConfig(
    max_retries=4,  # 4 次重试 + 1 次初始 = 5 总尝试
    initial_delay_ms=1000,
    max_delay_ms=30000,
    backoff_multiplier=2.0,
    jitter=True,
)


class ZhipuVendor(NativeAnthropicVendor):
    """智谱 GLM 原生 Anthropic 兼容端点供应商（薄透传 + 429 重试挽回）.

    通过官方 /api/anthropic 端点转发请求，
    仅替换模型名和认证头，其余原样透传。

    429 Rate Limit 时自动重试（指数退避），降低 failover 频率。
    """

    _vendor_name = "zhipu"
    _display_name = "Zhipu"

    def __init__(
        self,
        config: ZhipuConfig,
        model_mapper: ModelMapper,
        failover_config: FailoverConfig | None = None,
    ) -> None:
        super().__init__(config, model_mapper, failover_config)
        self._rl_retry = _RATE_LIMIT_RETRY

    # ── 非流式：429 重试 ────────────────────────────────────

    async def send_message(
        self,
        request_body: dict[str, Any],
        headers: dict[str, str],
    ) -> VendorResponse:
        """非流式请求，429 时自动重试."""
        max_attempts = self._rl_retry.max_attempts

        for attempt in range(max_attempts):
            resp = await super().send_message(request_body, headers)
            if resp.status_code != 429:
                return resp

            if attempt == max_attempts - 1:
                logger.warning(
                    "Zhipu 429 rate limit exhausted after %d attempts",
                    max_attempts,
                )
                return resp

            delay = self._compute_retry_delay_from_headers(
                resp.response_headers, attempt
            )
            logger.info(
                "Zhipu 429 rate limit, retry %d/%d in %.1fms",
                attempt + 1,
                max_attempts - 1,
                delay,
            )
            await asyncio.sleep(delay / 1000.0)

        return resp  # pragma: no cover

    # ── 流式：429 重试 ──────────────────────────────────────

    async def send_message_stream(
        self,
        request_body: dict[str, Any],
        headers: dict[str, str],
    ) -> AsyncIterator[bytes]:
        """流式请求，429 时自动重试.

        安全性：429 在 BaseVendor.send_message_stream 中于
        status code 检查阶段即 raise（在任何 chunk yield 之前），
        因此重试不会导致已发出数据不一致。
        """
        max_attempts = self._rl_retry.max_attempts

        for attempt in range(max_attempts):
            try:
                # 429 在 status code 检查阶段即 raise（在任何 chunk 之前），
                # 因此 __anext__ 安全：要么拿到首个 chunk，要么抛异常。
                ait = super().send_message_stream(request_body, headers)
                head = await ait.__anext__()
            except StopAsyncIteration:
                return
            except httpx.HTTPStatusError as exc:
                if exc.response is None or exc.response.status_code != 429:
                    raise
                if attempt == max_attempts - 1:
                    logger.warning(
                        "Zhipu 429 stream rate limit exhausted after %d attempts",
                        max_attempts,
                    )
                    raise

                delay = self._compute_retry_delay_from_response(exc.response, attempt)
                logger.info(
                    "Zhipu 429 stream rate limit, retry %d/%d in %.1fms",
                    attempt + 1,
                    max_attempts - 1,
                    delay,
                )
                await asyncio.sleep(delay / 1000.0)
                continue

            # yield 在 try/except 之外，避免捕获外部 athrow 的异常
            yield head
            async for chunk in ait:
                yield chunk
            return

    # ── 延迟计算 ────────────────────────────────────────────

    def _compute_retry_delay_from_headers(
        self,
        headers: dict[str, str] | None,
        attempt: int,
    ) -> float:
        """计算重试延迟（毫秒），优先使用 server retry-after."""
        rl_info = parse_rate_limit_headers(headers, 429, None)
        server_delay_s = compute_effective_retry_seconds(rl_info)
        if server_delay_s is not None:
            return min(server_delay_s * 1000, self._rl_retry.max_delay_ms)
        return calculate_delay(attempt, self._rl_retry)

    def _compute_retry_delay_from_response(
        self,
        response: httpx.Response,
        attempt: int,
    ) -> float:
        """计算重试延迟（毫秒），从 httpx.Response 提取 header."""
        rl_info = parse_rate_limit_headers(
            response.headers,
            response.status_code,
            response.text[:500] if response.text else None,
        )
        server_delay_s = compute_effective_retry_seconds(rl_info)
        if server_delay_s is not None:
            return min(server_delay_s * 1000, self._rl_retry.max_delay_ms)
        return calculate_delay(attempt, self._rl_retry)


# 向后兼容别名
ZhipuBackend = ZhipuVendor
