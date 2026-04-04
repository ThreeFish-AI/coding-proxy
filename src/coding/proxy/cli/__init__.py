"""CLI 入口 — Typer 命令行工具.

Auth 子命令已正交提取至 :mod:`.auth_commands`.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import TYPE_CHECKING, Optional

import typer

if TYPE_CHECKING:
    from ..config.schema import ProxyConfig

from rich.console import Console

from ..config.loader import load_config
from ..logging.db import TokenLogger
from ..logging.stats import show_usage
from .auth_commands import app as auth_app, auto_login_if_needed as _auto_login_if_needed

app = typer.Typer(name="coding-proxy", help="Claude Code 多供应商智能代理服务")
console = Console()
logger = logging.getLogger(__name__)

# 注册 Auth 子应用
app.add_typer(auth_app, name="auth")


def _build_token_store(cfg_path: Path | None = None):
    """按配置解析 Token Store 路径并完成加载."""
    from ..auth.store import TokenStoreManager

    cfg = load_config(cfg_path)
    store = TokenStoreManager(
        store_path=Path(cfg.auth.token_store_path) if cfg.auth.token_store_path else None,
    )
    store.load()
    logger.debug("OAuth token store loaded from config path: %s", cfg.auth.token_store_path)
    return cfg, store


# ── 主命令 ─────────────────────────────────────────────────────


@app.command()
def start(
    config: Optional[str] = typer.Option(None, "--config", "-c", help="配置文件路径"),
    port: Optional[int] = typer.Option(None, "--port", "-p", help="监听端口"),
    host: Optional[str] = typer.Option(None, "--host", "-h", help="监听地址"),
) -> None:
    """启动代理服务."""
    import uvicorn

    from ..server.app import create_app

    cfg_path = _resolve_config_path(config)
    cfg = load_config(cfg_path)

    if port:
        cfg.server.port = port
    if host:
        cfg.server.host = host

    # 自动登录检查
    asyncio.run(_auto_login_if_needed(cfg_path))

    from ..logging import build_log_config

    fastapi_app = create_app(cfg)
    uvicorn.run(
        fastapi_app,
        host=cfg.server.host,
        port=cfg.server.port,
        log_config=build_log_config(cfg.logging.level),
    )


@app.command()
def status(
    port: int = typer.Option(8046, "--port", "-p", help="代理服务端口"),
) -> None:
    """查看代理状态和当前活跃供应商."""
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
    backend: Optional[str] = typer.Option(None, "--backend", "-b", help="过滤供应商"),
    model: Optional[str] = typer.Option(None, "--model", "-m", help="过滤请求模型"),
    db_path: Optional[str] = typer.Option(None, "--db", help="数据库路径"),
) -> None:
    """查看 Token 使用统计."""
    cfg = load_config(Path(db_path) if db_path else None)
    token_logger = TokenLogger(cfg.db_path)
    asyncio.run(_run_usage(token_logger, days, backend, model, cfg))


async def _run_usage(token_logger: TokenLogger, days: int, backend: str | None,
                     model: str | None, cfg: "ProxyConfig") -> None:
    from ..pricing import PricingTable
    await token_logger.init()
    pricing_table = PricingTable(cfg.pricing)
    await show_usage(token_logger, days, backend, model, pricing_table)
    await token_logger.close()


@app.command()
def reset(
    port: int = typer.Option(8046, "--port", "-p", help="代理服务端口"),
) -> None:
    """重置所有层级的熔断器和配额守卫（恢复使用最高优先级供应商）."""
    import httpx

    try:
        resp = httpx.post(f"http://127.0.0.1:{port}/api/reset", timeout=5)
        if resp.status_code == 200:
            console.print("[green]所有层级的熔断器和配额守卫已重置[/green]")
        else:
            console.print(f"[red]重置失败: {resp.status_code}[/red]")
    except httpx.ConnectError:
        console.print("[red]代理服务未运行[/red]")


def _resolve_config_path(config: str | Path | None = None) -> Path | None:
    """标准化配置路径输入."""
    if config is None:
        return None
    return config if isinstance(config, Path) else Path(config)


__all__ = ["app", "_build_token_store"]
