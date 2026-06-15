"""智谱 GLM 供应商 — 原生 Anthropic 兼容端点代理（兼容转换 + 429/529 重试）.

官方端点 (https://open.bigmodel.cn/api/anthropic) 支持大部分
Anthropic Messages API 协议，本模块做以下适配：
  1. 模型名映射（Claude -> GLM）
  2. 认证头替换（x-api-key）
  3. 首选 tier 参数兼容转换（_prepare_request）

实测验证 GLM 对 Anthropic 扩展参数的处理方式：
- thinking.type="enabled"：原生支持（GLM 有自己的 thinking 机制）
- thinking.type="adaptive"：不支持，触发 [1210] 参数错误 → 转换为 enabled + budget
- cache_control 字段：静默忽略（GLM 使用隐式自动缓存）
- reasoning_effort 参数：静默忽略
- metadata 字段：暂不处理（待进一步诊断确认兼容性）

额外提供 429/529 专用重试挽回机制：
  - 429 Rate Limit（限流）与 529 Overloaded（并发过载）共用同一退避策略
  - max_attempt = 5（1 初始 + 4 重试）
  - 指数退避 + Full Jitter（1s → 2s → 4s → 8s）
  - 优先尊重 server retry-after header

并发限流由 BaseVendor._concurrency_controller 统一管控
（limited 模式），在 executor 层通过 ``track_in_flight`` 触发，
slot 跨 429 重试自然持有（executor 的 async with 包裹整个
send_message/send_message_stream 调用链）。
"""

from __future__ import annotations

import asyncio
import json
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
from .concurrency import ModelConcurrencyController
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

# 触发指数退避重试的瞬态状态码（单一事实源）
#   429 Rate Limit（限流）；529 Overloaded（并发过载）
# 二者均为服务端瞬态过载信号，适用同一退避挽回策略。
_BACKOFF_RETRY_STATUS = frozenset({429, 529})


