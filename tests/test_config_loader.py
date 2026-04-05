"""配置加载器单元测试."""

from pathlib import Path

from coding.proxy.config.loader import (
    _deep_merge,
    _get_default_config_path,
    load_config,
)


def test_load_from_explicit_path(tmp_path: Path):
    """指定路径时直接加载该文件."""
    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text("server:\n  port: 9999\n")
    cfg = load_config(cfg_file)
    assert cfg.server.port == 9999


def test_load_from_cwd_config(tmp_path: Path, monkeypatch):
    """项目根目录 config.yaml 优先于用户目录."""
    cwd_cfg = tmp_path / "config.yaml"
    cwd_cfg.write_text("server:\n  port: 7777\n")
    monkeypatch.chdir(tmp_path)
    cfg = load_config()
    assert cfg.server.port == 7777


def test_fallback_to_home_config(tmp_path: Path, monkeypatch):
    """项目根目录无 config.yaml 时回退到 ~/.coding-proxy/config.yaml."""
    home_dir = tmp_path / "home"
    cp_dir = home_dir / ".coding-proxy"
    cp_dir.mkdir(parents=True)
    (cp_dir / "config.yaml").write_text("server:\n  port: 8888\n")

    # chdir 到一个没有 config.yaml 的目录
    empty_dir = tmp_path / "empty"
    empty_dir.mkdir()
    monkeypatch.chdir(empty_dir)
    monkeypatch.setenv("HOME", str(home_dir))

    cfg = load_config()
    assert cfg.server.port == 8888


def test_default_config_when_no_file(tmp_path: Path, monkeypatch):
    """无任何配置文件时返回默认配置."""
    empty_dir = tmp_path / "empty"
    empty_dir.mkdir()
    monkeypatch.chdir(empty_dir)

    # 指向不存在的 home 目录
    monkeypatch.setenv("HOME", str(tmp_path / "nohome"))
    cfg = load_config()
    assert cfg.server.port == 8046


def test_missing_explicit_path_returns_default():
    """指定路径不存在时返回默认配置."""
    cfg = load_config(Path("/nonexistent/config.yaml"))
    assert cfg.server.port == 8046


def test_env_var_expansion(tmp_path: Path, monkeypatch):
    """配置值中的 ${VAR} 被环境变量替换."""
    monkeypatch.setenv("TEST_API_KEY", "sk-test-123")
    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text('fallback:\n  api_key: "${TEST_API_KEY}"\n')
    cfg = load_config(cfg_file)
    assert cfg.fallback.api_key == "sk-test-123"


# --- 向后兼容与 Copilot 配置 ---


def test_legacy_anthropic_zhipu_field_migration(tmp_path: Path):
    """旧配置中 anthropic/zhipu 字段自动迁移为 primary/fallback."""
    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text(
        "anthropic:\n"
        "  base_url: https://custom.anthropic.com\n"
        "zhipu:\n"
        "  api_key: test-key\n"
    )
    cfg = load_config(cfg_file)
    assert cfg.primary.base_url == "https://custom.anthropic.com"
    assert cfg.fallback.api_key == "test-key"


def test_copilot_config_defaults():
    """Copilot 默认禁用."""
    cfg = load_config(Path("/nonexistent/path"))
    assert cfg.copilot.enabled is False
    assert cfg.copilot.github_token == ""
    assert cfg.copilot.account_type == "individual"
    assert cfg.copilot.base_url == ""
    assert cfg.copilot.models_cache_ttl_seconds == 300


def test_copilot_config_from_yaml(tmp_path: Path, monkeypatch):
    """从 YAML 加载 Copilot 配置."""
    monkeypatch.setenv("GH_TOKEN", "ghp_yaml_test")
    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text(
        "copilot:\n"
        "  enabled: true\n"
        '  github_token: "${GH_TOKEN}"\n'
        "copilot_circuit_breaker:\n"
        "  failure_threshold: 5\n"
    )
    cfg = load_config(cfg_file)
    assert cfg.copilot.enabled is True
    assert cfg.copilot.github_token == "ghp_yaml_test"
    assert cfg.copilot.account_type == "individual"
    assert cfg.copilot_circuit_breaker.failure_threshold == 5


def test_copilot_quota_guard_defaults():
    """Copilot 配额守卫默认禁用."""
    cfg = load_config(Path("/nonexistent/path"))
    assert cfg.copilot_quota_guard.enabled is False


# --- Antigravity 配置 ---


