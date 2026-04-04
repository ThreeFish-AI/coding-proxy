"""用量记录器 — 封装 token 用量日志、定价计算与证据构建."""

from __future__ import annotations

import json
import logging
import time
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from ..pricing import PricingTable
    from ..logging.db import TokenLogger
    from .usage_parser import UsageInfo

logger = logging.getLogger(__name__)


class UsageRecorder:
    """封装路由层的用量记录、定价日志与证据构建逻辑."""

    def __init__(
        self,
        token_logger: TokenLogger | None = None,
        pricing_table: PricingTable | None = None,
    ) -> None:
        self._token_logger = token_logger
        self._pricing_table = pricing_table

    def set_pricing_table(self, table: PricingTable) -> None:
        self._pricing_table = table

    # ── 用量信息构建 ──────────────────────────────────────

    @staticmethod
    def build_usage_info(usage: dict[str, Any]) -> UsageInfo:
        from .usage_parser import UsageInfo

        return UsageInfo(
            input_tokens=usage.get("input_tokens", 0),
            output_tokens=usage.get("output_tokens", 0),
            cache_creation_tokens=usage.get("cache_creation_tokens", 0),
            cache_read_tokens=usage.get("cache_read_tokens", 0),
            request_id=usage.get("request_id", ""),
        )

    # ── 模型调用日志 ──────────────────────────────────────

    def log_model_call(
        self,
        *,
        backend: str,
        model_requested: str,
        model_served: str,
        duration_ms: int,
        usage: UsageInfo,
    ) -> None:
        """打印模型调用级别的详细 Access Log."""
        cost_str = "-"
        if self._pricing_table is not None:
            cost = self._pricing_table.compute_cost(
                backend=backend,
                model_served=model_served,
                input_tokens=usage.input_tokens,
                output_tokens=usage.output_tokens,
                cache_creation_tokens=usage.cache_creation_tokens,
                cache_read_tokens=usage.cache_read_tokens,
            )
            if cost is not None:
                cost_str = f"${cost:.4f}"
        logger.info(
            "ModelCall: backend=%s model_requested=%s model_served=%s "
            "duration=%dms tokens=[in:%d out:%d cache_create:%d cache_read:%d] cost=%s",
            backend, model_requested, model_served, duration_ms,
            usage.input_tokens, usage.output_tokens,
            usage.cache_creation_tokens, usage.cache_read_tokens, cost_str,
        )

    # ── 持久化记录 ────────────────────────────────────────

    async def record(
        self,
        backend: str,
        model_requested: str,
        model_served: str,
        usage: UsageInfo,
        duration_ms: int,
        success: bool,
        failover: bool,
        failover_from: str | None = None,
        evidence_records: list[dict[str, Any]] | None = None,
    ) -> None:
        if not self._token_logger:
            return
        await self._token_logger.log(
            backend=backend, model_requested=model_requested, model_served=model_served,
            input_tokens=usage.input_tokens, output_tokens=usage.output_tokens,
            cache_creation_tokens=usage.cache_creation_tokens, cache_read_tokens=usage.cache_read_tokens,
            duration_ms=duration_ms, success=success, failover=failover, failover_from=failover_from,
            request_id=usage.request_id,
        )
        if not evidence_records or backend != "copilot":
            return
        if not hasattr(self._token_logger, "log_evidence"):
            return
        for record in evidence_records:
            await self._token_logger.log_evidence(**record)

    # ── 证据记录构建 ──────────────────────────────────────

    @staticmethod
    def build_nonstream_evidence_records(*, backend: str, model_served: str, usage: UsageInfo) -> list[dict[str, Any]]:
        if backend != "copilot":
            return []
        raw_usage: dict[str, Any] = {"input_tokens": usage.input_tokens, "output_tokens": usage.output_tokens}
        if usage.cache_creation_tokens > 0:
            raw_usage["cache_creation_input_tokens"] = usage.cache_creation_tokens
        if usage.cache_read_tokens > 0:
            raw_usage["cache_read_input_tokens"] = usage.cache_read_tokens
        return [{
            "backend": backend, "request_id": usage.request_id, "model_served": model_served,
            "evidence_kind": "nonstream_usage_summary",
            "raw_usage_json": json.dumps(raw_usage, ensure_ascii=False, sort_keys=True),
            "parsed_input_tokens": usage.input_tokens, "parsed_output_tokens": usage.output_tokens,
            "parsed_cache_creation_tokens": usage.cache_creation_tokens, "parsed_cache_read_tokens": usage.cache_read_tokens,
            "cache_signal_present": usage.cache_creation_tokens > 0 or usage.cache_read_tokens > 0,
            "source_field_map_json": json.dumps({
                "input_tokens": "input_tokens", "output_tokens": "output_tokens",
                "cache_creation_tokens": "cache_creation_input_tokens" if usage.cache_creation_tokens > 0 else "",
                "cache_read_tokens": "cache_read_input_tokens" if usage.cache_read_tokens > 0 else "",
            }, ensure_ascii=False, sort_keys=True),
        }]
