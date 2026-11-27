"""内置 vendor 模型目录与 Model Bind vendor.model 锁定语义测试.

覆盖 :mod:`coding.proxy.vendors.model_catalog` 的合成/identity 规则、
vendor ``supports_model`` 判定矩阵、executor 严格链与模型门控、
ModelMapper identity 命中优先级。
"""

from __future__ import annotations

import pytest

from coding.proxy.config.routing import ModelMappingRule
from coding.proxy.config.session_policy import BindableModel
from coding.proxy.config.vendors import AnthropicConfig, ZhipuConfig
from coding.proxy.routing.model_mapper import ModelMapper
from coding.proxy.vendors.anthropic import AnthropicVendor
from coding.proxy.vendors.base import FailoverConfig
from coding.proxy.vendors.model_catalog import (
    BUILTIN_CATALOG,
    builtin_entries_for,
    builtin_identity_rules,
    compose_bindable_catalog,
)
from coding.proxy.vendors.zhipu import ZhipuVendor

# ── compose_bindable_catalog ────────────────────────────────


def _cfg(label: str, model: str, tiers: list[str] | None = None, **kw) -> BindableModel:
    return BindableModel(label=label, model=model, tiers=tiers or [], **kw)


class TestComposeCatalog:
    def test_empty_config_disables_binding(self):
        """config 清单为空 = 整体关闭（含内置目录），保留 off 开关语义."""
        assert compose_bindable_catalog(["zhipu", "anthropic"], []) == []

    def test_builtin_filtered_by_active_vendors(self):
        cat = compose_bindable_catalog(["zhipu"], [_cfg("A", "glm-5.3")])
        models = {e["model"] for e in cat}
        assert "glm-5.3" in models
        assert all(not m.startswith("claude-") for m in models)

    def test_config_entry_overrides_label_and_tiers(self):
        cat = compose_bindable_catalog(
            ["zhipu"], [_cfg("我的 GLM", "glm-5.3", ["zhipu"])]
        )
        glm = next(e for e in cat if e["model"] == "glm-5.3")
        assert glm["label"] == "我的 GLM"
        assert glm["tiers"] == ["zhipu"]

    def test_config_override_empty_tiers_inherits_builtin_lock(self):
        """config 同名条目 tiers 留空 → 继承内置目录的锁定 tiers."""
        cat = compose_bindable_catalog(["zhipu"], [_cfg("GLM", "glm-5.3")])
        glm = next(e for e in cat if e["model"] == "glm-5.3")
        assert glm["tiers"] == ["zhipu"]

    def test_exclude_removes_builtin_entry(self):
        cat = compose_bindable_catalog(
            ["zhipu"], [_cfg("X", "glm-4.5-air", exclude=True)]
        )
        assert all(e["model"] != "glm-4.5-air" for e in cat)

    def test_custom_entry_kept_with_vendor_custom(self):
        cat = compose_bindable_catalog(["zhipu"], [_cfg("Opus", "claude-opus-4.6")])
        custom = next(e for e in cat if e["model"] == "claude-opus-4.6")
        assert custom["vendor"] == "custom"
        assert custom["tiers"] == []

    def test_same_model_deduped_between_builtin_and_config(self):
        cat = compose_bindable_catalog(["zhipu"], [_cfg("GLM 5.3", "glm-5.3")])
        assert [e["model"] for e in cat].count("glm-5.3") == 1

    def test_label_collision_falls_back_to_model_name(self):
        # config 用内置 label 指向不同 model → 内置同名 label 条目降级
        cat = compose_bindable_catalog(
            ["zhipu"],
            [_cfg("GLM 5.3", "glm-my-custom")],
        )
        labels = [e["label"] for e in cat]
        # 内置 GLM 5.3 label 已被自定义条目占用，glm-5.3 条目降级用 model 名
        assert labels.count("GLM 5.3") == 1
        assert "glm-5.3" in labels

    def test_exclude_on_non_builtin_model_is_noop(self):
        cat = compose_bindable_catalog(
            ["zhipu"], [_cfg("X", "not-a-builtin", exclude=True)]
        )
        assert any(e["model"] == "not-a-builtin" for e in cat)

    def test_builtin_catalog_labels_and_models_unique(self):
        for vendor, entries in BUILTIN_CATALOG.items():
            labels = [e.label for e in entries]
            models = [e.model for e in entries]
            assert len(labels) == len(set(labels)), vendor
            assert len(models) == len(set(models)), vendor

    def test_builtin_entries_pin_owner_vendor(self):
        for entry in builtin_entries_for("zhipu") + builtin_entries_for("anthropic"):
            assert entry.tiers == (entry.vendor,)

    def test_zhipu_glm_5_4_in_builtin_catalog(self):
        """回归：pricing 与调用记录不是目录来源，GLM 5.4 须随内置目录进下拉."""
        cat = compose_bindable_catalog(["zhipu"], [_cfg("A", "glm-5.3")])
        glm = next(e for e in cat if e["model"] == "glm-5.4")
        assert (glm["label"], glm["vendor"], glm["tiers"]) == (
            "GLM 5.4",
            "zhipu",
            ["zhipu"],
        )