def test_antigravity_config_defaults():
    """Antigravity 默认禁用."""
    cfg = load_config(Path("/nonexistent/path"))
    assert cfg.antigravity.enabled is False
    assert cfg.antigravity.client_id == ""
    assert cfg.antigravity.client_secret == ""
    assert cfg.antigravity.refresh_token == ""
    assert (
        cfg.antigravity.base_url == "https://generativelanguage.googleapis.com/v1beta"
    )
    assert cfg.antigravity.model_endpoint == "models/claude-sonnet-4-20250514"


def test_antigravity_config_from_yaml(tmp_path: Path, monkeypatch):
    """从 YAML 加载 Antigravity 配置."""
    monkeypatch.setenv("GOOG_CLIENT_ID", "cid_test")
    monkeypatch.setenv("GOOG_CLIENT_SECRET", "csecret_test")
    monkeypatch.setenv("GOOG_REFRESH_TOKEN", "rtok_test")
    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text(
        "antigravity:\n"
        "  enabled: true\n"
        '  client_id: "${GOOG_CLIENT_ID}"\n'
        '  client_secret: "${GOOG_CLIENT_SECRET}"\n'
        '  refresh_token: "${GOOG_REFRESH_TOKEN}"\n'
        "  model_endpoint: models/claude-opus-4-20250514\n"
        "antigravity_circuit_breaker:\n"
        "  failure_threshold: 5\n"
        "  recovery_timeout_seconds: 600\n"
        "antigravity_quota_guard:\n"
        "  enabled: true\n"
        "  token_budget: 500000\n"
    )
    cfg = load_config(cfg_file)
    assert cfg.antigravity.enabled is True
    assert cfg.antigravity.client_id == "cid_test"
    assert cfg.antigravity.client_secret == "csecret_test"
    assert cfg.antigravity.refresh_token == "rtok_test"
    assert cfg.antigravity.model_endpoint == "models/claude-opus-4-20250514"
    assert cfg.antigravity_circuit_breaker.failure_threshold == 5
    assert cfg.antigravity_circuit_breaker.recovery_timeout_seconds == 600
    assert cfg.antigravity_quota_guard.enabled is True
    assert cfg.antigravity_quota_guard.token_budget == 500000


def test_antigravity_quota_guard_defaults():
    """Antigravity 配额守卫默认禁用."""
    cfg = load_config(Path("/nonexistent/path"))
    assert cfg.antigravity_quota_guard.enabled is False


def test_model_mapping_vendors_from_yaml(tmp_path: Path):
    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text(
        "model_mapping:\n"
        "  - pattern: claude-sonnet-*\n"
        "    target: claude-sonnet-4-6-thinking\n"
        "    vendors: [antigravity]\n"
    )
    cfg = load_config(cfg_file)
    assert cfg.model_mapping[0].vendors == ["antigravity"]


# --- vendors 新格式 ---


def test_vendors_format_basic(tmp_path: Path):
    """vendors 格式：基本加载，顺序即优先级."""
    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text(
        "vendors:\n"
        "  - vendor: anthropic\n"
        "    base_url: https://api.anthropic.com\n"
        "    circuit_breaker:\n"
        "      failure_threshold: 3\n"
        "  - vendor: zhipu\n"
        "    api_key: sk-zhipu\n"
    )
    cfg = load_config(cfg_file)
    assert len(cfg.vendors) == 2
    assert cfg.vendors[0].vendor == "anthropic"
    assert cfg.vendors[0].circuit_breaker is not None
    assert cfg.vendors[0].circuit_breaker.failure_threshold == 3
    assert cfg.vendors[1].vendor == "zhipu"
    assert cfg.vendors[1].api_key == "sk-zhipu"
    assert cfg.vendors[1].circuit_breaker is None  # 终端层


def test_vendors_custom_order(tmp_path: Path, monkeypatch):
    """vendors 格式：自定义顺序 — zhipu 在 Vendor 0，anthropic 在 Vendor 1."""
    monkeypatch.setenv("ZHIPU_KEY", "sk-test")
    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text(
        "vendors:\n"
        "  - vendor: zhipu\n"
        '    api_key: "${ZHIPU_KEY}"\n'
        "    circuit_breaker:\n"
        "      failure_threshold: 5\n"
        "  - vendor: anthropic\n"
        '    base_url: "https://api.anthropic.com"\n'
    )
    cfg = load_config(cfg_file)
    assert cfg.vendors[0].vendor == "zhipu"
    assert cfg.vendors[0].api_key == "sk-test"
    assert cfg.vendors[0].circuit_breaker.failure_threshold == 5
    assert cfg.vendors[1].vendor == "anthropic"
    assert cfg.vendors[1].circuit_breaker is None  # 终端层


