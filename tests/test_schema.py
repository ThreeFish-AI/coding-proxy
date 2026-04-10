"""schema.py 配置模型语义标注与校验单元测试."""

import logging

from coding.proxy.config.schema import (
    _ANTIGRAVITY_FIELDS,
    _COPILOT_FIELDS,
    _VENDOR_EXCLUSIVE_FIELDS,
    _ZHIPU_FIELDS,
    VendorConfig,
)

# ── 供应商专属字段分组常量 ────────────────────────────────────


def test_copilot_fields_set():
    assert "github_token" in _COPILOT_FIELDS
    assert "account_type" in _COPILOT_FIELDS
    assert "token_url" in _COPILOT_FIELDS
    assert "models_cache_ttl_seconds" in _COPILOT_FIELDS
    assert len(_COPILOT_FIELDS) == 4


def test_antigravity_fields_set():
    assert "client_id" in _ANTIGRAVITY_FIELDS
    assert "client_secret" in _ANTIGRAVITY_FIELDS
    assert "refresh_token" in _ANTIGRAVITY_FIELDS
    assert "model_endpoint" in _ANTIGRAVITY_FIELDS
    assert len(_ANTIGRAVITY_FIELDS) == 4


def test_zhipu_fields_set():
    assert "api_key" in _ZHIPU_FIELDS
    assert len(_ZHIPU_FIELDS) == 1


def test_vendor_exclusive_fields_mapping_complete():
    assert set(_VENDOR_EXCLUSIVE_FIELDS.keys()) == {
        "copilot",
        "antigravity",
        "zhipu",
        "minimax",
        "kimi",
        "doubao",
        "xiaomi",
        "alibaba",
    }


# ── VendorConfig 字段描述标注 ─────────────────────────────────


def test_vendorconfig_copilot_fields_have_description():
    """Copilot 专属字段应包含 [copilot] 前缀的 description."""
    for field_name in _COPILOT_FIELDS:
        field_info = VendorConfig.model_fields[field_name]
        assert "[copilot]" in field_info.description, (
            f"{field_name} 缺少 [copilot] 标注"
        )


def test_vendorconfig_antigravity_fields_have_description():
    """Antigravity 专属字段应包含 [antigravity] 前缀的 description."""
    for field_name in _ANTIGRAVITY_FIELDS:
        field_info = VendorConfig.model_fields[field_name]
        assert "[antigravity]" in field_info.description, (
            f"{field_name} 缺少 [antigravity] 标注"
        )


def test_vendorconfig_zhipu_field_has_description():
    """Zhipu/原生 Anthropic 兼容供应商专属字段应包含 [zhipu/...] 前缀的 description."""
    field_info = VendorConfig.model_fields["api_key"]
    assert "[zhipu/" in field_info.description


def test_vendorconfig_common_fields_have_description():
    """通用字段应有非空 description."""
    for field_name in ("base_url", "timeout_ms"):
        field_info = VendorConfig.model_fields[field_name]
        assert field_info.description, f"{field_name} 缺少 description"


# ── _warn_irrelevant_fields 校验器 ───────────────────────────


def test_warn_irrelevant_fields_copilot_with_antigravity_values(caplog):
    """Copilot vendor 配置了 Antigravity 专属字段时应发出 warning."""
    with caplog.at_level(logging.WARNING, logger="coding.proxy.config.routing"):
        VendorConfig(
            vendor="copilot",
            github_token="ghp_test",
            client_id="should_be_ignored",  # Antigravity 专属
            refresh_token="also_ignored",  # Antigravity 专属
        )
    warnings = [
        r
        for r in caplog.records
        if r.levelno >= logging.WARNING and "将被忽略" in r.message
    ]
    assert len(warnings) >= 2  # client_id 和 refresh_token 各一条 warning


def test_warn_irrelevant_fields_antigravity_with_copilot_values(caplog):
    """Antigravity vendor 配置了 Copilot 专属字段时应发出 warning."""
    with caplog.at_level(logging.WARNING, logger="coding.proxy.config.routing"):
        VendorConfig(
            vendor="antigravity",
            client_id="cid_test",
            github_token="ghp_misplaced",  # Copilot 专属
            account_type="business",  # Copilot 专属
        )
    warnings = [
        r
        for r in caplog.records
        if r.levelno >= logging.WARNING and "将被忽略" in r.message
    ]
    assert len(warnings) >= 2


def test_no_warning_for_correct_fields(caplog):
    """正确配置的字段不应触发 warning."""
    with caplog.at_level(logging.WARNING, logger="coding.proxy.config.routing"):
        VendorConfig(vendor="copilot", github_token="ghp_ok")
    warnings = [
        r
        for r in caplog.records
        if r.levelno >= logging.WARNING and "将被忽略" in r.message
    ]
    assert len(warnings) == 0


def test_no_warning_for_default_values(caplog):
    """使用默认值的非当前 vendor 字段不应触发 warning（空字符串等于默认值）."""
    with caplog.at_level(logging.WARNING, logger="coding.proxy.config.routing"):
        VendorConfig(vendor="anthropic", base_url="https://api.anthropic.com")
    warnings = [
        r
        for r in caplog.records
        if r.levelno >= logging.WARNING and "将被忽略" in r.message
    ]
    assert len(warnings) == 0


def test_anthropic_vendor_skips_validation(caplog):
    """Anthropic vendor 无专属字段，不应触发任何 warning."""
    with caplog.at_level(logging.WARNING, logger="coding.proxy.config.routing"):
        VendorConfig(vendor="anthropic")
    warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert len(warnings) == 0


def test_no_warning_for_native_anthropic_shared_fields(caplog):
    """原生 Anthropic 兼容供应商之间共享的 api_key 字段不应触发 warning."""
    with caplog.at_level(logging.WARNING, logger="coding.proxy.config.routing"):
        VendorConfig(vendor="zhipu", api_key="sk-test-key")
    warnings = [
        r
        for r in caplog.records
        if r.levelno >= logging.WARNING and "将被忽略" in r.message
    ]
    assert len(warnings) == 0


# ── ProxyConfig Legacy 字段标注 ──────────────────────────────


def test_proxyconfig_legacy_fields_have_deprecation_marker():
    """ProxyConfig 的 legacy 字段应包含 [legacy] 标记."""
    from coding.proxy.config.schema import ProxyConfig

    legacy_fields = [
        "primary",
        "copilot",
        "antigravity",
        "fallback",
        "circuit_breaker",
        "copilot_circuit_breaker",
        "antigravity_circuit_breaker",
        "quota_guard",
        "copilot_quota_guard",
        "antigravity_quota_guard",
    ]
    for field_name in legacy_fields:
        field_info = ProxyConfig.model_fields[field_name]
        assert "[legacy]" in field_info.description, f"{field_name} 缺少 [legacy] 标记"


def test_proxyconfig_non_legacy_fields_no_deprecation():
    """非 legacy 字段不应包含 [legacy] 标记."""
    from coding.proxy.config.schema import ProxyConfig

    non_legacy = [
        "server",
        "failover",
        "model_mapping",
        "pricing",
        "tiers",
        "auth",
        "database",
        "logging",
    ]
    for field_name in non_legacy:
        field_info = ProxyConfig.model_fields[field_name]
        assert "[legacy]" not in (field_info.description or ""), (
            f"{field_name} 不应有 [legacy] 标记"
        )
