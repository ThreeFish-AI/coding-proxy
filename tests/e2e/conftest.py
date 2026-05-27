"""E2E 集成测试共享 fixtures — Antigravity 真实凭证加载与测试对象构建."""

from __future__ import annotations

import os
from typing import Any

import pytest

# ── 模块级门控：未设置环境变量时跳过整个 e2e 包 ──

_SKIP_REASON = "Set RUN_ANTIGRAVITY_E2E=1 to enable Antigravity E2E tests"


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line(
        "markers", "e2e: End-to-end tests requiring real Antigravity credentials"
    )


def _load_real_credentials() -> dict[str, str] | None:
    """从 ~/.coding-proxy/ 加载真实的 Google OAuth 凭证."""
    from coding.proxy.auth.providers.google import (
        _DEFAULT_CLIENT_ID,
        _DEFAULT_CLIENT_SECRET,
    )
    from coding.proxy.auth.store import TokenStoreManager
    from coding.proxy.config.loader import load_config

    try:
        token_store = TokenStoreManager()
        token_store.load()
        google_tokens = token_store.get("google")
        if not google_tokens.refresh_token:
            return None

        config = load_config()

        # 从 vendors 列表查找 antigravity 配置
        client_id = ""
        client_secret = ""
        base_url = ""
        model_endpoint = "models/claude-sonnet-4-20250514"
        project_id = ""

        for vc in config.vendors:
            if vc.vendor == "antigravity":
                client_id = vc.client_id or _DEFAULT_CLIENT_ID
                client_secret = vc.client_secret or _DEFAULT_CLIENT_SECRET
                base_url = (
                    vc.base_url or "https://generativelanguage.googleapis.com/v1beta"
                )
                model_endpoint = vc.model_endpoint or model_endpoint
                break

        # 优先使用 config.yaml 中的 refresh_token，否则使用 token store
        refresh_token = ""
        for vc in config.vendors:
            if vc.vendor == "antigravity" and vc.refresh_token:
                refresh_token = vc.refresh_token
                break
        if not refresh_token:
            refresh_token = google_tokens.refresh_token

        return {
            "client_id": client_id,
            "client_secret": client_secret,
            "refresh_token": refresh_token,
            "base_url": base_url,
            "model_endpoint": model_endpoint,
            "project_id": project_id,
        }
    except Exception:
        return None


# ── Fixtures ──


@pytest.fixture(scope="session")
def e2e_credentials() -> dict[str, str]:
    """加载真实 Antigravity OAuth 凭证，失败则跳过."""
    if os.environ.get("RUN_ANTIGRAVITY_E2E") != "1":
        pytest.skip(_SKIP_REASON)
    creds = _load_real_credentials()
    if creds is None:
        pytest.skip("No valid Antigravity credentials found in ~/.coding-proxy/")
    return creds


@pytest.fixture(scope="session")
def antigravity_config(e2e_credentials: dict[str, str]) -> Any:
    """构建标准 GLA 模式的 AntigravityConfig."""
    from coding.proxy.config.vendors import AntigravityConfig

    return AntigravityConfig(
        enabled=True,
        client_id=e2e_credentials["client_id"],
        client_secret=e2e_credentials["client_secret"],
        refresh_token=e2e_credentials["refresh_token"],
        base_url=e2e_credentials["base_url"],
        model_endpoint=e2e_credentials["model_endpoint"],
        timeout_ms=60000,
    )


@pytest.fixture(scope="session")
def antigravity_config_v1internal(e2e_credentials: dict[str, str]) -> Any:
    """构建 v1internal 模式的 AntigravityConfig（无 project_id，触发自动发现）."""
    from coding.proxy.config.vendors import AntigravityConfig

    return AntigravityConfig(
        enabled=True,
        client_id=e2e_credentials["client_id"],
        client_secret=e2e_credentials["client_secret"],
        refresh_token=e2e_credentials["refresh_token"],
        base_url="https://cloudcode-pa.googleapis.com/v1internal",
        model_endpoint=e2e_credentials["model_endpoint"],
        timeout_ms=60000,
    )


@pytest.fixture
async def antigravity_vendor(antigravity_config: Any) -> Any:
    """构建标准 GLA 模式的 AntigravityVendor（function scope，每次测试独立）."""
    from coding.proxy.config.schema import FailoverConfig
    from coding.proxy.routing.model_mapper import ModelMapper
    from coding.proxy.vendors.antigravity import AntigravityVendor

    vendor = AntigravityVendor(antigravity_config, FailoverConfig(), ModelMapper([]))
    yield vendor
    await vendor.close()


@pytest.fixture
async def antigravity_vendor_v1internal(antigravity_config_v1internal: Any) -> Any:
    """构建 v1internal 模式的 AntigravityVendor."""
    from coding.proxy.config.schema import FailoverConfig
    from coding.proxy.routing.model_mapper import ModelMapper
    from coding.proxy.vendors.antigravity import AntigravityVendor

    vendor = AntigravityVendor(
        antigravity_config_v1internal, FailoverConfig(), ModelMapper([])
    )
    yield vendor
    await vendor.close()


@pytest.fixture
def minimal_request_body() -> dict[str, Any]:
    """最小 Anthropic 格式请求体（用于最小化 token 消耗）."""
    return {
        "model": "claude-sonnet-4-20250514",
        "messages": [{"role": "user", "content": "Say exactly: pong"}],
        "max_tokens": 32,
    }


@pytest.fixture(scope="session")
def e2e_app(e2e_credentials: dict[str, str]) -> Any:
    """构建仅启用 Antigravity 的 FastAPI 应用（临时 DB）."""
    import tempfile

    from coding.proxy.config.schema import ProxyConfig
    from coding.proxy.server.app import create_app

    tmpdir = tempfile.mkdtemp(prefix="e2e-antigravity-")
    db_path = os.path.join(tmpdir, "usage.db")
    compat_path = os.path.join(tmpdir, "compat.db")

    config = ProxyConfig(
        vendors=[
            {
                "vendor": "antigravity",
                "enabled": True,
                "client_id": e2e_credentials["client_id"],
                "client_secret": e2e_credentials["client_secret"],
                "refresh_token": e2e_credentials["refresh_token"],
                "base_url": "https://cloudcode-pa.googleapis.com/v1internal",
                "model_endpoint": e2e_credentials["model_endpoint"],
                "timeout_ms": 60000,
            },
        ],
        tiers=["antigravity"],
        database={"path": db_path, "compat_state_path": compat_path},
    )
    return create_app(config)


@pytest.fixture
async def e2e_client(e2e_app: Any) -> Any:
    """构建异步 HTTP 客户端（支持 SSE 流式测试）."""
    import httpx

    transport = httpx.ASGITransport(app=e2e_app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://test", timeout=60.0
    ) as client:
        yield client
