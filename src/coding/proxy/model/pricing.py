"""定价数据模型.

从 :mod:`coding.proxy.pricing` 正交提取 ``ModelPricing`` dataclass。
``PricingTable`` 查询与计算逻辑保留在原模块。
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class ModelPricing:
    """单个模型的 Token 单价（USD/token）."""

    input_cost_per_token: float = 0.0
    output_cost_per_token: float = 0.0
    cache_creation_input_token_cost: float = 0.0
    cache_read_input_token_cost: float = 0.0
