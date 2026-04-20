"""模型定价表.

基于配置文件中的手动定价条目，按 (vendor, model_served) 计算 Cost。

``ModelPricing`` / ``Currency`` / ``CostValue`` 数据模型已迁移至 :mod:`coding.proxy.model.pricing`。
本文件保留 ``PricingTable`` 查询与计算逻辑，类型通过 re-export 提供。

.. deprecated::
    未来版本将移除类型 re-export，请直接从 :mod:`coding.proxy.model.pricing` 导入。
"""

from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING

# noqa: F401
from .model.pricing import CostValue, Currency, ModelPricing

if TYPE_CHECKING:
    from .config.schema import ModelPricingEntry

logger = logging.getLogger(__name__)


def _normalize(name: str) -> str:
    """规范化模型名称以提升匹配成功率.

    规则：
    - 去除 @版本后缀（如 @20241022）
    - 将 `.` 替换为 `-`
    - 转小写
    """
    name = re.sub(r"@[\w.]+$", "", name)
    return name.replace(".", "-").lower()


class PricingTable:
    """基于配置文件的本地定价表，支持按 (vendor, model_served) 查询单价."""

    def __init__(self, entries: list[ModelPricingEntry]) -> None:
        self._index: dict[tuple[str, str], ModelPricing] = {}
        for entry in entries:
            pricing = ModelPricing(
                currency=Currency(entry.currency),
                input_cost_per_token=entry.input_cost_per_mtok / 1e6,
                output_cost_per_token=entry.output_cost_per_mtok / 1e6,
                cache_creation_input_token_cost=entry.cache_write_cost_per_mtok / 1e6,
                cache_read_input_token_cost=entry.cache_read_cost_per_mtok / 1e6,
            )
            # 精确匹配
            self._index[(entry.vendor, entry.model)] = pricing
            # 规范化匹配（如 "glm-4.5-air" → "glm-4-5-air"）
            norm = _normalize(entry.model)
            if norm != entry.model:
                self._index.setdefault((entry.vendor, norm), pricing)

        if entries:
            logger.info("定价表加载成功，共 %d 条模型配置", len(entries))

    # ── 单价查询 ──────────────────────────────────────────────

    def get_pricing(self, vendor: str, model_served: str) -> ModelPricing | None:
        """获取 (vendor, model_served) 对应的 ModelPricing.

        查找顺序：
        1. 精确匹配：(vendor, model_served)
        2. 规范化匹配：(vendor, normalized(model_served))
        """
        hit = self._index.get((vendor, model_served))
        if hit is not None:
            return hit
        return self._index.get((vendor, _normalize(model_served)))

    # ── 费用计算 ──────────────────────────────────────────────

    def compute_cost(
        self,
        vendor: str,
        model_served: str,
        input_tokens: int,
        output_tokens: int,
        cache_creation_tokens: int,
        cache_read_tokens: int,
        *,
        extra_tokens: dict[str, int] | None = None,
    ) -> CostValue | None:
        """按单价计算总费用（含币种信息）.

        Args:
            extra_tokens: 非规范 token 字段字典（如 ``{"reasoning_tokens": 128,
                "audio_input_tokens": 32, "cache_5m_tokens": 512}``），
                供未来 PR 追加 reasoning / audio / tiered-cache 单价计算。
                当前版本不参与计算，仅作为前向兼容钩子保留。

        Returns:
            :class:`CostValue`（携带币种）。若无匹配定价返回 ``None``。

        Note:
            为避免本 PR 引入非预期账单变化，``extra_tokens`` 默认不参与计算；
            当 ``ModelPricingEntry`` 新增 ``reasoning_cost_per_mtok`` 等字段后，
            可在本方法内按需追加 ``extra_tokens.get(...) * entry.extra_cost`` 分项。
        """
        pricing = self.get_pricing(vendor, model_served)
        if pricing is None:
            return None
        amount = (
            input_tokens * pricing.input_cost_per_token
            + output_tokens * pricing.output_cost_per_token
            + cache_creation_tokens * pricing.cache_creation_input_token_cost
            + cache_read_tokens * pricing.cache_read_input_token_cost
        )
        # extra_tokens 预留钩子：当前 no-op 以保证既有账单完全不变
        _ = extra_tokens
        return CostValue(amount=amount, currency=pricing.currency)