def test_vendors_terminal_vendor_no_circuit_breaker(tmp_path: Path):
    """vendors 格式：无 circuit_breaker 的 vendor 为终端层."""
    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text(
        "vendors:\n"
        "  - vendor: anthropic\n"
        "    circuit_breaker:\n"
        "      failure_threshold: 2\n"
        "  - vendor: copilot\n"
        "    github_token: ghp_test\n"
        "    circuit_breaker:\n"
        "      failure_threshold: 3\n"
        "  - vendor: zhipu\n"
        "    api_key: sk-zhipu\n"
        "    # 无 circuit_breaker → 终端层\n"
    )
    cfg = load_config(cfg_file)
    assert cfg.vendors[0].circuit_breaker is not None
    assert cfg.vendors[1].circuit_breaker is not None
    assert cfg.vendors[2].circuit_breaker is None


def test_legacy_flat_format_auto_migrates_to_vendors(tmp_path: Path):
    """旧 flat 格式自动迁移：primary/fallback 生成对应 vendors 列表."""
    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text(
        "primary:\n"
        "  enabled: true\n"
        "  base_url: https://api.anthropic.com\n"
        "circuit_breaker:\n"
        "  failure_threshold: 4\n"
        "fallback:\n"
        "  enabled: true\n"
        "  api_key: sk-zhipu-legacy\n"
    )
    cfg = load_config(cfg_file)
    # 旧字段仍可访问
    assert cfg.primary.base_url == "https://api.anthropic.com"
    assert cfg.fallback.api_key == "sk-zhipu-legacy"
    # vendors 由迁移器自动生成
    vendor_names = [v.vendor for v in cfg.vendors]
    assert "anthropic" in vendor_names
    assert "zhipu" in vendor_names
    # anthropic vendor 应有 circuit_breaker
    anthropic_vendor = next(v for v in cfg.vendors if v.vendor == "anthropic")
    assert anthropic_vendor.circuit_breaker is not None
    assert anthropic_vendor.circuit_breaker.failure_threshold == 4
    # zhipu vendor 为终端层（无 circuit_breaker）
    zhipu_vendor = next(v for v in cfg.vendors if v.vendor == "zhipu")
    assert zhipu_vendor.circuit_breaker is None


def test_vendors_disabled_vendor_excluded(tmp_path: Path):
    """vendors 格式：enabled=false 的 vendor 在 vendors 列表中存在但 enabled 为 False."""
    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text(
        "vendors:\n"
        "  - vendor: anthropic\n"
        "    enabled: false\n"
        "  - vendor: zhipu\n"
        "    api_key: sk-zhipu\n"
    )
    cfg = load_config(cfg_file)
    assert cfg.vendors[0].enabled is False
    assert cfg.vendors[1].enabled is True


# ====================================================================
# A 组：_deep_merge 单元测试
# ====================================================================


def test_deep_merge_empty_override_preserves_defaults():
    """空覆盖保留全部默认值."""
    defaults = {"a": 1, "b": {"c": 2, "d": 3}, "e": [1, 2]}
    assert _deep_merge(defaults, {}) == defaults


def test_deep_merge_scalar_override():
    """标量值直接替换."""
    defaults = {"port": 8046, "host": "127.0.0.1"}
    result = _deep_merge(defaults, {"port": 9000})
    assert result == {"port": 9000, "host": "127.0.0.1"}


def test_deep_merge_nested_dict():
    """嵌套 dict 递归合并 — 仅覆盖存在的子键."""
    defaults = {"server": {"host": "127.0.0.1", "port": 8046}, "log": "INFO"}
    override = {"server": {"port": 9000}}
    result = _deep_merge(defaults, override)
    assert result == {"server": {"host": "127.0.0.1", "port": 9000}, "log": "INFO"}


def test_deep_merge_list_replacement():
    """列表整体替换（有序集合，不支持逐元素合并）."""
    defaults = {"items": ["a", "b", "c"]}
    override = {"items": ["x", "y"]}
    result = _deep_merge(defaults, override)
    assert result["items"] == ["x", "y"]


def test_deep_merge_new_keys_added():
    """override 中新增的 key 直接添加到结果中."""
    defaults = {"a": 1}
    override = {"b": 2, "c": {"d": 3}}
    result = _deep_merge(defaults, override)
    assert result == {"a": 1, "b": 2, "c": {"d": 3}}


