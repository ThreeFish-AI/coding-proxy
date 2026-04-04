"""PricingTable 模型定价表测试."""

from __future__ import annotations

import pytest

from coding.proxy.model.pricing import CostValue, Currency
from coding.proxy.pricing import ModelPricing, PricingTable, _normalize


class TestModelNormalization:
    """模型名规范化规则测试."""

    def test_remove_version_suffix(self):
        assert _normalize("claude-sonnet-4-6@20241022") == "claude-sonnet-4-6"

    def test_replace_dot_with_dash(self):
        assert _normalize("glm.4.5.air") == "glm-4-5-air"

    def test_lower_case(self):
        assert _normalize("CLAUDE-Opus-4.6") == "claude-opus-4-6"

    def test_noop_when_already_normalized(self):
        name = "claude-sonnet-4-6"
        assert _normalize(name) == name


class TestModelPricing:
    """ModelPricing 数据类测试."""

    def test_default_zero(self):
        p = ModelPricing()
        assert p.input_cost_per_token == 0.0
        assert p.output_cost_per_token == 0.0

    def test_fields(self):
        p = ModelPricing(
            input_cost_per_token=3e-6,
            output_cost_per_token=15e-6,
            cache_creation_input_token_cost=1.5e-6,
            cache_read_input_token_cost=0.75e-6,
        )
        assert p.input_cost_per_token == 3e-6
        assert p.output_cost_per_token == 15e-6


class TestPricingTable:
    """PricingTable 查询与费用计算测试."""

    def _make_entry(self, backend: str = "copilot", model: str = "test", **kwargs):
        from coding.proxy.config.routing import ModelPricingEntry
        # 字段单位为 USD / 1M tokens，直接传入原始值（如 3 表示 $3/M tokens）
        return ModelPricingEntry(
            backend=backend,
            model=model,
            input_cost_per_mtok=kwargs.get("input", 0),
            output_cost_per_mtok=kwargs.get("output", 0),
            cache_write_cost_per_mtok=kwargs.get("cwrite", 0),
            cache_read_cost_per_mtok=kwargs.get("cread", 0),
        )

    def test_empty_table(self):
        table = PricingTable([])
        assert table.get_pricing("copilot", "any") is None
        assert table.compute_cost("copilot", "any", 100, 200, 0, 0) is None

    def test_exact_match(self):
        table = PricingTable([
            self._make_entry("copilot", "model-a", input=2, output=10),
        ])
        pricing = table.get_pricing("copilot", "model-a")
        assert pricing is not None
        assert pricing.input_cost_per_token == 2e-6
        assert pricing.output_cost_per_token == 10e-6

    def test_normalized_match(self):
        """规范化匹配：'glm.4.5-air' → 'glm-4-5-air'."""
        table = PricingTable([
            self._make_entry("antigravity", "glm.4.5.air", input=1, output=5),
        ])
        # 精确不命中（含点号）
        assert table.get_pricing("antigravity", "glm.4.5.air") is not None
        # 规范化命中
        assert table.get_pricing("antigravity", "glm-4-5-air") is not None

    def test_compute_cost(self):
        table = PricingTable([
            self._make_entry("copilot", "m", input=3, output=15, cwrite=1, cread=2),
        ])
        cost_value = table.compute_cost("copilot", "m", 1000, 2000, 50, 100)
        assert cost_value is not None
        assert isinstance(cost_value, CostValue)
        # input=3 → 3e-6/token, output=15 → 15e-6/token, cwrite=1 → 1e-6, cread=2 → 2e-6
        expected = 1000 * 3e-6 + 2000 * 15e-6 + 50 * 1e-6 + 100 * 2e-6
        assert abs(cost_value.amount - expected) < 1e-12

    def test_compute_cost_miss_returns_none(self):
        table = PricingTable([self._make_entry("copilot", "m", input=1, output=2)])
        assert table.compute_cost("copilot", "unknown", 100, 200, 0, 0) is None

    def test_multiple_entries_same_backend(self):
        """同一 backend 下多模型独立定价."""
        table = PricingTable([
            self._make_entry("copilot", "cheap", input=1, output=1),
            self._make_entry("copilot", "expensive", input=10, output=50),
        ])
        cheap = table.get_pricing("copilot", "cheap")
        expensive = table.get_pricing("copilot", "expensive")
        assert cheap.input_cost_per_token < expensive.input_cost_per_token


