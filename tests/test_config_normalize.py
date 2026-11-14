"""``$tmpl`` 节点与 section 合并 / 最小化覆盖."""

from __future__ import annotations

from coding.proxy.config.defaults import packaged_defaults_raw
from coding.proxy.config.loader import _deep_merge
from coding.proxy.config.normalize import (
    ABSENT_KEY,
    NO_OVERRIDE,
    ORDER_KEY,
    compose_raw,
    deep_merge,
    denormalize,
    effective_vendors,
    expand_tmpl,
    iter_literal_tmpl_locs,
    minimize_override,
    normalize_raw,
    strip_vendor_secrets,
    whole_env_var,
)


class TestTemplates:
    def test_normalize_and_expand(self):
        raw = {"a": "x-${FOO}-y", "b": ["${BAR}", "plain"], "c": 3}
        norm = normalize_raw(raw)
        assert norm == {
            "a": {"$tmpl": "x-${FOO}-y"},
            "b": [{"$tmpl": "${BAR}"}, "plain"],
            "c": 3,
        }
        assert normalize_raw(norm) == norm
        assert expand_tmpl(norm, {"FOO": "1"}) == {
            "a": "x-1-y",
            "b": ["", "plain"],
            "c": 3,
        }
        assert denormalize(norm) == raw

    def test_literal_template_locations(self):
        doc = {
            "a": {"$tmpl": "${OK}"},
            "b": ["plain", "x${Y}"],
            "c": {"d": "${Z}", "e": 1},
        }
        assert list(iter_literal_tmpl_locs(doc, ["root"])) == [
            ["root", "b", 1],
            ["root", "c", "d"],
        ]
        # 字面量经 YAML 往返后被 normalize 成模板（这正是写入侧拒绝它的原因）
        assert normalize_raw(denormalize(doc))["c"]["d"] == {"$tmpl": "${Z}"}

    def test_whole_env_var(self):
        assert whole_env_var({"$tmpl": "${ZHIPU_API_KEY}"}) == "ZHIPU_API_KEY"
        assert whole_env_var({"$tmpl": "Bearer ${X}"}) is None
        assert whole_env_var("${X}") is None

    def test_defaults_are_normalized(self):
        defaults = packaged_defaults_raw()
        zhipu = next(v for v in defaults["vendors"] if v["vendor"] == "zhipu")
        assert zhipu["api_key"] == {"$tmpl": "${ZHIPU_API_KEY}"}
        defaults["server"]["port"] = 1
        assert packaged_defaults_raw()["server"]["port"] == 3392

    def test_deep_merge_matches_legacy(self):
        a = {"x": {"y": 1, "z": [1]}, "k": 1}
        b = {"x": {"z": [2], "w": {"q": 1}}, "k": None, "n": 2}
        assert deep_merge(a, b) == _deep_merge(a, b)


class TestMinimize:
    def test_dict_section_keeps_only_differences(self):
        default = {"openai": {"enabled": True, "base_url": "https://a"}, "x": [1, 2]}
        effective = {"openai": {"enabled": True, "base_url": "https://b"}, "x": [1, 2]}
        assert minimize_override("native_api", default, effective) == {
            "openai": {"base_url": "https://b"}
        }
        assert minimize_override("native_api", default, default) is NO_OVERRIDE
        # list 整体比较
        assert minimize_override("failover", {"s": [1, 2]}, {"s": [2, 1]}) == {
            "s": [2, 1]
        }
        # 数值等价（int / float）不产生覆盖，bool 与 int 不等价
        assert minimize_override("q", {"h": 24.0}, {"h": 24}) is NO_OVERRIDE
        assert minimize_override("q", {"e": True}, {"e": 1}) == {"e": 1}

    def test_missing_default_dict(self):
        assert minimize_override("database", None, {}) is NO_OVERRIDE
        assert minimize_override("database", None, {"path": "/x"}) == {"path": "/x"}

    def test_list_and_scalar_sections(self):
        rules = [{"pattern": "a", "target": "b"}]
        assert minimize_override("model_mapping", rules, list(rules)) is NO_OVERRIDE
        assert minimize_override("model_mapping", rules, []) == []

    def test_tiers_null_is_an_override(self):
        assert minimize_override("tiers", ["zhipu"], None) is None
        assert minimize_override("tiers", ["zhipu"], ["zhipu"]) is NO_OVERRIDE

    def test_vendors_override_object(self):
        defaults = packaged_defaults_raw()["vendors"]
        default_order = [v["vendor"] for v in defaults]
        effective = [strip_vendor_secrets(v) for v in defaults]
        assert minimize_override("vendors", defaults, effective) is NO_OVERRIDE

        zhipu = next(v for v in effective if v["vendor"] == "zhipu")
        zhipu["timeout_ms"] = 1
        changed = [v for v in effective if v["vendor"] != "minimax"]
        changed.insert(0, changed.pop(changed.index(zhipu)))
        override = minimize_override("vendors", defaults, changed)
        assert set(override) == {"zhipu", ORDER_KEY, ABSENT_KEY}
        assert override["zhipu"]["timeout_ms"] == 1
        assert "api_key" not in override["zhipu"]
        assert override[ABSENT_KEY] == ["minimax"]
        assert override[ORDER_KEY][0] == "zhipu"
        assert len(override[ORDER_KEY]) == len(default_order) - 1

        resolved = effective_vendors(defaults, override)
        assert [v["vendor"] for v in resolved] == override[ORDER_KEY]
        resolved_zhipu = next(v for v in resolved if v["vendor"] == "zhipu")
        assert "api_key" not in resolved_zhipu

    def test_effective_vendors_appends_new_default_types(self):
        defaults = [{"vendor": "a"}, {"vendor": "b"}, {"vendor": "c"}]
        override = {ORDER_KEY: ["b", "a"], "a": {"x": 1}}
        assert [v["vendor"] for v in effective_vendors(defaults, override)] == [
            "b",
            "a",
            "c",
        ]
        assert effective_vendors(defaults, override)[1] == {"x": 1, "vendor": "a"}

    def test_compose_raw(self):
        defaults = packaged_defaults_raw()
        raw = compose_raw(
            defaults,
            {"tiers": None, "server": {"port": 1}, "primary": {"enabled": False}},
        )
        assert raw["tiers"] is None
        assert raw["server"] == {"host": "127.0.0.1", "port": 1}
        assert raw["primary"] == {"enabled": False}
        assert raw["vendors"] == defaults["vendors"]