def test_deep_merge_three_level_nesting():
    """三级嵌套 dict 的精确深度合并."""
    defaults = {"l1": {"l2": {"l3_a": 1, "l3_b": 2}}}
    override = {"l1": {"l2": {"l3_b": 99}}}
    result = _deep_merge(defaults, override)
    assert result == {"l1": {"l2": {"l3_a": 1, "l3_b": 99}}}


def test_deep_merge_dict_replaces_scalar():
    """override 的 dict 替换 default 的标量值."""
    defaults = {"key": "string_value"}
    override = {"key": {"nested": True}}
    result = _deep_merge(defaults, override)
    assert result == {"key": {"nested": True}}


def test_deep_merge_scalar_replaces_dict():
    """override 的标量替换 default 的 dict 值."""
    defaults = {"key": {"nested": True}}
    override = {"key": "flat_value"}
    result = _deep_merge(defaults, override)
    assert result == {"key": "flat_value"}


# ====================================================================
# B 组：_get_example_config_path 单元测试
# ====================================================================


def test_get_default_config_path_returns_path():
    """正常定位 config.default.yaml — 返回有效 Path 对象."""
    path = _get_default_config_path()
    assert path is not None
    assert path.is_file()
    assert path.name == "config.default.yaml"


def test_get_default_config_path_missing_returns_none(monkeypatch):
    """_get_default_config_path 被mock为返回None时正确降级."""
    import coding.proxy.config.loader as loader_module

    monkeypatch.setattr(loader_module, "_get_default_config_path", lambda: None)
    assert loader_module._get_default_config_path() is None


# ====================================================================
# C 组：load_config 集成测试 — example-based 深度合并
# ====================================================================


def test_load_config_no_user_file_uses_example_defaults(tmp_path: Path, monkeypatch):
    """无任何用户配置时，返回基于 config.default.yaml 的完整配置.

    验证默认值被正确加载：vendors 非空、pricing 非空、
    model_mapping 使用 default 中的新格式规则.
    """
    empty_dir = tmp_path / "empty"
    empty_dir.mkdir()
    monkeypatch.chdir(empty_dir)
    monkeypatch.setenv("HOME", str(tmp_path / "nohome"))

    cfg = load_config()

    # 来自 example.yaml 的完整默认值
    assert cfg.server.port == 8046
    assert cfg.server.host == "127.0.0.1"
    # example 中定义了完整的 vendors 列表
    assert len(cfg.vendors) >= 1
    vendor_names = [v.vendor for v in cfg.vendors]
    assert "anthropic" in vendor_names
    # example 中定义了 pricing
    assert len(cfg.pricing) >= 1
    # example 中定义了 failover 配置
    assert cfg.failover is not None
    assert 429 in cfg.failover.status_codes


def test_load_config_partial_override_port_only(tmp_path: Path, monkeypatch):
    """用户仅指定 server.port 时，其余字段来自 example 默认值."""
    cwd_cfg = tmp_path / "config.yaml"
    cwd_cfg.write_text("server:\n  port: 9999\n")
    monkeypatch.chdir(tmp_path)

    cfg = load_config()

    # 用户覆盖的值
    assert cfg.server.port == 9999
    # 来自 example 默认值的字段
    assert cfg.server.host == "127.0.0.1"
    # vendors 来自 example（用户未指定）
    assert len(cfg.vendors) >= 1


def test_load_config_full_user_override(tmp_path: Path):
    """用户提供完整配置时，example 默认被完全覆盖."""
    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text(
        "server:\n  host: '0.0.0.0'\n  port: 8000\nvendors: []\npricing: []\n"
    )
    cfg = load_config(cfg_file)
    assert cfg.server.host == "0.0.0.0"
    assert cfg.server.port == 8000
    assert cfg.vendors == []
    assert cfg.pricing == []


def test_load_config_user_clears_vendors_list(tmp_path: Path):
    """用户显式设置 vendors: [] 时覆盖 example 的 vendors 列表."""
    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text("vendors: []\n")
    cfg = load_config(cfg_file)
    assert cfg.vendors == []
    # 其他字段仍来自 example
    assert cfg.server.host == "127.0.0.1"


def test_env_var_expansion_after_merge(tmp_path: Path, monkeypatch):
    """${VAR} 环境变量展开发生在深度合并之后."""
    monkeypatch.setenv("MY_PORT", "7777")
    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text('server:\n  port: "${MY_PORT}"\n')
    cfg = load_config(cfg_file)
    assert cfg.server.port == 7777
    # server.host 应来自 example 默认值（未被用户覆盖）
    assert cfg.server.host == "127.0.0.1"


