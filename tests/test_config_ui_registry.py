"""ui_registry 覆盖门：每个非 legacy 叶子字段要么在 REGISTRY，要么在 HIDDEN."""

from __future__ import annotations

from coding.proxy.config.secrets import SECRET_FIELDS_BY_VENDOR
from coding.proxy.config.ui_registry import (
    HIDDEN,
    REGISTRY,
    WIDGETS,
    find_spec,
    is_hidden,
    walk_schema,
)


def test_every_leaf_is_registered_or_hidden():
    required, _ = walk_schema()
    registered = {spec.path for spec in REGISTRY}
    missing = sorted(p for p in required if p not in registered and not is_hidden(p))
    assert missing == []


def test_every_registry_path_exists():
    _, all_paths = walk_schema()
    assert sorted(s.path for s in REGISTRY if s.path not in all_paths) == []
    assert len({s.path for s in REGISTRY}) == len(REGISTRY)


def test_hidden_entries_exist():
    _, all_paths = walk_schema()
    from coding.proxy.config.loader import _LEGACY_FLAT_KEYS

    for path in HIDDEN - set(_LEGACY_FLAT_KEYS):
        assert path in all_paths, path


def test_spec_constraints():
    for spec in REGISTRY:
        assert spec.widget in WIDGETS, spec.path
        assert 0 < len(spec.label) <= 14, spec.path
        assert spec.label.isascii(), spec.path
        assert 0 < len(spec.description) <= 120, spec.path
        if spec.widget == "enum":
            assert spec.enum, spec.path


def test_secret_widgets_match_secret_fields():
    secret_specs = {s.path for s in REGISTRY if s.widget == "secret"}
    expected = {
        f"vendors.*.{f}" for fields in SECRET_FIELDS_BY_VENDOR.values() for f in fields
    } | {"auth.google_client_secret"}
    assert secret_specs == expected


def test_bootstrap_fields_are_readonly():
    for spec in REGISTRY:
        section = spec.path.split(".")[0]
        if section in ("server", "logging", "database") or spec.path in (
            "auth.google_client_id",
            "auth.token_store_path",
        ):
            assert spec.readonly, spec.path


def test_find_spec_wildcards():
    assert find_spec("vendors.zhipu.timeout_ms").path == "vendors.*.timeout_ms"
    assert find_spec("model_mapping.*.target") is not None
    assert find_spec("vendors.zhipu.nope") is None
