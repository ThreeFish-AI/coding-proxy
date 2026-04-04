"""ModelPricing (coding.proxy.model.pricing) 单元测试."""

from dataclasses import fields

from coding.proxy.model.pricing import Currency, ModelPricing


class TestModelPricing:
    """定价数据类验证."""


def test_default_values_all_zero():
    """默认构造时所有价格字段应为 0.0."""
    pricing = ModelPricing()
    assert pricing.input_cost_per_token == 0.0
    assert pricing.output_cost_per_token == 0.0
    assert pricing.cache_creation_input_token_cost == 0.0
    assert pricing.cache_read_input_token_cost == 0.0


def test_custom_pricing_values():
    """自定义价格值应正确赋值."""
    pricing = ModelPricing(
        input_cost_per_token=3e-6,
        output_cost_per_token=15e-6,
        cache_creation_input_token_cost=1.875e-6,
        cache_read_input_token_cost=0.15e-6,
    )
    assert pricing.input_cost_per_token == 3e-6
    assert pricing.output_cost_per_token == 15e-6
    assert pricing.cache_creation_input_token_cost == 1.875e-6
    assert pricing.cache_read_input_token_cost == 0.15e-6


def test_field_completeness():
    """数据类应恰好包含声明的五个字段（含 currency）."""
    field_names = {f.name for f in fields(ModelPricing)}
    assert field_names == {
        "currency",
        "input_cost_per_token",
        "output_cost_per_token",
        "cache_creation_input_token_cost",
        "cache_read_input_token_cost",
    }


def test_price_field_types_are_float():
    """价格字段的类型注解应为 float（字符串形式，因 from __future__ import annotations）."""
    price_fields = {
        "input_cost_per_token",
        "output_cost_per_token",
        "cache_creation_input_token_cost",
        "cache_read_input_token_cost",
    }
    for f in fields(ModelPricing):
        if f.name in price_fields:
            assert f.type == "float", f"字段 {f.name} 的类型注解应为 'float'，实际为 {f.type!r}"


def test_equality():
    """相同字段值的两个实例应相等，不同则不等."""
    a = ModelPricing(input_cost_per_token=1.0, output_cost_per_token=2.0)
    b = ModelPricing(input_cost_per_token=1.0, output_cost_per_token=2.0)
    c = ModelPricing(input_cost_per_token=1.0, output_cost_per_token=3.0)

    assert a == b
    assert a != c


def test_default_currency_is_usd():
    """默认构造时 currency 应为 USD."""
    pricing = ModelPricing()
    assert pricing.currency == Currency.USD


def test_explicit_cny_currency():
    """显式指定 CNY 币种应正确赋值."""
    pricing = ModelPricing(currency=Currency.CNY, input_cost_per_token=1e-6)
    assert pricing.currency == Currency.CNY
