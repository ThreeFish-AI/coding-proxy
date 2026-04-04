"""PricingTable 模型定价表测试."""

from __future__ import annotations

import pytest

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
        cost = table.compute_cost("copilot", "m", 1000, 2000, 50, 100)
        assert cost is not None
        # input=3 → 3e-6/token, output=15 → 15e-6/token, cwrite=1 → 1e-6, cread=2 → 2e-6
        expected = 1000 * 3e-6 + 2000 * 15e-6 + 50 * 1e-6 + 100 * 2e-6
        assert abs(cost - expected) < 1e-12

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