class TestPricingTableCurrency:
    """PricingTable 双币种（USD/CNY）计费测试."""

    def _make_usd_entry(self, **kwargs):
        from coding.proxy.config.routing import ModelPricingEntry
        return ModelPricingEntry(
            backend="anthropic",
            model="claude-test",
            input_cost_per_mtok=kwargs.get("input", "$3.0"),
            output_cost_per_mtok=kwargs.get("output", "$15.0"),
            cache_write_cost_per_mtok=kwargs.get("cwrite", "$1.0"),
            cache_read_cost_per_mtok=kwargs.get("cread", "$0.30"),
        )

    def _make_cny_entry(self, **kwargs):
        from coding.proxy.config.routing import ModelPricingEntry
        return ModelPricingEntry(
            backend="zhipu",
            model="glm-test",
            input_cost_per_mtok=kwargs.get("input", "\u00a51.0"),
            output_cost_per_mtok=kwargs.get("output", "\u00a53.2"),
            cache_write_cost_per_mtok=kwargs.get("cwrite", "\u00a50.5"),
            cache_read_cost_per_mtok=kwargs.get("cread", "\u00a50.10"),
        )

    def test_compute_cost_returns_costvalue_usd(self):
        """USD 定价的 compute_cost 应返回带 USD 的 CostValue."""
        table = PricingTable([self._make_usd_entry()])
        result = table.compute_cost("anthropic", "claude-test", 1000, 2000, 0, 0)
        assert result is not None
        assert isinstance(result, CostValue)
        assert result.currency == Currency.USD

    def test_compute_cost_returns_costvalue_cny(self):
        """CNY 定价的 compute_cost 应返回带 CNY 的 CostValue."""
        table = PricingTable([self._make_cny_entry()])
        result = table.compute_cost("zhipu", "glm-test", 1000, 2000, 0, 0)
        assert result is not None
        assert isinstance(result, CostValue)
        assert result.currency == Currency.CNY

    def test_compute_cost_amount_correct_usd(self):
        """USD 定价金额计算正确."""
        table = PricingTable([self._make_usd_entry(input="$3.0", output="$15.0")])
        result = table.compute_cost("anthropic", "claude-test", 1000, 2000, 0, 0)
        assert result is not None
        expected = 1000 * 3e-6 + 2000 * 15e-6
        assert abs(result.amount - expected) < 1e-12

    def test_compute_cost_amount_correct_cny(self):
        """CNY 定价金额计算正确."""
        table = PricingTable([self._make_cny_entry(input="\u00a51.0", output="\u00a53.2")])
        result = table.compute_cost("zhipu", "glm-test", 1000, 2000, 0, 0)
        assert result is not None
        expected = 1000 * 1e-6 + 2000 * 3.2e-6
        assert abs(result.amount - expected) < 1e-12

    def test_compute_cost_format_usd(self):
        """USD CostValue.format() 输出应包含 $ 符号."""
        table = PricingTable([self._make_usd_entry()])
        result = table.compute_cost("anthropic", "claude-test", 1000, 2000, 0, 0)
        assert result is not None
        assert result.format().startswith("$")

    def test_compute_cost_format_cny(self):
        """CNY CostValue.format() 输出应包含 ¥ 符号."""
        table = PricingTable([self._make_cny_entry()])
        result = table.compute_cost("zhipu", "glm-test", 1000, 2000, 0, 0)
        assert result is not None
        assert result.format().startswith("\u00a5")

    def test_backward_compatible_plain_number(self):
        """不带前缀的纯数字应默认为 USD（向后兼容）."""
        from coding.proxy.config.routing import ModelPricingEntry
        entry = ModelPricingEntry(
            backend="copilot", model="legacy",
            input_cost_per_mtok=3.0,
            output_cost_per_mtok=15.0,
        )
        table = PricingTable([entry])
        result = table.compute_cost("copilot", "legacy", 1000, 0, 0, 0)
        assert result is not None
        assert result.currency == Currency.USD
        assert result.format().startswith("$")

    def test_get_pricing_carries_currency(self):
        """get_pricing 返回的 ModelPricing 应携带正确的 currency."""
        table = PricingTable([self._make_cny_entry()])
        pricing = table.get_pricing("zhipu", "glm-test")
        assert pricing is not None
        assert pricing.currency == Currency.CNY

    def test_mixed_currency_rejected(self):
        """同一 entry 内混用 $ 和 ¥ 应抛出 ValidationError."""
        from coding.proxy.config.routing import ModelPricingEntry
        with pytest.raises(Exception):   # pydantic.ValidationError
            ModelPricingEntry(
                backend="test", model="mixed",
                input_cost_per_mtok="$3.0",
                output_cost_per_mtok="\u00a35.0",
            )

    def test_negative_price_rejected(self):
        """负数价格应抛出 ValidationError."""
        from coding.proxy.config.routing import ModelPricingEntry
        with pytest.raises(Exception):   # pydantic.ValidationError
            ModelPricingEntry(
                backend="test", model="neg",
                input_cost_per_mtok="$-3.0",
                output_cost_per_mtok="$15.0",
            )

    def test_private_attr_not_in_dump(self):
        """_currency 不应出现在 model_dump 序列化输出中."""
        from coding.proxy.config.routing import ModelPricingEntry
        entry = ModelPricingEntry(backend="t", model="m", input_cost_per_mtok="$3.0")
        dump = entry.model_dump()
        assert "__currency__" not in dump
        assert "_currency" not in dump
        assert "currency" not in dump  # currency 是 property，非字段

    def test_all_zero_no_currency_error(self):
        """所有价格字段为零时不应触发币种校验错误."""
        from coding.proxy.config.routing import ModelPricingEntry
        entry = ModelPricingEntry(backend="t", model="m")
        assert entry.currency == "USD"   # 默认值