# ── builtin_identity_rules ──────────────────────────────────


class TestIdentityRules:
    def test_native_vendor_models_get_identity_rules(self):
        cat = compose_bindable_catalog(["zhipu", "anthropic"], [_cfg("A", "glm-5.3")])
        rules = builtin_identity_rules(cat)
        zhipu_rules = {r.pattern for r in rules if r.vendors == ["zhipu"]}
        assert "glm-5.3" in zhipu_rules
        assert all(r.pattern == r.target and not r.is_regex for r in rules)

    def test_glm_5_4_gets_identity_rule(self):
        """内置目录里的 GLM 5.4 发往 zhipu 时 exact 命中原名，不被 default 回退改写."""
        cat = compose_bindable_catalog(["zhipu"], [_cfg("A", "glm-5.3")])
        rules = builtin_identity_rules(cat)
        assert any(r.pattern == "glm-5.4" and r.vendors == ["zhipu"] for r in rules)

    def test_anthropic_entries_get_no_rules(self):
        """anthropic 恒等透传，不进 ModelMapper 规则空间."""
        cat = compose_bindable_catalog(["anthropic"], [_cfg("A", "claude-opus-4-6")])
        assert builtin_identity_rules(cat) == []

    def test_custom_entry_tiers_get_rule(self):
        cat = compose_bindable_catalog(["zhipu"], [_cfg("M", "glm-9.9", ["zhipu"])])
        rules = builtin_identity_rules(cat)
        assert any(r.pattern == "glm-9.9" and r.vendors == ["zhipu"] for r in rules)

    def test_custom_entry_empty_tiers_prefix_fallback(self):
        """custom 条目 tiers 为空时按前缀族兜底归属."""
        cat = compose_bindable_catalog(["zhipu"], [_cfg("M", "glm-9.9")])
        rules = builtin_identity_rules(cat)
        assert any(r.pattern == "glm-9.9" and r.vendors == ["zhipu"] for r in rules)

    def test_user_explicit_rule_wins_over_identity(self):
        """ModelMapper exact 优先级：用户显式规则先命中."""
        user = [
            ModelMappingRule(pattern="glm-5.3", target="glm-5.1", vendors=["zhipu"])
        ]
        identity = [
            ModelMappingRule(
                pattern="glm-5.3", target="glm-5.3", is_regex=False, vendors=["zhipu"]
            )
        ]
        mapper = ModelMapper([*user, *identity])
        assert mapper.map_with_reason("glm-5.3", vendor="zhipu") == (
            "glm-5.1",
            "exact",
        )


# ── supports_model 判定矩阵 ─────────────────────────────────


def _zhipu_vendor() -> ZhipuVendor:
    mapper = ModelMapper(
        [
            *builtin_identity_rules(
                compose_bindable_catalog(["zhipu"], [_cfg("A", "glm-5.3")])
            ),
            ModelMappingRule(
                pattern="claude-sonnet-.*",
                target="glm-5.1",
                is_regex=True,
                vendors=["zhipu"],
            ),
        ]
    )
    return ZhipuVendor(ZhipuConfig(api_key="k"), mapper)


def _anthropic_vendor() -> AnthropicVendor:
    return AnthropicVendor(AnthropicConfig(), FailoverConfig())


class TestSupportsModel:
    def test_anthropic_supports_own_family(self):
        v = _anthropic_vendor()
        assert v.supports_model("claude-opus-4-6") is True

    def test_anthropic_rejects_foreign_native(self):
        v = _anthropic_vendor()
        assert v.supports_model("glm-5.3") is False
        assert v.supports_model("qwen3.6-plus") is False

    def test_anthropic_unknown_model_passthrough(self):
        v = _anthropic_vendor()
        assert v.supports_model("my-private-finetune") is True

    def test_zhipu_supports_catalog_model_via_identity(self):
        assert _zhipu_vendor().supports_model("glm-5.3") is True

    def test_zhipu_supports_mapped_claude_via_pattern(self):
        assert _zhipu_vendor().supports_model("claude-sonnet-4-6") is True

    def test_native_rejects_foreign_native(self):
        """zhipu 对他族原生名（如 minimax）不支持（无 exact/pattern 命中）."""
        v = _zhipu_vendor()
        assert v.supports_model("minimax-m2.7") is False


# ── executor 严格链与模型门控 ────────────────────────────────


