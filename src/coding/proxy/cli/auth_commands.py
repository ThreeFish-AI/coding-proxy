"""CLI Auth 子命令 — OAuth 登录、状态、重认证与登出."""

from __future__ import annotations

import asyncio
import inspect
import logging
from pathlib import Path

import typer
from rich.console import Console

from ..config.loader import load_config

app = typer.Typer(name="auth", help="管理 OAuth 登录凭证")
console = Console()
logger = logging.getLogger(__name__)


def _build_token_store(cfg_path: Path | None = None):
    """按配置解析 Token Store 路径并完成加载."""
    from ..auth.store import TokenStoreManager

    cfg = load_config(cfg_path)
    store = TokenStoreManager(
        store_path=Path(cfg.auth.token_store_path)
        if cfg.auth.token_store_path
        else None,
    )
    store.load()
    logger.debug(
        "OAuth token store loaded from config path: %s", cfg.auth.token_store_path
    )
    return cfg, store


# ── Auth 子命令 ─────────────────────────────────────────────


@app.command("login")
def auth_login(
    provider: str | None = typer.Option(
        None, "--provider", "-p", help="指定 provider (github/google)"
    ),
) -> None:
    """执行 OAuth 浏览器登录."""
    asyncio.run(_run_auth_login(provider))


async def _run_auth_login(provider: str | None) -> None:
    from ..auth.providers.github import GitHubDeviceFlowProvider
    from ..auth.providers.google import GoogleOAuthProvider

    cfg, store = _build_token_store()

    providers = []
    if provider == "github":
        providers = [("github", GitHubDeviceFlowProvider())]
    elif provider == "google":
        providers = [
            (
                "google",
                GoogleOAuthProvider(
                    client_id=cfg.auth.google_client_id,
                    client_secret=cfg.auth.google_client_secret,
                ),
            )
        ]
    elif provider is None:
        providers = [
            ("github", GitHubDeviceFlowProvider()),
            (
                "google",
                GoogleOAuthProvider(
                    client_id=cfg.auth.google_client_id,
                    client_secret=cfg.auth.google_client_secret,
                ),
            ),
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
        except Exception as exc:
            console.print(f"[red]{name} 登录失败: {exc}[/red]")
        finally:
            await prov.close()


@app.command("status")
def auth_status() -> None:
    """查看已登录的 OAuth 凭证状态."""
    _, store = _build_token_store()

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


@app.command("reauth")
def auth_reauth(
    provider: str = typer.Argument(..., help="provider 名称 (github/google)"),
    port: int = typer.Option(3392, "--port", "-p", help="代理服务端口"),
) -> None:
    """触发运行中代理的 OAuth 重认证."""
    import httpx as _httpx

    try:
        resp = _httpx.post(f"http://127.0.0.1:{port}/api/reauth/{provider}", timeout=5)
        if resp.status_code == 202:
            console.print(
                f"[green]{provider} 重认证已触发，请在浏览器中完成登录[/green]"
            )
        elif resp.status_code == 404:
            console.print("[red]重认证不可用（代理未启用对应后端）[/red]")
        else:
            console.print(f"[red]触发失败: {resp.status_code} {resp.text}[/red]")
    except _httpx.ConnectError:
        console.print("[red]代理服务未运行[/red]")


@app.command("logout")
def auth_logout(
    provider: str | None = typer.Option(
        None, "--provider", "-p", help="指定 provider（不指定则全部登出）"
    ),
) -> None:
    """清除已存储的 OAuth 凭证."""
    _, store = _build_token_store()

    if provider:
        store.remove(provider)
        console.print(f"[green]已登出 {provider}[/green]")
    else:
        for name in store.list_providers():
            store.remove(name)
        console.print("[green]已登出所有 provider[/green]")


# ── 自动登录辅助 ─────────────────────────────────────────────
async def auto_login_if_needed(cfg_path: Path | None) -> None:
    """检查各 Provider 是否缺少凭证，自动触发浏览器登录.

    仅对已启用、且未在 config 中显式提供凭证的 Tier 做检查。
    对 Google/Antigravity，若本地存在 refresh_token 且 access_token 过期，
    优先执行静默刷新，避免每次启动都重新走浏览器 OAuth。

    三阶段检查:
    1. needs_login() — 快速本地判断（无凭证或已过期且无 refresh_token）
    2. refresh()    — Google access_token 过期且存在 refresh_token 时静默刷新
    3. validate()   — 网络验证已有凭证是否仍有效（仅在有凭证且未刷新时触发）
    """
    from ..auth.providers.github import GitHubDeviceFlowProvider
    from ..auth.providers.google import GoogleOAuthProvider

    cfg, store = _build_token_store(cfg_path)

    async def _resolve_needs_login(provider, tokens) -> bool:
        result = provider.needs_login(tokens)
        if inspect.isawaitable(result):
            return bool(await result)
        return bool(result)

    # --- GitHub / Copilot ---
    if cfg.copilot.enabled and not cfg.copilot.github_token:
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
            console.print(
                "[bold cyan]Copilot 层缺少有效凭证，启动 GitHub OAuth 登录...[/bold cyan]"
            )
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
    if cfg.antigravity.enabled and not cfg.antigravity.refresh_token:
        tokens = store.get("google")
        prov = GoogleOAuthProvider(
            client_id=cfg.auth.google_client_id,
            client_secret=cfg.auth.google_client_secret,
        )
        needs = await _resolve_needs_login(prov, tokens)
        try:
            if not needs and tokens.is_expired and tokens.refresh_token:
                logger.info(
                    "Google access_token 已过期，尝试使用 refresh_token 静默刷新"
                )
                try:
                    tokens = await prov.refresh(tokens)
                    store.set("google", tokens)
                    logger.info("Google refresh_token 静默刷新成功")
                except Exception as exc:
                    logger.warning(
                        "Google refresh_token 静默刷新失败，回退交互登录: %s", exc
                    )
                    console.print(
                        "[bold cyan]Antigravity 凭证刷新失败，启动 Google OAuth 登录...[/bold cyan]"
                    )
                    tokens = await prov.login()
                    store.set("google", tokens)
                    console.print("[green]Google 登录成功[/green]")
            elif not needs and tokens.has_credentials:
                try:
                    if not await prov.validate(tokens):
                        needs = True
                except Exception:
                    pass

            if needs:
                console.print(
                    "[bold cyan]Antigravity 层缺少有效凭证，启动 Google OAuth 登录...[/bold cyan]"
                )
                tokens = await prov.login()
                store.set("google", tokens)
                console.print("[green]Google 登录成功[/green]")
        except Exception as exc:
            console.print(f"[red]Google 登录失败: {exc}[/red]")
        finally:
            await prov.close()


__all__ = ["app", "auto_login_if_needed"]
