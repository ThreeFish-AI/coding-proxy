"""模型映射器单元测试."""

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
