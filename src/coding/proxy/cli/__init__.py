"""CLI 入口 — Typer 命令行工具.

Auth 子命令已正交提取至 :mod:`.auth_commands`.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import TYPE_CHECKING

import typer

if TYPE_CHECKING:
    from ..config.schema import ProxyConfig

from rich.console import Console

from ..config.loader import load_config
from ..logging.db import TimePeriod, TokenLogger
from ..logging.stats import show_usage
from .auth_commands import app as auth_app
from .auth_commands import auto_login_if_needed as _auto_login_if_needed

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
        store_path=Path(cfg.auth.token_store_path)
        if cfg.auth.token_store_path
        else None,
    )
    store.load()
    logger.debug(
        "OAuth token store loaded from config path: %s", cfg.auth.token_store_path
    )
    return cfg, store


def _resolve_period(
    *,
    days: int = 7,
    week: int | None = None,
    month: int | None = None,
    total: bool = False,
) -> tuple[TimePeriod, int]:
    """将互斥的时间维度标志解析为 (TimePeriod, count) 元组.

    优先级: ``-t`` > ``-m`` > ``-w`` > ``-d``。

    Returns:
        ``(period, count)`` 元组。``count`` 表示查询最近第 N 个周期的数据。
    """
    if total:
        return TimePeriod.TOTAL, 1
    if month is not None:
        return TimePeriod.MONTH, max(1, month)
    if week is not None:
        return TimePeriod.WEEK, max(1, week)
    return TimePeriod.DAY, max(1, days)


# ── 主命令 ─────────────────────────────────────────────────────


@app.command()
def start(
    config: str | None = typer.Option(None, "--config", "-c", help="配置文件路径"),
    port: int | None = typer.Option(None, "--port", "-p", help="监听端口"),
    host: str | None = typer.Option(None, "--host", "-h", help="监听地址"),
) -> None:
    """启动代理服务."""
    import uvicorn

    from ..server.app import create_app
    from .banner import print_banner

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

    # 打印启动品牌横幅
    print_banner(console, host=cfg.server.host, port=cfg.server.port)

    # 解析文件日志路径：未显式配置时使用默认值
    _file_path: str | None = cfg.logging.file or "coding-proxy.log"
    uvicorn.run(
        fastapi_app,
        host=cfg.server.host,
        port=cfg.server.port,
        log_config=build_log_config(
            level=cfg.logging.level,
            file_path=_file_path,
            max_bytes=cfg.logging.max_bytes,
            backup_count=cfg.logging.backup_count,
        ),
    )


@app.command()
def status(
    port: int = typer.Option(3392, "--port", "-p", help="代理服务端口"),
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
                console.print(
                    f"  [cyan]熔断器:[/] {cb.get('state', 'unknown')}  失败={cb.get('failure_count', 0)}"
                )
            qg = tier_info.get("quota_guard")
            if qg:
                console.print(
                    f"  [cyan]配额:[/] {qg.get('state', 'unknown')}  {qg.get('usage_percent', 0)}% ({qg.get('window_usage_tokens', 0)}/{qg.get('budget_tokens', 0)})"
                )
    except httpx.ConnectError:
        console.print("[red]代理服务未运行[/red]")


@app.command()
def usage(
    days: int = typer.Option(7, "--days", "-d", help="统计天数（与 -w/-m/-t 互斥）"),
    week: int | None = typer.Option(
        None, "--week", "-w", help="最近第 N 周统计（按周聚合，默认 1）"
    ),
    month: int | None = typer.Option(
        None, "--month", "-m", help="最近第 N 月统计（按月聚合，默认 1）"
    ),
    total: bool = typer.Option(False, "--total", "-t", help="统计全部历史记录"),
    vendor: str | None = typer.Option(None, "--vendor", "-v", help="过滤供应商"),
    model: str | None = typer.Option(
        None, "--model", help="过滤实际服务模型（model_served），逗号分隔可指定多个"
    ),
    db_path: str | None = typer.Option(None, "--db", help="数据库路径"),
) -> None:
    """查看 Token 使用统计.

    时间维度（互斥，优先级 -t > -m > -w > -d）：

      \b
      -d 7         最近 7 天（默认，按日聚合）
      -w [N]       最近第 N 周（按周聚合，默认 1＝本周）
      -m [N]       最近第 N 月（按月聚合，默认 1＝本月）
      -t           全部历史（按供应商+模型聚合）
    """
    period, count = _resolve_period(days=days, week=week, month=month, total=total)
    cfg = load_config(Path(db_path) if db_path else None)
    token_logger = TokenLogger(cfg.db_path)
    # 解析逗号分隔的多 vendor（如 "anthropic,zhipu" → ["anthropic", "zhipu"]）
    vendor_filter: str | list[str] | None = None
    if vendor:
        parts = [v.strip() for v in vendor.split(",") if v.strip()]
        vendor_filter = parts[0] if len(parts) == 1 else parts
    # 解析逗号分隔的多 model（如 "glm-5,glm-5.1" → ["glm-5", "glm-5.1"]）
    model_filter: str | list[str] | None = None
    if model:
        parts = [m.strip() for m in model.split(",") if m.strip()]
        model_filter = parts[0] if len(parts) == 1 else parts
    asyncio.run(
        _run_usage(token_logger, period, count, vendor_filter, model_filter, cfg)
    )


async def _run_usage(
    token_logger: TokenLogger,
    period: TimePeriod,
    count: int,
    vendor: str | list[str] | None,
    model: str | list[str] | None,
    cfg: ProxyConfig,
) -> None:
    from ..pricing import PricingTable

    await token_logger.init()
    pricing_table = PricingTable(cfg.pricing)
    await show_usage(
        token_logger,
        vendor=vendor,
        model=model,
        pricing_table=pricing_table,
        period=period,
        count=count,
    )
    await token_logger.close()


@app.command()
def reset(
    port: int = typer.Option(3392, "--port", "-p", help="代理服务端口"),
    vendor: str | None = typer.Option(
        None,
        "--vendor",
        "-v",
        help="提升/重排序 vendor 优先级（单个或逗号分隔多个）",
    ),
) -> None:
    """重置所有层级的熔断器和配额守卫.

    可通过 -v 指定运行时 N-tier 链路重排序：

    \b
      -v zhipu               提升 zhipu 到最高优先级
      -v zhipu,anthropic     替换整个 N-tier 链路顺序
    """
    import httpx

    # 构建请求 body
    json_body: dict | None = None
    if vendor:
        parts = [v.strip() for v in vendor.split(",") if v.strip()]
        if parts:
            json_body = {"vendors": parts}

    try:
        resp = httpx.post(
            f"http://127.0.0.1:{port}/api/reset",
            json=json_body,
            timeout=5,
        )
        if resp.status_code == 200:
            data = resp.json()
            console.print("[green]所有层级的熔断器和配额守卫已重置[/green]")
            tier_order = data.get("tier_order")
            if tier_order:
                order_str = " → ".join(tier_order)
                console.print(f"[cyan]当前链路顺序:[/] {order_str}")
        else:
            try:
                err = resp.json()
                msg = err.get("error", {}).get("message", resp.text)
            except Exception:
                msg = resp.text
            console.print(f"[red]重置失败: {msg}[/red]")
    except httpx.ConnectError:
        console.print("[red]代理服务未运行[/red]")


def _resolve_config_path(config: str | Path | None = None) -> Path | None:
    """标准化配置路径输入."""
    if config is None:
        return None
    return config if isinstance(config, Path) else Path(config)


__all__ = ["app", "_build_token_store"]
