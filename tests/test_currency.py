"""双币种计费 — Currency / CostValue / 价格解析器 单元测试."""

from __future__ import annotations

import pytest

from coding.proxy.config.routing import _detect_currency, _price_to_float
from coding.proxy.model.pricing import CostValue, Currency, ModelPricing


# ── Currency 枚举 ────────────────────────────────────────────


class TestCurrency:
    """Currency 枚举测试."""

    def test_usd_symbol(self):
        assert Currency.USD.symbol == "$"

    def test_cny_symbol(self):
        assert Currency.CNY.symbol == "\u00a5"   # ¥ (U+00A5)

    def test_default_is_usd(self):
        assert Currency.default() == Currency.USD

    def test_from_string_usd(self):
        assert Currency("USD") == Currency.USD

    def test_from_string_cny(self):
        assert Currency("CNY") == Currency.CNY

    def test_members_count(self):
        assert len(Currency) == 2

    def test_str_enum_behavior(self):
        """StrEnum: str(Currency.USD) 应返回 'USD'."""
        assert str(Currency.USD) == "USD"
        assert str(Currency.CNY) == "CNY"


# ── CostValue 值对象 ────────────────────────────────────────


class TestCostValue:
    """CostValue 值对象测试."""

    def test_format_usd(self):
        cv = CostValue(amount=0.3421, currency=Currency.USD)
        assert cv.format() == "$0.3421"

    def test_format_cny(self):
        cv = CostValue(amount=0.0953, currency=Currency.CNY)
        assert cv.format() == "\u00a50.0953"

    def test_format_precision(self):
        cv = CostValue(amount=0.12345678, currency=Currency.USD)
        assert cv.format(precision=2) == "$0.12"
        assert cv.format(precision=6) == "$0.123457"

    def test_format_zero(self):
        cv = CostValue(amount=0.0, currency=Currency.USD)
        assert cv.format() == "$0.0000"

    def test_equality_same_currency(self):
        a = CostValue(1.0, Currency.USD)
        b = CostValue(1.0, Currency.USD)
        assert a == b

    def test_equality_different_amount(self):
        a = CostValue(1.0, Currency.USD)
        b = CostValue(2.0, Currency.USD)
        assert a != b

    def test_equality_different_currency(self):
        a = CostValue(1.0, Currency.USD)
        b = CostValue(1.0, Currency.CNY)
        assert a != b

    def test_frozen_immutable(self):
        cv = CostValue(1.0, Currency.USD)
        with pytest.raises(AttributeError):
            cv.amount = 2.0   # type: ignore[misc]

    def test_symbol_property(self):
        assert CostValue(1.0, Currency.USD).symbol == "$"
        assert CostValue(1.0, Currency.CNY).symbol == "\u00a5"

    def test_default_currency(self):
        cv = CostValue(amount=5.0)
        assert cv.currency == Currency.USD


# ── ModelPricing currency 字段 ───────────────────────────────


class TestModelPricingCurrency:
    """ModelPricing 新增 currency 字段测试."""

    def test_default_currency_is_usd(self):
        pricing = ModelPricing()
        assert pricing.currency == Currency.USD

    def test_explicit_cny_currency(self):
        pricing = ModelPricing(currency=Currency.CNY, input_cost_per_token=1e-6)
        assert pricing.currency == Currency.CNY

    def test_other_fields_unchanged(self):
        pricing = ModelPricing(
            currency=Currency.CNY,
            input_cost_per_token=3e-6,
            output_cost_per_token=15e-6,
            cache_creation_input_token_cost=1e-6,
            cache_read_input_token_cost=0.5e-6,
        )
        assert pricing.input_cost_per_token == 3e-6
        assert pricing.output_cost_per_token == 15e-6
        assert pricing.cache_creation_input_token_cost == 1e-6
        assert pricing.cache_read_input_token_cost == 0.5e-6


# ── 价格解析器 ───────────────────────────────────────────────


class TestPriceParser:
    """价格字符串解析器测试（$ / ¥ 前缀 → float + 币种检测）."""

    # ── $ 前缀 ──

    def test_dollar_prefix_to_float(self):
        assert _price_to_float("$3.0") == 3.0

    def test_dollar_prefix_detect(self):
        assert _detect_currency("$3.0") == "USD"

    def test_dollar_with_decimal(self):
        assert _price_to_float("$0.50") == 0.5

    # ── ¥ 前缀 ──

    def test_yen_prefix_to_float(self):
        assert _price_to_float("\u00a53.2") == 3.2

    def test_yen_prefix_detect(self):
        assert _detect_currency("\u00a53.2") == "CNY"

    def test_yen_with_decimal(self):
        assert _price_to_float("\u00a50.30") == 0.3

    # ── 纯数字（向后兼容） ──

    def test_plain_float(self):
        assert _price_to_float(5.0) == 5.0
        assert _detect_currency(5.0) is None

    def test_plain_int(self):
        assert _price_to_float(3) == 3.0
        assert _detect_currency(3) is None

    def test_plain_string_number(self):
        assert _price_to_float("5.0") == 5.0
        assert _detect_currency("5.0") is None

    def test_plain_string_int(self):
        assert _price_to_float("3") == 3.0
        assert _detect_currency("3") is None

    # ── 零值 ──

    def test_zero_float_no_currency(self):
        assert _detect_currency(0.0) is None

    def test_zero_int_no_currency(self):
        assert _detect_currency(0) is None

    def test_zero_with_dollar_prefix(self):
        """即使值为 0，有前缀也应检测为 USD."""
        assert _detect_currency("$0") == "USD"

    def test_zero_with_yen_prefix(self):
        assert _detect_currency("\u00a50") == "CNY"

    # ── 空白容差 ──

    def test_whitespace_after_dollar(self):
        assert _price_to_float("$ 3.0") == 3.0
        assert _detect_currency("$ 3.0") == "USD"

    def test_whitespace_after_yen(self):
        assert _price_to_float("\u00a5  3.0") == 3.0
        assert _detect_currency("\u00a5  3.0") == "CNY"

    # ── 边界值 ──

    def test_scientific_notation_string(self):
        assert _price_to_float("1e-6") == 1e-6

    def test_negative_value_parsed(self):
        """解析器层面：负数可被正确解析（校验由 ModelPricingEntry 负责）."""
        assert _price_to_float("$-1.0") == -1.0
        assert _detect_currency("$-1.0") == "USD"
