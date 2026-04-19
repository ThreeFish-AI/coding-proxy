"""配置自动初始化单元测试."""

from pathlib import Path

from coding.proxy.config.loader import _ensure_user_config, load_config

# ── A 组：_ensure_user_config 核心逻辑 ───────────────────────────


class TestEnsureUserConfig:
    """_ensure_user_config 幂等性与安全性测试."""

    def test_creates_config_when_none_exists(self, tmp_path: Path, monkeypatch):
        """无任何用户配置时，自动从 default 复制到 ~/.coding-proxy/config.yaml."""
        home_dir = tmp_path / "home"
        home_dir.mkdir()
        monkeypatch.setenv("HOME", str(home_dir))

        result = _ensure_user_config()

        assert result is not None
        expected_path = home_dir / ".coding-proxy" / "config.yaml"
        assert result == expected_path
        assert expected_path.exists()
        # 验证内容非空（确实是从 default 复制的）
        content = expected_path.read_text()
        assert "server:" in content
        assert "vendors:" in content

    def test_returns_cwd_config_if_exists(self, tmp_path: Path, monkeypatch):
        """CWD 下已有 config.yaml 时直接返回，不创建新文件."""
        cwd_cfg = tmp_path / "config.yaml"
        cwd_cfg.write_text("server:\n  port: 7777\n")
        monkeypatch.chdir(tmp_path)

        home_dir = tmp_path / "home"
        home_dir.mkdir()
        monkeypatch.setenv("HOME", str(home_dir))

        result = _ensure_user_config()

        # 返回的是相对路径 "config.yaml"，解析后应与 cwd_cfg 一致
        assert result.resolve() == cwd_cfg.resolve()
        # 不应在 home 下创建
        assert not (home_dir / ".coding-proxy" / "config.yaml").exists()

    def test_returns_home_config_if_exists(self, tmp_path: Path, monkeypatch):
        """~/.coding-proxy/config.yaml 已存在时直接返回，不覆盖."""
        home_dir = tmp_path / "home"
        cp_dir = home_dir / ".coding-proxy"
        cp_dir.mkdir(parents=True)
        existing = cp_dir / "config.yaml"
        existing.write_text("server:\n  port: 8888\n")
        monkeypatch.setenv("HOME", str(home_dir))
        empty_dir = tmp_path / "empty"
        empty_dir.mkdir()
        monkeypatch.chdir(empty_dir)

        result = _ensure_user_config()

        assert result == existing
        # 不应覆盖现有内容
        assert existing.read_text() == "server:\n  port: 8888\n"

    def test_idempotent_multiple_calls(self, tmp_path: Path, monkeypatch):
        """多次调用不产生副作用（幂等性）."""
        home_dir = tmp_path / "home"
        home_dir.mkdir()
        monkeypatch.setenv("HOME", str(home_dir))
        monkeypatch.chdir(tmp_path)

        first = _ensure_user_config()
        second = _ensure_user_config()

        assert first == second
        assert first.exists()

    def test_creates_parent_directory(self, tmp_path: Path, monkeypatch):
        """~/.coding-proxy/ 目录不存在时自动创建."""
        home_dir = tmp_path / "home"
        home_dir.mkdir()  # 只创建 home，不创建 .coding-proxy
        monkeypatch.setenv("HOME", str(home_dir))
        monkeypatch.chdir(tmp_path)

        result = _ensure_user_config()

        assert result is not None
        assert result.parent.exists()
        assert result.parent.is_dir()

    def test_graceful_when_default_missing(self, tmp_path: Path, monkeypatch):
        """config.default.yaml 缺失时返回 None，不崩溃."""
        home_dir = tmp_path / "home"
        home_dir.mkdir()
        monkeypatch.setenv("HOME", str(home_dir))
        monkeypatch.chdir(tmp_path)

        import coding.proxy.config.loader as loader_module

        monkeypatch.setattr(loader_module, "_get_default_config_path", lambda: None)

        result = _ensure_user_config()

        assert result is None
        # 不应创建任何文件
        assert not (home_dir / ".coding-proxy").exists()


# ── B 组：与 load_config 的集成测试 ─────────────────────────────


class TestAutoInitIntegration:
    """验证 _ensure_user_config 与 load_config 的集成行为."""

    def test_load_config_auto_creates_home_config(self, tmp_path: Path, monkeypatch):
        """无配置时 load_config 自动创建 ~/.coding-proxy/config.yaml."""
        home_dir = tmp_path / "home"
        home_dir.mkdir()
        monkeypatch.setenv("HOME", str(home_dir))
        empty_dir = tmp_path / "empty"
        empty_dir.mkdir()
        monkeypatch.chdir(empty_dir)

        cfg = load_config()

        assert cfg.server.port == 3392
        created = home_dir / ".coding-proxy" / "config.yaml"
        assert created.exists()

    def test_load_config_with_explicit_path_no_auto_init(self, tmp_path: Path):
        """指定 -c 路径时不会触发自动初始化."""
        home_dir = tmp_path / "home"
        home_dir.mkdir()

        cfg_file = tmp_path / "my-config.yaml"
        cfg_file.write_text("server:\n  port: 9999\n")

        cfg = load_config(cfg_file)

        assert cfg.server.port == 9999
        assert not (home_dir / ".coding-proxy").exists()

    def test_load_config_cwd_priority_over_auto_init(self, tmp_path: Path, monkeypatch):
        """CWD 有 config.yaml 时优先使用，不触发 home 初始化."""
        cwd_cfg = tmp_path / "config.yaml"
        cwd_cfg.write_text("server:\n  port: 7777\n")
        monkeypatch.chdir(tmp_path)

        home_dir = tmp_path / "home"
        home_dir.mkdir()
        monkeypatch.setenv("HOME", str(home_dir))

        cfg = load_config()

        assert cfg.server.port == 7777
        assert not (home_dir / ".coding-proxy" / "config.yaml").exists()
