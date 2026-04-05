"""tiers 降级链路配置项单元测试."""

from pathlib import Path

import pytest

from coding.proxy.config.schema import ProxyConfig


def _load_yaml_config(text: str) -> ProxyConfig:
    """从 YAML 文本加载 ProxyConfig（通过 loader 模块）."""
    import tempfile

    from coding.proxy.config.loader import load_config

    with tempfile.TemporaryDirectory() as tmpdir:
        cfg_file = Path(tmpdir) / "config.yaml"
        cfg_file.write_text(text)
        return load_config(cfg_file)


# ── A 组：基础加载与解析 ──────────────────────────────


def test_tiers_basic_load(tmp_path: Path):
    """YAML 含 tiers 时正确解析."""
    cfg = _load_yaml_config(
        "vendors:\n"
        "  - vendor: anthropic\n"
        "    enabled: true\n"
        "  - vendor: zhipu\n"
        "    api_key: sk-test\n"
        "tiers:\n"
        "  - anthropic\n"
        "  - zhipu\n"
    )
    assert cfg.tiers == ["anthropic", "zhipu"]


def test_tiers_default_none(tmp_path: Path):
    """无 tiers 字段时为 None."""
    cfg = _load_yaml_config("vendors:\n  - vendor: anthropic\n    enabled: true\n")
    assert cfg.tiers is None


def test_tiers_empty_list(tmp_path: Path):
    """空列表合法（表示不启用任何降级链路）."""
    cfg = _load_yaml_config(
        "vendors:\n  - vendor: anthropic\n    enabled: true\ntiers: []\n"
    )
    assert cfg.tiers == []


def test_tiers_single_entry(tmp_path: Path):
    """仅含一项."""
    cfg = _load_yaml_config(
        "vendors:\n  - vendor: zhipu\n    api_key: sk-test\ntiers:\n  - zhipu\n"
    )
    assert cfg.tiers == ["zhipu"]


def test_tiers_all_four_vendors(tmp_path: Path):
    """四个 vendor 全部列出."""
    cfg = _load_yaml_config(
        "vendors:\n"
        "  - vendor: anthropic\n"
        "    enabled: true\n"
        "  - vendor: copilot\n"
        "    enabled: true\n"
        "  - vendor: antigravity\n"
        "    enabled: true\n"
        "  - vendor: zhipu\n"
        "    api_key: sk-test\n"
        "tiers:\n"
        "  - anthropic\n"
        "  - copilot\n"
        "  - antigravity\n"
        "  - zhipu\n"
    )
    assert cfg.tiers == ["anthropic", "copilot", "antigravity", "zhipu"]


# ── B 组：Pydantic 类型校验 ────────────────────────────────


def test_tiers_rejects_invalid_vendor(tmp_path: Path):
    """非法 vendor 值触发 ValidationError."""
    import pydantic

    with pytest.raises(pydantic.ValidationError):
        _load_yaml_config("vendors:\n  - vendor: anthropic\ntiers:\n  - openai\n")


def test_tiers_rejects_wrong_type(tmp_path: Path):
    """非列表类型触发 ValidationError."""
    import pydantic

    with pytest.raises(pydantic.ValidationError):
        _load_yaml_config("vendors:\n  - vendor: anthropic\ntiers: anthropic\n")


# ── C 组：语义校验 ──────────────────────────────────────


def test_tiers_rejects_unknown_vendor(tmp_path: Path):
    """引用不存在的 vendor 触发 ValidationError（Pydantic Literal 校验）."""
    import pydantic

    with pytest.raises(pydantic.ValidationError):
        _load_yaml_config(
            "vendors:\n"
            "  - vendor: anthropic\n"
            "    enabled: true\n"
            "  - vendor: zhipu\n"
            "    api_key: sk-test\n"
            "tiers:\n"
            "  - anthropic\n"
            "  - nonexistent\n"
        )


def test_tiers_rejects_duplicates(tmp_path: Path):
    """重复值触发 ValueError."""
    with pytest.raises(ValueError, match="重复"):
        _load_yaml_config(
            "vendors:\n"
            "  - vendor: anthropic\n"
            "    enabled: true\n"
            "tiers:\n"
            "  - anthropic\n"
            "  - anthropic\n"
        )


def test_tiers_warns_on_disabled_vendor(tmp_path: Path, caplog):
    """引用 disabled 的 vendor 发出 warning."""
    import logging

    with caplog.at_level(logging.WARNING, logger="coding.proxy.config.schema"):
        _load_yaml_config(
            "vendors:\n"
            "  - vendor: anthropic\n"
            "    enabled: true\n"
            "  - vendor: copilot\n"
            "    enabled: false\n"
            "tiers:\n"
            "  - anthropic\n"
            "  - copilot\n"
        )
    warnings = [
        r
        for r in caplog.records
        if r.levelno >= logging.WARNING and "disabled" in r.message
    ]
    assert len(warnings) >= 1


def test_tiers_accepts_enabled_only(tmp_path: Path):
    """全部引用均为 enabled vendor 时正常返回."""
    cfg = _load_yaml_config(
        "vendors:\n"
        "  - vendor: anthropic\n"
        "    enabled: true\n"
        "  - vendor: zhipu\n"
        "    api_key: sk-test\n"
        "tiers:\n"
        "  - anthropic\n"
        "  - zhipu\n"
    )
    assert cfg.tiers == ["anthropic", "zhipu"]
    assert len(cfg.vendors) == 2


# ── D 组：向后兼容 ──────────────────────────────────────


def test_no_tiers_preserves_vendor_order(tmp_path: Path):
    """无 tiers 时 vendors 顺序不变（回退行为）."""
    cfg = _load_yaml_config(
        "vendors:\n"
        "  - vendor: anthropic\n"
        "    enabled: true\n"
        "  - vendor: zhipu\n"
        "    api_key: sk-test\n"
    )
    assert cfg.tiers is None
    assert cfg.vendors[0].vendor == "anthropic"
    assert cfg.vendors[1].vendor == "zhipu"


def test_legacy_format_no_tiers(tmp_path: Path):
    """旧 flat 格式迁移后 tiers 为 None."""
    cfg = _load_yaml_config(
        "primary:\n  enabled: true\nfallback:\n  api_key: sk-legacy\n"
    )
    assert cfg.tiers is None
    # vendors 应由迁移器自动生成
    vendor_names = [v.vendor for v in cfg.vendors]
    assert "anthropic" in vendor_names
    assert "zhipu" in vendor_names


# ── E 组：集成测试 ──────────────────────────────────────


def test_tiers_reorders_vendor_chain(tmp_path: Path):
    """tiers 指定顺序与 vendors 定义顺序不同时，以 tiers 为准."""
    cfg = _load_yaml_config(
        "vendors:\n"
        "  - vendor: anthropic\n"
        "    enabled: true\n"
        "  - vendor: copilot\n"
        "    enabled: true\n"
        "  - vendor: zhipu\n"
        "    api_key: sk-test\n"
        "tiers:\n"
        "  - zhipu\n"
        "  - anthropic\n"
    )
    assert cfg.tiers == ["zhipu", "anthropic"]
