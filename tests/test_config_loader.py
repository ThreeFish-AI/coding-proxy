"""配置加载器单元测试."""

from pathlib import Path

from coding.proxy.config.loader import load_config


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
    assert cfg.antigravity.base_url == "https://generativelanguage.googleapis.com/v1beta"
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


def test_model_mapping_backends_from_yaml(tmp_path: Path):
    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text(
        "model_mapping:\n"
        "  - pattern: claude-sonnet-*\n"
        "    target: claude-sonnet-4-6-thinking\n"
        "    backends: [antigravity]\n"
    )
    cfg = load_config(cfg_file)
    assert cfg.model_mapping[0].backends == ["antigravity"]
