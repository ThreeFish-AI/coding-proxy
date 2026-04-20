"""用量记录器 — 封装 token 用量日志、定价计算与证据构建."""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from ..logging.db import TokenLogger
    from ..pricing import PricingTable
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
        vendor: str,
        model_requested: str,
        model_served: str,
        duration_ms: int,
        usage: UsageInfo,
    ) -> None:
        """打印模型调用级别的详细 Access Log."""
        cost_str = "-"
        if self._pricing_table is not None:
            cost_value = self._pricing_table.compute_cost(
                vendor=vendor,
                model_served=model_served,
                input_tokens=usage.input_tokens,
                output_tokens=usage.output_tokens,
                cache_creation_tokens=usage.cache_creation_tokens,
                cache_read_tokens=usage.cache_read_tokens,
            )
            if cost_value is not None:
                cost_str = cost_value.format()
        logger.debug(
            "ModelCall: vendor=%s model_requested=%s model_served=%s "
            "duration=%dms tokens=[in:%d out:%d cache_create:%d cache_read:%d] cost=%s",
            vendor,
            model_requested,
            model_served,
            duration_ms,
            usage.input_tokens,
            usage.output_tokens,
            usage.cache_creation_tokens,
            usage.cache_read_tokens,
            cost_str,
        )

    # ── 持久化记录 ────────────────────────────────────────

    async def record(
        self,
        vendor: str,
        model_requested: str,
        model_served: str,
        usage: UsageInfo,
        duration_ms: int,
        success: bool,
        failover: bool,
        failover_from: str | None = None,
        evidence_records: list[dict[str, Any]] | None = None,
        client_category: str = "cc",
        operation: str = "",
        endpoint: str = "",
        extra_usage: dict[str, Any] | None = None,
    ) -> None:
        """记录用量到 TokenLogger.

        Args:
            client_category: 客户端类别（``cc`` = Claude Code，``api`` = 原生 API 透传）。
                默认 ``cc`` 保持既有调用方零改动。
            operation: 规范化操作名（``chat`` / ``embedding`` / ``generate_content`` ...）。
            endpoint: 原始上游路径（``/v1/chat/completions`` ...），用于多维度排障。
            extra_usage: 非规范 token 字段字典（Gemini thoughts / OpenAI reasoning 等），
                序列化为 ``extra_usage_json`` 列供后续分析或补算单价。
        """
        if not self._token_logger:
            return
        extra_usage_json = "{}"
        if extra_usage:
            try:
                extra_usage_json = json.dumps(
                    extra_usage, ensure_ascii=False, sort_keys=True, default=str
                )
            except (TypeError, ValueError):
                # 防御性兜底：任何序列化异常降级为空对象，避免污染主流程
                logger.warning(
                    "Failed to serialize extra_usage for vendor=%s operation=%s",
                    vendor,
                    operation,
                )
                extra_usage_json = "{}"
        await self._token_logger.log(
            vendor=vendor,
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
            client_category=client_category,
            operation=operation,
            endpoint=endpoint,
            extra_usage_json=extra_usage_json,
        )
        if not evidence_records:
            return
        if not hasattr(self._token_logger, "log_evidence"):
            return
        # Evidence 归档策略：
        # - 既有 copilot 流量保持原行为（保证 copilot 相关告警/审计的字段稳定）；
        # - client_category='api' 的原生透传流量全量归档（便于后续补算 reasoning/audio
        #   等非规范 token 的单价与审计模型返回漂移）。
        if vendor != "copilot" and client_category != "api":
            return
        for record in evidence_records:
            await self._token_logger.log_evidence(**record)

    # ── 证据记录构建 ──────────────────────────────────────

    @staticmethod
    def build_nonstream_evidence_records(
        *, vendor: str, model_served: str, usage: UsageInfo
    ) -> list[dict[str, Any]]:
        if vendor != "copilot":
            return []
        raw_usage: dict[str, Any] = {
            "input_tokens": usage.input_tokens,
            "output_tokens": usage.output_tokens,
        }
        if usage.cache_creation_tokens > 0:
            raw_usage["cache_creation_input_tokens"] = usage.cache_creation_tokens
        if usage.cache_read_tokens > 0:
            raw_usage["cache_read_input_tokens"] = usage.cache_read_tokens
        return [
            {
                "vendor": vendor,
                "request_id": usage.request_id,
                "model_served": model_served,
                "evidence_kind": "nonstream_usage_summary",
                "raw_usage_json": json.dumps(
                    raw_usage, ensure_ascii=False, sort_keys=True
                ),
                "parsed_input_tokens": usage.input_tokens,
                "parsed_output_tokens": usage.output_tokens,
                "parsed_cache_creation_tokens": usage.cache_creation_tokens,
                "parsed_cache_read_tokens": usage.cache_read_tokens,
                "cache_signal_present": usage.cache_creation_tokens > 0
                or usage.cache_read_tokens > 0,
                "source_field_map_json": json.dumps(
                    {
                        "input_tokens": "input_tokens",
                        "output_tokens": "output_tokens",
                        "cache_creation_tokens": "cache_creation_input_tokens"
                        if usage.cache_creation_tokens > 0
                        else "",
                        "cache_read_tokens": "cache_read_input_tokens"
                        if usage.cache_read_tokens > 0
                        else "",
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                ),
            }
        ]
