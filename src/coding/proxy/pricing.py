"""模型定价表.

基于配置文件中的手动定价条目，按 (backend, model_served) 计算 Cost。
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING

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


@dataclass
class ModelPricing:
    """单个模型的 Token 单价（USD/token）."""

    input_cost_per_token: float = 0.0
    output_cost_per_token: float = 0.0
    cache_creation_input_token_cost: float = 0.0
    cache_read_input_token_cost: float = 0.0


class PricingTable:
    """基于配置文件的本地定价表，支持按 (backend, model_served) 查询单价."""

    def __init__(self, entries: list[ModelPricingEntry]) -> None:
        self._index: dict[tuple[str, str], ModelPricing] = {}
        for entry in entries:
            pricing = ModelPricing(
                input_cost_per_token=entry.input_cost_per_mtok / 1e6,
                output_cost_per_token=entry.output_cost_per_mtok / 1e6,
                cache_creation_input_token_cost=entry.cache_write_cost_per_mtok / 1e6,
                cache_read_input_token_cost=entry.cache_read_cost_per_mtok / 1e6,
            )
            # 精确匹配
            self._index[(entry.backend, entry.model)] = pricing
            # 规范化匹配（如 "glm-4.5-air" → "glm-4-5-air"）
            norm = _normalize(entry.model)
            if norm != entry.model:
                self._index.setdefault((entry.backend, norm), pricing)

        if entries:
            logger.info("定价表加载成功，共 %d 条模型配置", len(entries))

    # ── 单价查询 ──────────────────────────────────────────────

    def get_pricing(self, backend: str, model_served: str) -> ModelPricing | None:
        """获取 (backend, model_served) 对应的 ModelPricing.

        查找顺序：
        1. 精确匹配：(backend, model_served)
        2. 规范化匹配：(backend, normalized(model_served))
        """
        hit = self._index.get((backend, model_served))
        if hit is not None:
            return hit
        return self._index.get((backend, _normalize(model_served)))

    # ── 费用计算 ──────────────────────────────────────────────

    def compute_cost(
        self,
        backend: str,
        model_served: str,
        input_tokens: int,
        output_tokens: int,
        cache_creation_tokens: int,
        cache_read_tokens: int,
    ) -> float | None:
        """按单价计算总费用（USD）.

        若无匹配定价返回 None。
        """
        pricing = self.get_pricing(backend, model_served)
        if pricing is None:
            return None
        return (
            input_tokens * pricing.input_cost_per_token
            + output_tokens * pricing.output_cost_per_token
            + cache_creation_tokens * pricing.cache_creation_input_token_cost
            + cache_read_tokens * pricing.cache_read_input_token_cost
        )
