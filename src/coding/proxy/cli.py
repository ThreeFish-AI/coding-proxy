"""CLI 入口 — Typer 命令行工具."""

from __future__ import annotations

import asyncio
import inspect
from pathlib import Path
from typing import Optional

import typer
from rich.console import Console

from .config.loader import load_config
from .logging.db import TokenLogger
from .logging.stats import show_usage

app = typer.Typer(name="coding-proxy", help="Claude Code 多后端智能代理服务")
console = Console()


# ── Auth 子命令 ─────────────────────────────────────────────
auth_app = typer.Typer(name="auth", help="管理 OAuth 登录凭证")
app.add_typer(auth_app, name="auth")


@auth_app.command("login")
def auth_login(
    provider: Optional[str] = typer.Option(None, "--provider", "-p", help="指定 provider (github/google)"),
) -> None:
    """执行 OAuth 浏览器登录."""
    asyncio.run(_run_auth_login(provider))


async def _run_auth_login(provider: str | None) -> None:
    from .auth.providers.github import GitHubDeviceFlowProvider
    from .auth.providers.google import GoogleOAuthProvider
    from .auth.store import TokenStoreManager

    store = TokenStoreManager()
    store.load()

    providers = []
    if provider == "github":
        providers = [("github", GitHubDeviceFlowProvider())]
    elif provider == "google":
        providers = [("google", GoogleOAuthProvider())]
    elif provider is None:
        providers = [
            ("github", GitHubDeviceFlowProvider()),
            ("google", GoogleOAuthProvider()),
        ]
    else:
        console.print(f"[red]未知 provider: {provider}[/red]")
        raise typer.Exit(1)

    for name, prov in providers:
        try:
            console.print(f"\n[bold cyan]登录 {name}...[/bold cyan]")
            tokens = await prov.login()
            store.set(name, tokens)
            console.print(f"[green]{name} 登录成功[/green]")
            await prov.close()
        except Exception as exc:
            console.print(f"[red]{name} 登录失败: {exc}[/red]")


@auth_app.command("status")
def auth_status() -> None:
    """查看已登录的 OAuth 凭证状态."""
    from .auth.store import TokenStoreManager

    store = TokenStoreManager()
    store.load()

    providers = store.list_providers()
    if not providers:
        console.print("[yellow]尚未登录任何 provider[/yellow]")
        return

    for name in providers:
        tokens = store.get(name)
        expired = tokens.is_expired
        status_text = "[red]已过期[/red]" if expired else "[green]有效[/green]"
        has_refresh = "有 refresh_token" if tokens.refresh_token else "无 refresh_token"
        console.print(f"  {name}: {status_text}  {has_refresh}")


@auth_app.command("reauth")
def auth_reauth(
    provider: str = typer.Argument(..., help="provider 名称 (github/google)"),
    port: int = typer.Option(8046, "--port", "-p", help="代理服务端口"),
) -> None:
    """触发运行中代理的 OAuth 重认证."""
    import httpx as _httpx

    try:
        resp = _httpx.post(f"http://127.0.0.1:{port}/api/reauth/{provider}", timeout=5)
        if resp.status_code == 202:
            console.print(f"[green]{provider} 重认证已触发，请在浏览器中完成登录[/green]")
        elif resp.status_code == 404:
            console.print(f"[red]重认证不可用（代理未启用对应后端）[/red]")
        else:
            console.print(f"[red]触发失败: {resp.status_code} {resp.text}[/red]")
    except _httpx.ConnectError:
        console.print("[red]代理服务未运行[/red]")


@auth_app.command("logout")
def auth_logout(
    provider: Optional[str] = typer.Option(None, "--provider", "-p", help="指定 provider（不指定则全部登出）"),
) -> None:
    """清除已存储的 OAuth 凭证."""
    from .auth.store import TokenStoreManager

    store = TokenStoreManager()
    store.load()

    if provider:
        store.remove(provider)
        console.print(f"[green]已登出 {provider}[/green]")
    else:
        for name in store.list_providers():
            store.remove(name)
        console.print("[green]已登出所有 provider[/green]")


