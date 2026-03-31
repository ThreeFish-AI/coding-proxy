"""启动自动登录 _auto_login_if_needed 单元测试."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from coding.proxy.auth.store import ProviderTokens, TokenStoreManager


def _make_store(data: dict | None = None) -> TokenStoreManager:
    """构造不读写磁盘的 TokenStoreManager."""
    store = TokenStoreManager()
    store._data = data or {}
    store.load = MagicMock()  # 阻止从磁盘读取覆盖 _data
    store.save = MagicMock()  # 阻止写磁盘
    return store


# ── loader.py 环境变量展开测试 ──────────────────────────────


def test_unexpanded_env_var_becomes_empty(tmp_path: Path):
    """未设置的环境变量 ${VAR} 展开为空字符串."""
    from coding.proxy.config.loader import load_config

    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text(
        "copilot:\n"
        "  enabled: true\n"
        '  github_token: "${NONEXISTENT_VAR_12345}"\n'
    )
    cfg = load_config(cfg_file)
    assert cfg.copilot.github_token == ""


def test_partial_env_expansion(tmp_path: Path, monkeypatch):
    """部分环境变量展开: 已设置的替换，未设置的清零."""
    monkeypatch.setenv("SET_VAR_AUTO_LOGIN_TEST", "hello")

    from coding.proxy.config.loader import load_config

    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text(
        "copilot:\n"
        "  enabled: true\n"
        '  github_token: "${SET_VAR_AUTO_LOGIN_TEST}"\n'
        "antigravity:\n"
        '  refresh_token: "${UNSET_VAR_67890}"\n'
    )
    cfg = load_config(cfg_file)
    assert cfg.copilot.github_token == "hello"
    assert cfg.antigravity.refresh_token == ""


# ── _auto_login_if_needed 测试 ──────────────────────────────


@pytest.mark.asyncio
async def test_trigger_login_even_when_disabled(tmp_path: Path):
    """Copilot 禁用但无凭证时仍触发登录（凭证获取与 enabled 解耦）."""
    from coding.proxy.cli import _auto_login_if_needed

    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text(
        "copilot:\n  enabled: false\n"
        "antigravity:\n  refresh_token: skip\n"
    )

    mock_prov = AsyncMock()
    mock_prov.needs_login.return_value = True
    mock_prov.login.return_value = ProviderTokens(access_token="ghp_new")

    empty_store = _make_store()

    with (
        patch("coding.proxy.auth.providers.github.GitHubDeviceFlowProvider", return_value=mock_prov),
        patch("coding.proxy.auth.store.TokenStoreManager", return_value=empty_store),
    ):
        await _auto_login_if_needed(cfg_file)

    mock_prov.login.assert_awaited_once()


@pytest.mark.asyncio
async def test_skip_login_when_store_has_credentials(tmp_path: Path):
    """Store 已有有效凭证时不触发登录（无论 enabled 状态）."""
    from coding.proxy.cli import _auto_login_if_needed

    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text(
        "copilot:\n  enabled: false\n"
        "antigravity:\n  refresh_token: skip\n"
    )

    mock_prov = AsyncMock()
    mock_prov.needs_login = MagicMock(return_value=False)
    mock_prov.validate.return_value = True

    valid_store = _make_store({"github": {"access_token": "ghp_valid"}})

    with (
        patch("coding.proxy.auth.providers.github.GitHubDeviceFlowProvider", return_value=mock_prov),
        patch("coding.proxy.auth.store.TokenStoreManager", return_value=valid_store),
    ):
        await _auto_login_if_needed(cfg_file)

    mock_prov.login.assert_not_awaited()


@pytest.mark.asyncio
async def test_skip_when_config_has_token(tmp_path: Path, monkeypatch):
    """config.yaml 已有 github_token 时不触发登录."""
    from coding.proxy.cli import _auto_login_if_needed

    monkeypatch.setenv("GH_TOKEN_TEST_AUTO", "ghp_from_env")
    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text(
        "copilot:\n"
        "  enabled: true\n"
        '  github_token: "${GH_TOKEN_TEST_AUTO}"\n'
        "antigravity:\n"
        "  refresh_token: skip\n"
    )

    with patch("coding.proxy.auth.providers.github.GitHubDeviceFlowProvider") as mock_cls:
        await _auto_login_if_needed(cfg_file)
        mock_cls.assert_not_called()


@pytest.mark.asyncio
async def test_trigger_login_when_no_token(tmp_path: Path):
    """Copilot 启用且无 token 时触发登录."""
    from coding.proxy.cli import _auto_login_if_needed

    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text("copilot:\n  enabled: true\nantigravity:\n  refresh_token: skip\n")

    mock_prov = AsyncMock()
    mock_prov.needs_login.return_value = True
    mock_prov.login.return_value = ProviderTokens(access_token="ghp_new")

    empty_store = _make_store()

    with (
        patch("coding.proxy.auth.providers.github.GitHubDeviceFlowProvider", return_value=mock_prov),
        patch("coding.proxy.auth.store.TokenStoreManager", return_value=empty_store),
    ):
        await _auto_login_if_needed(cfg_file)

    mock_prov.login.assert_awaited_once()
    mock_prov.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_validate_stale_token_triggers_login(tmp_path: Path):
    """Store 中有 token 但验证失败时触发重新登录."""
    from coding.proxy.cli import _auto_login_if_needed

    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text("copilot:\n  enabled: true\nantigravity:\n  refresh_token: skip\n")

    mock_prov = AsyncMock()
    mock_prov.needs_login = MagicMock(return_value=False)  # 同步方法需用 MagicMock
    mock_prov.validate.return_value = False
    mock_prov.login.return_value = ProviderTokens(access_token="ghp_fresh")

    stale_store = _make_store({"github": {"access_token": "ghp_stale"}})

    with (
        patch("coding.proxy.auth.providers.github.GitHubDeviceFlowProvider", return_value=mock_prov),
        patch("coding.proxy.auth.store.TokenStoreManager", return_value=stale_store),
    ):
        await _auto_login_if_needed(cfg_file)

    mock_prov.validate.assert_awaited_once()
    mock_prov.login.assert_awaited_once()


@pytest.mark.asyncio
async def test_skip_login_when_token_valid(tmp_path: Path):
    """Store 中有 token 且验证通过时不触发登录."""
    from coding.proxy.cli import _auto_login_if_needed

    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text("copilot:\n  enabled: true\nantigravity:\n  refresh_token: skip\n")

    mock_prov = AsyncMock()
    mock_prov.needs_login = MagicMock(return_value=False)  # 同步方法需用 MagicMock
    mock_prov.validate.return_value = True

    valid_store = _make_store({"github": {"access_token": "ghp_valid"}})

    with (
        patch("coding.proxy.auth.providers.github.GitHubDeviceFlowProvider", return_value=mock_prov),
        patch("coding.proxy.auth.store.TokenStoreManager", return_value=valid_store),
    ):
        await _auto_login_if_needed(cfg_file)

    mock_prov.login.assert_not_awaited()
    mock_prov.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_network_failure_does_not_block_startup(tmp_path: Path):
    """validate() 网络异常时不阻塞启动，跳过登录."""
    from coding.proxy.cli import _auto_login_if_needed

    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text("copilot:\n  enabled: true\nantigravity:\n  refresh_token: skip\n")

    mock_prov = AsyncMock()
    mock_prov.needs_login = MagicMock(return_value=False)  # 同步方法需用 MagicMock
    mock_prov.validate.side_effect = ConnectionError("no network")

    store = _make_store({"github": {"access_token": "ghp_maybe_valid"}})

    with (
        patch("coding.proxy.auth.providers.github.GitHubDeviceFlowProvider", return_value=mock_prov),
        patch("coding.proxy.auth.store.TokenStoreManager", return_value=store),
    ):
        await _auto_login_if_needed(cfg_file)

    mock_prov.login.assert_not_awaited()


@pytest.mark.asyncio
async def test_login_failure_closes_provider(tmp_path: Path):
    """登录失败时仍正确关闭 provider."""
    from coding.proxy.cli import _auto_login_if_needed

    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text("copilot:\n  enabled: true\nantigravity:\n  refresh_token: skip\n")

    mock_prov = AsyncMock()
    mock_prov.needs_login.return_value = True
    mock_prov.login.side_effect = RuntimeError("device flow timeout")

    store = _make_store()

    with (
        patch("coding.proxy.auth.providers.github.GitHubDeviceFlowProvider", return_value=mock_prov),
        patch("coding.proxy.auth.store.TokenStoreManager", return_value=store),
    ):
        await _auto_login_if_needed(cfg_file)

    mock_prov.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_antigravity_trigger_login_when_no_refresh_token(tmp_path: Path):
    """Antigravity 无 refresh_token 时触发登录."""
    from coding.proxy.cli import _auto_login_if_needed

    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text("copilot:\n  github_token: skip\nantigravity:\n  enabled: true\n")

    mock_prov = AsyncMock()
    mock_prov.needs_login.return_value = True
    mock_prov.login.return_value = ProviderTokens(
        access_token="goog_access", refresh_token="goog_refresh",
    )

    empty_store = _make_store()

    with (
        patch("coding.proxy.auth.providers.google.GoogleOAuthProvider", return_value=mock_prov),
        patch("coding.proxy.auth.store.TokenStoreManager", return_value=empty_store),
    ):
        await _auto_login_if_needed(cfg_file)

    mock_prov.login.assert_awaited_once()
    mock_prov.close.assert_awaited_once()


# ── GitHub 浏览器自动打开测试 ─────────────────────────────────


@pytest.mark.asyncio
async def test_github_login_opens_browser():
    """GitHub Device Flow 登录时自动打开浏览器."""
    from coding.proxy.auth.providers.github import GitHubDeviceFlowProvider

    prov = GitHubDeviceFlowProvider()

    mock_resp_device = MagicMock()
    mock_resp_device.json.return_value = {
        "user_code": "ABCD-1234",
        "verification_uri": "https://github.com/login/device",
        "verification_uri_complete": "https://github.com/login/device?user_code=ABCD-1234",
        "device_code": "dc_test",
        "interval": 0,
    }
    mock_resp_device.raise_for_status = MagicMock()

    mock_resp_token = MagicMock()
    mock_resp_token.json.return_value = {
        "access_token": "ghp_test_tok",
        "token_type": "bearer",
    }

    call_count = 0
    device_call_data = {}

    async def _mock_post(url, **kwargs):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            device_call_data.update(kwargs.get("data", {}))
            return mock_resp_device
        return mock_resp_token

    prov._http.post = _mock_post  # type: ignore[method-assign]

    with patch("coding.proxy.auth.providers.github.webbrowser") as mock_wb:
        tokens = await prov.login()

    mock_wb.open.assert_called_once_with(
        "https://github.com/login/device?user_code=ABCD-1234"
    )
    assert device_call_data["scope"] == "read:user user:email repo workflow"
    assert tokens.access_token == "ghp_test_tok"
    await prov.close()


@pytest.mark.asyncio
async def test_github_login_fallback_without_complete_uri():
    """无 verification_uri_complete 时回退到 verification_uri."""
    from coding.proxy.auth.providers.github import GitHubDeviceFlowProvider

    prov = GitHubDeviceFlowProvider()

    mock_resp_device = MagicMock()
    mock_resp_device.json.return_value = {
        "user_code": "ABCD-1234",
        "verification_uri": "https://github.com/login/device",
        # 注意: 无 verification_uri_complete
        "device_code": "dc_test",
        "interval": 0,
    }
    mock_resp_device.raise_for_status = MagicMock()

    mock_resp_token = MagicMock()
    mock_resp_token.json.return_value = {
        "access_token": "ghp_fallback",
        "token_type": "bearer",
    }

    call_count = 0

    async def _mock_post(url, **kwargs):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return mock_resp_device
        return mock_resp_token

    prov._http.post = _mock_post  # type: ignore[method-assign]

    with patch("coding.proxy.auth.providers.github.webbrowser") as mock_wb:
        tokens = await prov.login()

    mock_wb.open.assert_called_once_with("https://github.com/login/device")
    assert tokens.access_token == "ghp_fallback"
    await prov.close()