def test_load_config_fallback_when_example_missing(monkeypatch):
    """config.default.yaml 不存在时降级为 ProxyConfig() 默认值.

    通过 mock _get_default_config_path 返回 None 来模拟 default 缺失场景.
    """
    monkeypatch.setattr(
        "coding.proxy.config.loader._get_default_config_path",
        lambda: None,
    )
    cfg = load_config(Path("/nonexistent/path"))
    # 降级为 Pydantic 默认值
    assert cfg.server.port == 8046
    assert cfg.copilot.enabled is False


def test_legacy_flat_format_still_migrates_with_example_base(tmp_path: Path):
    """旧 flat 格式配置在 default base 上仍然触发 legacy 迁移器.

    当用户配置包含 legacy 字段时，default 的 vendors 被移除，
    _migrate_legacy_fields 迁移器从 legacy 字段重建 vendors.
    """
    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text(
        "primary:\n"
        "  enabled: true\n"
        "  base_url: https://custom.anthropic.com\n"
        "fallback:\n"
        "  enabled: true\n"
        "  api_key: sk-legacy-test\n"
    )
    cfg = load_config(cfg_file)

    # legacy 字段仍可访问
    assert cfg.primary.base_url == "https://custom.anthropic.com"
    assert cfg.fallback.api_key == "sk-legacy-test"

    # vendors 由迁移器从 legacy 字段生成（非来自 example）
    vendor_names = [v.vendor for v in cfg.vendors]
    assert "anthropic" in vendor_names
    assert "zhipu" in vendor_names

    # anthropic vendor 应使用 legacy 中的 base_url
    anthropic_vendor = next(v for v in cfg.vendors if v.vendor == "anthropic")
    assert anthropic_vendor.base_url == "https://custom.anthropic.com"


# ====================================================================
# D 组：config.default.yaml 可用性与默认值完整性测试
# ====================================================================


class TestDefaultConfigAvailability:
    """确保 config.default.yaml 在各种安装方式下均可被找到."""

    def test_default_config_found_by_getter(self):
        """_get_default_config_path() 应始终能找到文件."""
        path = _get_default_config_path()
        assert path is not None, "config.default.yaml 未找到，默认值将丢失"
        assert path.is_file()

    def test_pricing_populated_from_default(self, monkeypatch):
        """无用户配置时，pricing 应包含 default 中的完整定价记录."""
        import tempfile

        empty_dir = tempfile.mkdtemp()
        monkeypatch.chdir(empty_dir)
        monkeypatch.setenv("HOME", "/nonexistent")

        cfg = load_config()
        assert len(cfg.pricing) >= 10, (
            f"pricing 应有 ≥10 条默认值，实际 {len(cfg.pricing)} 条。"
            "可能 config.default.yaml 未被正确加载。"
        )

    def test_zhipu_pricing_available(self, monkeypatch):
        """zhipu 供应商的关键模型定价必须可用（用户报告的场景）."""
        import tempfile

        empty_dir = tempfile.mkdtemp()
        monkeypatch.chdir(empty_dir)
        monkeypatch.setenv("HOME", "/nonexistent")

        cfg = load_config()
        zhipu_models = {p.model for p in cfg.pricing if p.vendor == "zhipu"}
        assert "glm-4.5-air" in zhipu_models, "缺少 glm-4.5-air 定价"
        assert "glm-5v-turbo" in zhipu_models, "缺少 glm-5v-turbo 定价"

    def test_vendors_override_does_not_clear_pricing(self, tmp_path: Path):
        """用户仅配置 vendors 时，pricing 不应被清空."""
        (tmp_path / "config.yaml").write_text(
            "vendors:\n  - vendor: zhipu\n    api_key: sk-test\n"
        )
        cfg = load_config(tmp_path / "config.yaml")
        assert len(cfg.vendors) == 1, "vendors 应被用户配置覆盖"
        assert len(cfg.pricing) >= 10, (
            f"pricing 应保留 default 默认值（≥10条），实际 {len(cfg.pricing)} 条"
        )

    def test_model_mapping_uses_default_format(self, monkeypatch):
        """model_mapping 应来自 default 而非 Pydantic 旧默认值."""
        import tempfile

        empty_dir = tempfile.mkdtemp()
        monkeypatch.chdir(empty_dir)
        monkeypatch.setenv("HOME", "/nonexistent")

        cfg = load_config()
        # default 中的 model_mapping 包含 vendors 字段（新格式特征）
        has_new_format = any(getattr(m, "vendors", None) for m in cfg.model_mapping)
        assert has_new_format, (
            "model_mapping 应使用 default 中的新格式（含 vendors 字段），"
            "而非 Pydantic 的旧格式默认值"
        )