@pytest.mark.asyncio
async def test_gate_skips_tier_not_supporting_bound_model():
    """绑定模型不被该 tier vendor 支持时 skip 并记因（模型门控）."""
    from coding.proxy.compat.canonical import build_canonical_request
    from coding.proxy.vendors.base import RequestCapabilities
    from tests.test_router_executor import (
        _executor,
        _make_tier,
        _mock_vendor,
    )

    vendor = _mock_vendor(name="anthropic")
    vendor.supports_model.side_effect = lambda m: m.startswith("claude-")
    tier = _make_tier(vendor)
    exec_inst = _executor([tier])
    req = build_canonical_request({"model": "glm-5.3"}, {})
    session_record, _ = await exec_inst._session_mgr.get_or_create_record(
        req.session_key, req.trace_id
    )
    reasons: list[str] = []
    result = await exec_inst._try_gate_tier(
        tier,
        is_last=True,
        request_caps=RequestCapabilities(),
        canonical_request=req,
        session_record=session_record,
        incompatible_reasons=reasons,
        bound_model="glm-5.3",
    )
    assert result == "skip"
    assert reasons == ["anthropic:model_not_supported"]


@pytest.mark.asyncio
async def test_gate_passes_when_bound_model_supported_or_unset():
    from coding.proxy.compat.canonical import build_canonical_request
    from coding.proxy.vendors.base import RequestCapabilities
    from tests.test_router_executor import (
        _executor,
        _make_tier,
        _mock_vendor,
    )

    vendor = _mock_vendor(name="anthropic")
    vendor.supports_model.side_effect = lambda m: m.startswith("claude-")
    tier = _make_tier(vendor)
    exec_inst = _executor([tier])
    req = build_canonical_request({"model": "x"}, {})
    session_record, _ = await exec_inst._session_mgr.get_or_create_record(
        req.session_key, req.trace_id
    )
    for bound in ("claude-opus-4-6", None):
        result = await exec_inst._try_gate_tier(
            tier,
            is_last=True,
            request_caps=RequestCapabilities(),
            canonical_request=req,
            session_record=session_record,
            incompatible_reasons=[],
            bound_model=bound,
        )
        assert result == "eligible"


def test_pinned_model_strict_chain_without_tail_append():
    """绑定模型带锁定 tiers 时严格链：不尾部追加未提及 vendor."""
    from unittest.mock import MagicMock

    from tests.test_router_executor import _executor, _make_tier, _mock_vendor

    zhipu_tier = _make_tier(_mock_vendor(name="zhipu"))
    anthropic_tier = _make_tier(_mock_vendor(name="anthropic"))
    router = MagicMock()
    router.get_bindable_models.return_value = [
        {"label": "GLM 5.3", "model": "glm-5.3", "tiers": ["zhipu"], "vendor": "zhipu"}
    ]
    exec_inst = _executor([zhipu_tier, anthropic_tier], router=router)
    from coding.proxy.config.session_policy import (
        SessionPolicy,
        SessionPolicyMatch,
    )

    policy = SessionPolicy(
        name="runtime:s",
        match=SessionPolicyMatch(session_keys=["s"]),
        tiers=["zhipu"],
        model="glm-5.3",
    )
    effective = exec_inst._resolve_effective_tiers("s", policy=policy)
    assert [t.name for t in effective] == ["zhipu"]


def test_unpinned_model_keeps_reorder_with_tail():
    """无锁定（custom 条目 tiers 空）时维持既有「置头 + 垫尾」语义."""
    from unittest.mock import MagicMock

    from coding.proxy.config.session_policy import (
        SessionPolicy,
        SessionPolicyMatch,
    )
    from tests.test_router_executor import _executor, _make_tier, _mock_vendor

    zhipu_tier = _make_tier(_mock_vendor(name="zhipu"))
    anthropic_tier = _make_tier(_mock_vendor(name="anthropic"))
    router = MagicMock()
    router.get_bindable_models.return_value = [
        {"label": "C", "model": "claude-opus-4.6", "tiers": [], "vendor": "custom"}
    ]
    exec_inst = _executor([zhipu_tier, anthropic_tier], router=router)
    policy = SessionPolicy(
        name="runtime:s",
        match=SessionPolicyMatch(session_keys=["s"]),
        tiers=["anthropic"],
        model="claude-opus-4.6",
    )
    effective = exec_inst._resolve_effective_tiers("s", policy=policy)
    assert [t.name for t in effective] == ["anthropic", "zhipu"]


