"""CLI ``start``：交给 GenerationManager 运行、Dashboard 链接（token 只进控制台）."""

from __future__ import annotations

import subprocess
import sys
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from typer.testing import CliRunner

from coding.proxy.cli import _dashboard_host, _dashboard_host_rejected, app
from coding.proxy.config.home import config_db_path, load_or_create_admin_token
from coding.proxy.config.importer import boot, ensure_store_ready
from coding.proxy.config.service import ConfigService
from coding.proxy.server.generations import GenerationManager, create_degraded_app

runner = CliRunner()


def _start(*args: str, uvicorn_run):
    """patch uvicorn.run 与 auto-login 后调用 start（不绑定端口）."""
    with (
        patch("uvicorn.run", side_effect=uvicorn_run),
        patch("coding.proxy.cli._auto_login_if_needed", new_callable=AsyncMock),
        patch("coding.proxy.cli.banner.print_banner"),
    ):
        return runner.invoke(app, ["start", *args])


def test_start_runs_generation_manager_with_proxy_headers_off():
    captured: dict = {}

    def fake_run(app_obj, **kwargs):
        captured["app"] = app_obj
        captured.update(kwargs)

    result = _start("-p", "4200", uvicorn_run=fake_run)
    assert result.exit_code == 0, result.output
    assert isinstance(captured["app"], GenerationManager)
    assert captured["port"] == 4200
    assert captured["proxy_headers"] is False
    # banner 显示的端口与 uvicorn 绑定端口一致
    assert captured["app"].server.port == 4200


def test_start_prints_dashboard_url_with_admin_token(caplog):
    token = load_or_create_admin_token()
    with caplog.at_level("DEBUG"):
        result = _start(uvicorn_run=lambda *a, **kw: None)
    assert result.exit_code == 0, result.output
    flat = result.output.replace("\n", "")
    assert (
        f"Dashboard: http://127.0.0.1:3392/dashboard?tab=config#admin={token}" in flat
    )
    assert token not in caplog.text  # token 不进任何日志


def test_start_dashboard_host_display():
    assert _dashboard_host("0.0.0.0") == "127.0.0.1"
    assert _dashboard_host("") == "127.0.0.1"
    assert _dashboard_host("::") == "[::1]"
    assert _dashboard_host("::1") == "[::1]"
    assert _dashboard_host("192.168.1.5") == "192.168.1.5"
    assert _dashboard_host("proxy.lan") == "proxy.lan"


@pytest.mark.parametrize(
    ("bind", "allowed", "rejected"),
    [
        ("0.0.0.0", [], False),
        ("127.0.0.1", [], False),
        ("::", [], False),
        ("localhost", [], False),
        ("192.168.1.5", [], False),  # 绑定的 IP 字面量：中间件按本机地址放行
        ("fe80::1", [], False),
        ("proxy.lan", [], True),
        ("proxy.lan", ["Proxy.LAN."], False),
    ],
)
async def test_dashboard_host_rejected_matches_security_middleware(
    strict_hosts, bind, allowed, rejected
):
    """打印的链接会被 403 时才提示；判定与 SecurityMiddleware 的实际结果一致."""
    shown = _dashboard_host(bind)
    assert _dashboard_host_rejected(shown, allowed) is rejected
    ensure_store_ready()
    if allowed:
        ConfigService().set_path("server.allowed_hosts", allowed)
    # ASGITransport 的 scope["server"] 取 URL 主机，等价于 uvicorn 绑定该地址时的本机地址
    transport = httpx.ASGITransport(app=create_degraded_app(boot(None)))
    async with httpx.AsyncClient(
        transport=transport, base_url=f"http://{shown}:4200"
    ) as client:
        status = (await client.get("/dashboard?tab=config")).status_code
    assert (status == 403) is rejected


def test_start_warns_when_dashboard_host_not_allowed():
    result = _start("-h", "proxy.lan", uvicorn_run=lambda *a, **kw: None)
    assert result.exit_code == 0, result.output
    flat = result.output.replace("\n", "")
    assert "Dashboard: http://proxy.lan:3392/dashboard?tab=config#admin=" in flat
    assert "coding-proxy config set server.allowed_hosts '[\"proxy.lan\"]'" in flat
    assert "http://127.0.0.1:3392/dashboard" in flat  # SSH 隧道替代方案


def test_start_no_host_warning_for_allowed_or_loopback_hosts():
    ensure_store_ready()
    ConfigService().set_path("server.allowed_hosts", ["proxy.lan"])
    for args in ((), ("-h", "0.0.0.0"), ("-h", "proxy.lan")):
        result = _start(*args, uvicorn_run=lambda *a, **kw: None)
        assert result.exit_code == 0, result.output
        assert "allowed_hosts" not in result.output, args


def test_start_with_invalid_store_config_boots_degraded():
    """存储配置无效：不再退出，以 degraded 模式启动（仅 Dashboard / 管理 API）."""
    from coding.proxy.config.store import ConfigStore

    with ConfigStore(config_db_path()).transaction() as txn:
        txn.put_section("tiers", ["no-such-vendor"])
    with (
        patch("uvicorn.run") as run,
        patch("coding.proxy.cli._auto_login_if_needed", new_callable=AsyncMock) as al,
        patch("coding.proxy.cli.banner.print_banner"),
    ):
        result = runner.invoke(app, ["start"])
    assert result.exit_code == 0, result.output
    run.assert_called_once()
    al.assert_not_called()  # 配置无效时跳过自动登录
    manager = run.call_args.args[0]
    assert isinstance(manager, GenerationManager)
    assert manager.boot.resolved.config is None  # degraded


def test_python_m_dash_m_start_still_works(tmp_path):
    import os

    env = {
        **os.environ,
        "CODING_PROXY_HOME": str(tmp_path / "home"),
        "HOME": str(tmp_path / "home"),
    }
    proc = subprocess.run(
        [sys.executable, "-m", "coding.proxy", "start", "--help"],
        capture_output=True,
        text=True,
        cwd=tmp_path,
        env=env,
        timeout=120,
    )
    assert proc.returncode == 0, proc.stderr
    assert "--port" in proc.stdout