class ZhipuVendor(NativeAnthropicVendor):
    """智谱 GLM 原生 Anthropic 兼容端点供应商（薄透传 + 429/529 重试挽回）.

    通过官方 /api/anthropic 端点转发请求，
    仅替换模型名和认证头，其余原样透传。

    429 Rate Limit（限流）/ 529 Overloaded（并发过载）时自动重试
    （指数退避），降低 failover 频率。
    并发限流由 BaseVendor._concurrency_controller 统一管控。
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
        # 覆盖 BaseVendor 默认的 monitor 模式为 limited 模式
        self._concurrency_controller = ModelConcurrencyController(config.concurrency)

    # ── 首选 tier 参数兼容转换 ────────────────────────────────

    # adaptive thinking → enabled 的默认预算（Anthropic 推荐的 adaptive 等价值）
    _ADAPTIVE_THINKING_BUDGET = 16000

    async def _prepare_request(
        self,
        request_body: dict[str, Any],
        headers: dict[str, Any],
    ) -> tuple[dict[str, Any], dict[str, str]]:
        """深拷贝 + 模型映射 + 认证头替换 + GLM 兼容转换.

        当 zhipu 作为首选 tier 时（source_vendor=None），请求体来自原始客户端，
        不经过跨供应商转换通道。此处对已知的 GLM 不兼容参数做兼容转换（而非移除），
        保留完整的 CC (Claude Code) 功能特性。
        """
        body, new_headers = await super()._prepare_request(request_body, headers)

        adaptations: list[str] = []

        # thinking.type="adaptive" 是 Anthropic Claude 4.x 新增的类型，
        # GLM 不支持此类型值，会触发 [1210] 参数错误。
        # 转换为 enabled + budget 保留 thinking 能力。
        thinking = body.get("thinking")
        if isinstance(thinking, dict) and thinking.get("type") == "adaptive":
            body["thinking"] = {
                "type": "enabled",
                "budget_tokens": self._ADAPTIVE_THINKING_BUDGET,
            }
            adaptations.append(
                f"converted_thinking_adaptive→enabled"
                f"(budget={self._ADAPTIVE_THINKING_BUDGET})"
            )

        if adaptations:
            logger.debug(
                "ZhipuVendor first-tier compat: %s%s",
                ", ".join(adaptations),
                _build_zhipu_request_snapshot(body),
            )

        return body, new_headers

    # ── 非流式：429/529 重试 ────────────────────────────────

    async def send_message(
        self,
        request_body: dict[str, Any],
        headers: dict[str, str],
    ) -> VendorResponse:
        """非流式请求，429/529 时自动重试."""
        max_attempts = self._rl_retry.max_attempts

        for attempt in range(max_attempts):
            resp = await super().send_message(request_body, headers)
            if resp.status_code not in _BACKOFF_RETRY_STATUS:
                return resp

            if attempt == max_attempts - 1:
                logger.warning(
                    "Zhipu %d overload exhausted after %d attempts",
                    resp.status_code,
                    max_attempts,
                )
                return resp

            delay = self._compute_retry_delay_from_headers(
                resp.response_headers, attempt
            )
            logger.info(
                "Zhipu %d overload, retry %d/%d in %.1fms",
                resp.status_code,
                attempt + 1,
                max_attempts - 1,
                delay,
            )
            await asyncio.sleep(delay / 1000.0)

        return resp  # pragma: no cover

    # ── 流式：429/529 重试 ──────────────────────────────────

    async def send_message_stream(
        self,
        request_body: dict[str, Any],
        headers: dict[str, str],
    ) -> AsyncIterator[bytes]:
        """流式请求，429/529 时自动重试.

        安全性：429/529 在 BaseVendor.send_message_stream 中于
        status code 检查阶段即 raise（在任何 chunk yield 之前），
        因此重试不会导致已发出数据不一致。
        """
        max_attempts = self._rl_retry.max_attempts

        for attempt in range(max_attempts):
            try:
                # 429/529 在 status code 检查阶段即 raise（在任何 chunk 之前），
                # 因此 __anext__ 安全：要么拿到首个 chunk，要么抛异常。
                ait = super().send_message_stream(request_body, headers)
                head = await ait.__anext__()
            except StopAsyncIteration:
                return
            except httpx.HTTPStatusError as exc:
                if (
                    exc.response is None
                    or exc.response.status_code not in _BACKOFF_RETRY_STATUS
                ):
                    raise
                status_code = exc.response.status_code
                if attempt == max_attempts - 1:
                    logger.warning(
                        "Zhipu %d stream overload exhausted after %d attempts",
                        status_code,
                        max_attempts,
                    )
                    raise

                delay = self._compute_retry_delay_from_headers(
                    exc.response.headers, attempt
                )
                logger.info(
                    "Zhipu %d stream overload, retry %d/%d in %.1fms",
                    status_code,
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
        """计算重试延迟（毫秒），优先使用 server retry-after.

        非流式与流式 429/529 重试共用此方法（单一延迟逻辑）。
        以"限流退避"语义（429）解析 header：``parse_rate_limit_headers``
        仅对 429/403 解析 retry-after，故此处固定传 429，
        使 529 也能尊重 server retry-after，与 429 行为一致。
        无 server 信号时回退到指数退避 + Full Jitter。
        """
        rl_info = parse_rate_limit_headers(headers, 429, None)
        server_delay_s = compute_effective_retry_seconds(rl_info)
        if server_delay_s is not None:
            return min(server_delay_s * 1000, self._rl_retry.max_delay_ms)
        return calculate_delay(attempt, self._rl_retry)


# 向后兼容别名
ZhipuBackend = ZhipuVendor


def _build_zhipu_request_snapshot(body: dict[str, Any]) -> str:
    """构建发往 zhipu 请求的轻量参数快照，用于诊断日志.

    输出格式与 executor._build_semantic_rejection_diagnostic 一致，
    使成功请求和失败请求的日志可直接 diff 对比，定位差异维度。

    仅在转换发生时输出（DEBUG 级别），避免常态化日志噪声。
    """
    parts: list[str] = []
    parts.append(f"messages={len(body.get('messages', []))}")

    thinking = body.get("thinking")
    if isinstance(thinking, dict):
        parts.append(f"thinking_type={thinking.get('type', 'unknown')}")

    metadata = body.get("metadata")
    if isinstance(metadata, dict) and metadata:
        parts.append(f"metadata_keys={len(metadata)}")

    tools = body.get("tools")
    if isinstance(tools, list):
        parts.append(f"tools={len(tools)}")

    system = body.get("system")
    if isinstance(system, list):
        parts.append(f"system_blocks={len(system)}")

    try:
        body_bytes = len(json.dumps(body, ensure_ascii=False).encode("utf-8"))
        parts.append(f"body_bytes={body_bytes}")
    except (TypeError, ValueError):
        pass

    return f" [{', '.join(parts)}]" if parts else ""