def test_pinned_model_honors_explicit_vendor_order():
    """锁定模型 + 用户显式点名多 vendor：按用户顺序，仍不垫尾."""
    from unittest.mock import MagicMock

    from coding.proxy.config.session_policy import (
        SessionPolicy,
        SessionPolicyMatch,
    )
    from tests.test_router_executor import _executor, _make_tier, _mock_vendor

    zhipu_tier = _make_tier(_mock_vendor(name="zhipu"))
    anthropic_tier = _make_tier(_mock_vendor(name="anthropic"))
    copilot_tier = _make_tier(_mock_vendor(name="copilot"))
    router = MagicMock()
    router.get_bindable_models.return_value = [
        {"label": "GLM 5.3", "model": "glm-5.3", "tiers": ["zhipu"], "vendor": "zhipu"}
    ]
    exec_inst = _executor([zhipu_tier, anthropic_tier, copilot_tier], router=router)
    policy = SessionPolicy(
        name="runtime:s",
        match=SessionPolicyMatch(session_keys=["s"]),
        tiers=["copilot", "zhipu"],
        model="glm-5.3",
    )
    effective = exec_inst._resolve_effective_tiers("s", policy=policy)
    assert [t.name for t in effective] == ["copilot", "zhipu"]


# ── 评审修复守护（对抗性 review 确认项） ─────────────────────


class TestReviewFixes:
    def test_identity_rule_skips_user_covered_model(self):
        """用户通配/正则规则已覆盖的模型不再合成 identity exact 规则（防遮蔽）."""
        catalog = compose_bindable_catalog(["zhipu"], [_cfg("A", "glm-5.3")])
        user = [
            ModelMappingRule(
                pattern="glm-5.*", target="glm-4.5", is_regex=True, vendors=["zhipu"]
            )
        ]
        rules = builtin_identity_rules(catalog, user)
        assert not any(r.pattern == "glm-5.3" for r in rules)
        # 用户规则保持生效
        mapper = ModelMapper([*user, *rules])
        assert mapper.map_with_reason("glm-5.3", vendor="zhipu") == (
            "glm-4.5",
            "pattern",
        )

    def test_identity_rule_synthesized_when_user_rule_absent(self):
        catalog = compose_bindable_catalog(["zhipu"], [_cfg("A", "glm-5.3")])
        rules = builtin_identity_rules(catalog, [])
        assert any(r.pattern == "glm-5.3" for r in rules)

    def test_exclude_entry_label_not_in_conflict_map(self):
        """exclude 剔除的条目本身不进目录，其 label 不参与内置降级判定."""
        cat = compose_bindable_catalog(
            ["zhipu"],
            [
                _cfg("GLM 5.2", "glm-5.3", exclude=True),
            ],
        )
        # glm-5.3 被 exclude 剔除；无关的内置 GLM 5.2 条目 label 不被降级
        glm52 = next(e for e in cat if e["model"] == "glm-5.2")
        assert glm52["label"] == "GLM 5.2"

    def test_degraded_fallback_label_second_collision_gets_vendor_suffix(self):
        """降级兜底名再撞 config label 时附 vendor 后缀保证可区分."""
        cat = compose_bindable_catalog(
            ["zhipu"],
            [
                _cfg("GLM 5.3", "my-glm"),
                _cfg("glm-5.3", "other-model"),
            ],
        )
        labels = [e["label"] for e in cat]
        assert len(labels) == len(set(labels)), labels

    def test_native_vendor_unknown_model_not_supported(self):
        """native 系对未知名（不在任何前缀族）不放行——default 是静默改写."""
        assert _zhipu_vendor().supports_model("my-private-ft") is False

    def test_native_vendor_own_prefix_supported_without_rule(self):
        """本族前缀名（无显式规则）放行，identity 由目录合成兜底."""
        assert _zhipu_vendor().supports_model("glm-9.9") is True

    def test_anthropic_unknown_model_still_passthrough(self):
        """真透传 vendor（anthropic）未知名仍宽容放行."""
        assert _anthropic_vendor().supports_model("my-private-ft") is True

    def test_copilot_explicit_mapping_hit_supported(self):
        """copilot 作用域显式映射命中的模型（含他族名）门控放行."""
        from coding.proxy.config.vendors import CopilotConfig
        from coding.proxy.vendors.copilot import CopilotVendor

        mapper = ModelMapper(
            [
                ModelMappingRule(
                    pattern="glm-5.3", target="claude-sonnet-4-5", vendors=["copilot"]
                )
            ]
        )
        vendor = CopilotVendor(CopilotConfig(), FailoverConfig(), model_mapper=mapper)
        assert vendor.supports_model("glm-5.3") is True
        # 他族名无显式规则仍不支持
        assert vendor.supports_model("glm-5.2") is False


def test_empty_pinned_chain_error_message():
    """锁定链为空的终态错误：流式/非流式同型 NoCompatibleVendorError，文案指向真因."""
    from coding.proxy.routing.executor import _no_compatible_message

    msg = _no_compatible_message([], "glm-5.3")
    assert "glm-5.3" in msg
    assert "锁定" in msg
    assert _no_compatible_message(["x:model_not_supported"], None).startswith(
        "当前请求包含"
    )
    assert _no_compatible_message([], None) == "无可用供应商层级"
