"""CLI 入口 — Typer 命令行工具."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Optional

import typer
from rich.console import Console

from .config.loader import load_config
from .logging.db import TokenLogger
from .logging.stats import show_usage

app = typer.Typer(name="coding-proxy", help="Claude Code 多后端智能代理服务")
console = Console()


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
        cb = data.get("circuit_breaker", {})
        console.print(f"[cyan]熔断器状态:[/] {cb.get('state', 'unknown')}")
        console.print(f"[green]主后端:[/] {data.get('primary', 'unknown')}")
        console.print(f"[green]备选后端:[/] {data.get('fallback', 'unknown')}")
        console.print(f"[blue]连续失败次数:[/] {cb.get('failure_count', 0)}")
        console.print(f"[blue]恢复超时(s):[/] {cb.get('current_recovery_seconds', 300)}")
    except httpx.ConnectError:
        console.print("[red]代理服务未运行[/red]")


@app.command()
def usage(
    days: int = typer.Option(7, "--days", "-d", help="统计天数"),
    backend: Optional[str] = typer.Option(None, "--backend", "-b", help="过滤后端"),
    db_path: Optional[str] = typer.Option(None, "--db", help="数据库路径"),
) -> None:
    """查看 Token 使用统计."""
    cfg = load_config(Path(db_path) if db_path else None)
    logger = TokenLogger(cfg.db_path)
    asyncio.run(_run_usage(logger, days, backend))


async def _run_usage(logger: TokenLogger, days: int, backend: str | None) -> None:
    await logger.init()
    await show_usage(logger, days, backend)
    await logger.close()


@app.command()
def reset(
    port: int = typer.Option(8046, "--port", "-p", help="代理服务端口"),
) -> None:
    """重置熔断器状态（恢复使用主后端）."""
    import httpx

    try:
        resp = httpx.post(f"http://127.0.0.1:{port}/api/reset", timeout=5)
        if resp.status_code == 200:
            console.print("[green]熔断器已重置为 CLOSED 状态[/green]")
        else:
            console.print(f"[red]重置失败: {resp.status_code}[/red]")
    except httpx.ConnectError:
        console.print("[red]代理服务未运行[/red]")