# ── 自动登录辅助 ─────────────────────────────────────────────
async def _auto_login_if_needed(cfg_path: Path | None) -> None:
    """检查各 Provider 是否缺少凭证，自动触发浏览器登录.

    凭证获取与 Tier enabled 状态解耦：无论后端是否启用，
    只要本地无有效凭证即触发登录，以便用户后续启用时无需再走登录流程。

    两阶段检查:
    1. needs_login() — 快速本地判断（无凭证或已过期且无 refresh_token）
    2. validate()   — 网络验证已有凭证是否仍有效（仅在有凭证时触发）
    """
    from .auth.providers.github import GitHubDeviceFlowProvider
    from .auth.providers.google import GoogleOAuthProvider
    from .auth.store import TokenStoreManager

    cfg = load_config(cfg_path)
    store = TokenStoreManager()
    store.load()

    async def _resolve_needs_login(provider, tokens) -> bool:
        result = provider.needs_login(tokens)
        if inspect.isawaitable(result):
            return bool(await result)
        return bool(result)

    # --- GitHub / Copilot ---
    if not cfg.copilot.github_token:
        tokens = store.get("github")
        prov = GitHubDeviceFlowProvider()
        needs = await _resolve_needs_login(prov, tokens)
        if not needs and tokens.has_credentials:
            # 有凭证但可能过期/吊销 → 网络验证
            try:
                if not await prov.validate(tokens):
                    needs = True
            except Exception:
                pass  # 网络失败不阻塞启动
        if needs:
            console.print("[bold cyan]Copilot 层缺少有效凭证，启动 GitHub OAuth 登录...[/bold cyan]")
            try:
                tokens = await prov.login()
                store.set("github", tokens)
                console.print("[green]GitHub 登录成功[/green]")
            except Exception as exc:
                console.print(f"[red]GitHub 登录失败: {exc}[/red]")
            finally:
                await prov.close()
        else:
            await prov.close()

    # --- Google / Antigravity ---
    if not cfg.antigravity.refresh_token:
        tokens = store.get("google")
        prov = GoogleOAuthProvider()
        needs = await _resolve_needs_login(prov, tokens)
        if not needs and tokens.has_credentials:
            try:
                if not await prov.validate(tokens):
                    needs = True
            except Exception:
                pass
        if needs:
            console.print("[bold cyan]Antigravity 层缺少有效凭证，启动 Google OAuth 登录...[/bold cyan]")
            try:
                tokens = await prov.login()
                store.set("google", tokens)
                console.print("[green]Google 登录成功[/green]")
            except Exception as exc:
                console.print(f"[red]Google 登录失败: {exc}[/red]")
            finally:
                await prov.close()
        else:
            await prov.close()


# ── 主命令 ─────────────────────────────────────────────────────
@app.command()
def start(
    config: Optional[str] = typer.Option(None, "--config", "-c", help="配置文件路径"),
    port: Optional[int] = typer.Option(None, "--port", "-p", help="监听端口"),
    host: Optional[str] = typer.Option(None, "--host", "-h", help="监听地址"),
) -> None:
    """启动代理服务."""
    import uvicorn

    from .server.app import create_app

    cfg_path = Path(config) if config else None
    cfg = load_config(cfg_path)

    if port:
        cfg.server.port = port
    if host:
        cfg.server.host = host

    # 自动登录检查
    asyncio.run(_auto_login_if_needed(cfg_path))

    fastapi_app = create_app(cfg)
    uvicorn.run(fastapi_app, host=cfg.server.host, port=cfg.server.port, log_level="info")


@app.command()
def status(
    port: int = typer.Option(8046, "--port", "-p", help="代理服务端口"),
) -> None:
    """查看代理状态和当前活跃后端."""
    import httpx

    try:
        resp = httpx.get(f"http://127.0.0.1:{port}/api/status", timeout=5)
        data = resp.json()
        for tier_info in data.get("tiers", []):
            name = tier_info.get("name", "unknown")
            console.print(f"\n[bold green]{name}[/bold green]")
            cb = tier_info.get("circuit_breaker")
            if cb:
                console.print(f"  [cyan]熔断器:[/] {cb.get('state', 'unknown')}  失败={cb.get('failure_count', 0)}")
            qg = tier_info.get("quota_guard")
            if qg:
                console.print(f"  [cyan]配额:[/] {qg.get('state', 'unknown')}  {qg.get('usage_percent', 0)}% ({qg.get('window_usage_tokens', 0)}/{qg.get('budget_tokens', 0)})")
    except httpx.ConnectError:
        console.print("[red]代理服务未运行[/red]")


@app.command()
def usage(
    days: int = typer.Option(7, "--days", "-d", help="统计天数"),
    backend: Optional[str] = typer.Option(None, "--backend", "-b", help="过滤后端"),
    model: Optional[str] = typer.Option(None, "--model", "-m", help="过滤请求模型"),
    db_path: Optional[str] = typer.Option(None, "--db", help="数据库路径"),
) -> None:
    """查看 Token 使用统计."""
    cfg = load_config(Path(db_path) if db_path else None)
    logger = TokenLogger(cfg.db_path)
    asyncio.run(_run_usage(logger, days, backend, model))


async def _run_usage(logger: TokenLogger, days: int, backend: str | None,
                     model: str | None) -> None:
    await logger.init()
    await show_usage(logger, days, backend, model)
    await logger.close()


@app.command()
def reset(
    port: int = typer.Option(8046, "--port", "-p", help="代理服务端口"),
) -> None:
    """重置所有层级的熔断器和配额守卫（恢复使用最高优先级后端）."""
    import httpx

    try:
        resp = httpx.post(f"http://127.0.0.1:{port}/api/reset", timeout=5)
        if resp.status_code == 200:
            console.print("[green]所有层级的熔断器和配额守卫已重置[/green]")
        else:
            console.print(f"[red]重置失败: {resp.status_code}[/red]")
    except httpx.ConnectError:
        console.print("[red]代理服务未运行[/red]")
