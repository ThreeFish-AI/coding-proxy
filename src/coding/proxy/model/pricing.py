"""定价数据模型.

从 :mod:`coding.proxy.pricing` 正交提取 ``ModelPricing`` dataclass。
``PricingTable`` 查询与计算逻辑保留在原模块。

本模块同时定义 ``Currency`` 枚举和 ``CostValue`` 值对象，
支撑双币种（USD/CNY）计费能力。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class Currency(StrEnum):
    """支持的币种."""

    USD = "USD"
    CNY = "CNY"

    @property
    def symbol(self) -> str:
        """货币显示符号."""
        if self is Currency.USD:
            return "$"
        # CNY 及未来扩展币种
        return "\u00a5"   # ¥ (U+00A5)

    @classmethod
    def default(cls) -> "Currency":
        """默认币种（向后兼容：无前缀视为 USD）."""
        return cls.USD


# 模块级常量：币种 → 符号映射（供外部查询使用）
_CURRENCY_SYMBOL_MAP: dict[Currency, str] = {
    Currency.USD: "$",
    Currency.CNY: "\u00a5",
}


@dataclass(frozen=True)
class CostValue:
    """带币种标注的费用值（Value Object，不可变）.

    遵循 Value Object 模式：通过 ``(amount, currency)`` 判等，不可变。
    """

    amount: float
    currency: Currency = Currency.default()

    def format(self, precision: int = 4) -> str:
        """格式化为 ``$0.1234`` 或 ``¥0.1234``."""
        return f"{self.currency.symbol}{self.amount:.{precision}f}"

    @property
    def symbol(self) -> str:
        return self.currency.symbol


@dataclass
class ModelPricing:
    """单个模型的 Token 单价（含币种信息）."""

    currency: Currency = Currency.default()
    input_cost_per_token: float = 0.0
    output_cost_per_token: float = 0.0
    cache_creation_input_token_cost: float = 0.0
    cache_read_input_token_cost: float = 0.0
