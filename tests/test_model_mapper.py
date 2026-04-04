"""模型映射器单元测试."""

import logging

from coding.proxy.config.schema import ModelMappingRule
from coding.proxy.routing.model_mapper import ModelMapper


def _make_mapper(rules: list[ModelMappingRule] | None = None) -> ModelMapper:
    if rules is None:
        rules = [
            ModelMappingRule(pattern="claude-sonnet-4-20250514", target="glm-exact"),
            ModelMappingRule(pattern="claude-sonnet-.*", target="glm-5.1", is_regex=True),
            ModelMappingRule(pattern="claude-opus-.*", target="glm-5.1", is_regex=True),
            ModelMappingRule(pattern="claude-haiku-.*", target="glm-4.5-air", is_regex=True),
        ]
    return ModelMapper(rules)


def test_exact_match_takes_priority():
    mapper = _make_mapper()
    assert mapper.map("claude-sonnet-4-20250514") == "glm-exact"


def test_regex_pattern_match():
    mapper = _make_mapper()
    assert mapper.map("claude-sonnet-4-latest") == "glm-5.1"
    assert mapper.map("claude-opus-4-20250514") == "glm-5.1"
    assert mapper.map("claude-haiku-3-latest") == "glm-4.5-air"


def test_default_fallback():
    mapper = _make_mapper()
    assert mapper.map("unknown-model") == "glm-5.1"


def test_glob_pattern():
    """非 is_regex 的通配符使用 fnmatch."""
    rules = [ModelMappingRule(pattern="gpt-4*", target="glm-mapped")]
    mapper = _make_mapper(rules)
    assert mapper.map("gpt-4o") == "glm-mapped"
    assert mapper.map("gpt-4-turbo") == "glm-mapped"


def test_regex_fullmatch():
    """is_regex 使用 fullmatch，不做部分匹配."""
    rules = [ModelMappingRule(pattern="claude-.*", target="glm-5.1", is_regex=True)]
    mapper = _make_mapper(rules)
    assert mapper.map("claude-sonnet") == "glm-5.1"
    # 不匹配前缀不完整的情况
    assert mapper.map("x-claude-sonnet") == "glm-5.1"  # default fallback


def test_empty_rules_use_default():
    mapper = _make_mapper([])
    assert mapper.map("any-model") == "glm-5.1"


def test_vendor_scoped_mapping():
    mapper = _make_mapper([
        ModelMappingRule(pattern="claude-sonnet-*", target="claude-sonnet-4-6-thinking", vendors=["antigravity"]),
        ModelMappingRule(pattern="claude-sonnet-*", target="glm-5.1", vendors=["fallback"]),
    ])
    assert mapper.map("claude-sonnet-4-20250514", vendor="antigravity") == "claude-sonnet-4-6-thinking"
    assert mapper.map("claude-sonnet-4-20250514", vendor="zhipu") == "glm-5.1"


def test_legacy_rule_only_applies_to_fallback():
    mapper = _make_mapper([
        ModelMappingRule(pattern="claude-sonnet-*", target="glm-5.1"),
    ])
    assert mapper.map("claude-sonnet-4-20250514", vendor="fallback") == "glm-5.1"
    assert mapper.map(
        "claude-sonnet-4-20250514",
        vendor="antigravity",
        default="claude-sonnet-4-20250514",
    ) == "claude-sonnet-4-20250514"


def test_zhipu_vendor_logs_with_original_name(caplog):
    """zhipu 供应商传入时，日志应显示 vendor=zhipu 而非 vendor=fallback."""
    caplog.set_level(logging.DEBUG, logger="coding.proxy.routing.model_mapper")
    mapper = _make_mapper([
        ModelMappingRule(pattern="claude-sonnet-*", target="glm-5.1"),
    ])
    result = mapper.map("claude-sonnet-4-20250514", vendor="zhipu")
    assert result == "glm-5.1"

    # 日志中应包含 vendor=zhipu 而非 vendor=fallback
    assert any("vendor=zhipu" in r.message for r in caplog.records)
    assert not any("vendor=fallback" in r.message for r in caplog.records)
