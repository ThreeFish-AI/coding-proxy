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
