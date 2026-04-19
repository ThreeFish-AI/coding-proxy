"""Native API 上游 ``base_url`` 覆写测试（YAML + 环境变量双通道）.

覆盖 :class:`coding.proxy.native_api.config.NativeApiConfig` 的三级优先级：

    env var（运行时） > YAML 显式字段（部署时） > Pydantic 内置默认（兜底）

以及确保 ``config.default.yaml`` 与 Pydantic ``default_factory`` 的双写一致性。
"""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest
import yaml
from fastapi import FastAPI
from fastapi.testclient import TestClient

from coding.proxy.config.loader import load_config
from coding.proxy.native_api import NativeProxyHandler
from coding.proxy.native_api.config import NativeApiConfig
from coding.proxy.native_api.routes import register_native_api_routes

# ── env 变量集合（与实现侧 _ENV_BASE_URL_MAP 对齐） ──────────────
_ENV_VARS = (
    "NATIVE_OPENAI_BASE_URL",
    "NATIVE_GEMINI_BASE_URL",
    "NATIVE_ANTHROPIC_BASE_URL",
)


@pytest.fixture(autouse=True)
def _clear_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """消除宿主环境对默认值断言的影响（各测试显式 setenv 覆写）."""
    for name in _ENV_VARS:
        monkeypatch.delenv(name, raising=False)


# ── 1. 默认 enabled=True（三家都开箱启用） ──────────────────────


def test_enabled_defaults_true_for_three_providers() -> None:
    cfg = NativeApiConfig()
    assert cfg.openai.enabled is True
    assert cfg.gemini.enabled is True
    assert cfg.anthropic.enabled is True


# ── 2. env 覆写三家 base_url ────────────────────────────────────


def test_env_overrides_all_three_base_urls(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NATIVE_OPENAI_BASE_URL", "https://openai.example")
    monkeypatch.setenv("NATIVE_GEMINI_BASE_URL", "https://gemini.example")
    monkeypatch.setenv("NATIVE_ANTHROPIC_BASE_URL", "https://anthropic.example")

    cfg = NativeApiConfig()
    assert cfg.openai.base_url == "https://openai.example"
    assert cfg.gemini.base_url == "https://gemini.example"
    assert cfg.anthropic.base_url == "https://anthropic.example"


# ── 3. YAML 字段覆盖内置默认（通过 load_config） ────────────────


def test_yaml_value_overrides_builtin_default(tmp_path: Path) -> None:
    """YAML 中 ``native_api.openai.base_url`` 显式赋值 → 生效，其他两家保持默认."""
    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text(
        'native_api:\n  openai:\n    base_url: "https://yaml.example"\n',
        encoding="utf-8",
    )
    cfg = load_config(cfg_file)

    assert cfg.native_api.openai.base_url == "https://yaml.example"
    # 其他两家 base_url 应保留 default（YAML 未覆盖）
    assert cfg.native_api.gemini.base_url == "https://generativelanguage.googleapis.com"
    assert cfg.native_api.anthropic.base_url == "https://api.anthropic.com"


# ── 4. env 优先级高于 YAML 显式值 ───────────────────────────────


def test_env_beats_explicit_yaml_value(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text(
        'native_api:\n  openai:\n    base_url: "https://yaml.example"\n',
        encoding="utf-8",
    )
    monkeypatch.setenv("NATIVE_OPENAI_BASE_URL", "https://env.example")

    cfg = load_config(cfg_file)
    assert cfg.native_api.openai.base_url == "https://env.example"


# ── 5. 空串 env 视作未设置，不覆盖上一层 ────────────────────────


def test_empty_env_var_does_not_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NATIVE_OPENAI_BASE_URL", "   ")  # 纯空白亦视作未设置
    cfg = NativeApiConfig()
    assert cfg.openai.base_url == "https://api.openai.com"


# ── 6. YAML 默认与 Pydantic 默认双写一致性（拦截漂移） ───────────


def test_default_config_yaml_matches_pydantic_builtin() -> None:
    """``config.default.yaml`` 中 ``native_api.*.base_url`` 必须与 Pydantic 默认严格相等."""
    # 定位 config.default.yaml：包内资源路径
    yaml_path = (
        Path(__file__).resolve().parent.parent
        / "src"
        / "coding"
        / "proxy"
        / "config"
        / "config.default.yaml"
    )
    assert yaml_path.is_file(), f"config.default.yaml missing at {yaml_path}"

    with yaml_path.open(encoding="utf-8") as fh:
        raw = yaml.safe_load(fh) or {}

    builtin = NativeApiConfig()
    yaml_block = raw.get("native_api", {})
    for provider in ("openai", "gemini", "anthropic"):
        yaml_url = yaml_block[provider]["base_url"]
        builtin_url = getattr(builtin, provider).base_url
        assert yaml_url == builtin_url, (
            f"native_api.{provider}.base_url 双写漂移："
            f"yaml={yaml_url!r} vs pydantic={builtin_url!r}"
        )


# ── 7. 默认配置允许 catch-all 路由（enabled=True 生效） ─────────


def test_default_config_allows_catch_all_routes() -> None:
    """默认 ``NativeApiConfig()``（无 env、无 YAML）下，/api/openai/** 不返回 404."""
    captured: list[httpx.Request] = []

    def route(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(200, json={"ok": True})

    transport = httpx.MockTransport(route)
    cfg = NativeApiConfig()  # 三家全默认 enabled=True
    handler = NativeProxyHandler(cfg, transport=transport)

    app = FastAPI()
    app.state.native_handler = handler
    register_native_api_routes(app, handler)

    with TestClient(app) as client:
        resp = client.post(
            "/api/openai/v1/chat/completions",
            json={"model": "gpt-4o", "messages": []},
            headers={"authorization": "Bearer sk-test"},
        )
    # enabled=True 默认生效 → 不应返回 404 "not enabled"
    assert resp.status_code != 404
    assert resp.status_code == 200
    assert len(captured) == 1
