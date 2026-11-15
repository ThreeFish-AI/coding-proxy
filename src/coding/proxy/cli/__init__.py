"""CLI 入口 — Typer 命令行工具.

Auth / Config 子命令已正交提取至 :mod:`.auth_commands` / :mod:`.config_commands`.
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

from ..logging.db import TimePeriod, TokenLogger
from ..logging.stats import show_usage
from .auth_commands import _build_token_store, _offline_config
from .auth_commands import app as auth_app
from .auth_commands import auto_login_if_needed as _auto_login_if_needed
from .config_commands import app as config_app

app = typer.Typer(
    name="coding-proxy",
    help="Claude Code 多供应商智能代理服务",
    pretty_exceptions_show_locals=False,
)
console = Console()
err_console = Console(stderr=True)
logger = logging.getLogger(__name__)

# 注册 Auth / Config 子应用
app.add_typer(auth_app, name="auth")
app.add_typer(config_app, name="config")

# 注册 Session 子应用
session_app = typer.Typer(
    name="session",
    help="管理 Session-Vendor 运行时绑定",
    pretty_exceptions_show_locals=False,
)
app.add_typer(session_app, name="session")


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
    """启动代理服务.

    uvicorn 运行 :class:`GenerationManager`：Dashboard 保存配置后进程内热切换，无需重启；
    存储的配置无效时以 degraded 模式启动（仅 Dashboard 与管理 API 可用）。
    """
    import uvicorn

    from ..config.importer import (
        ExplicitConfigMissingError,
        LegacyConfigError,
        StaleYamlError,
        boot,
    )
    from ..config.resolver import MODE_LEGACY
    from ..config.secrets import MasterKeyError
    from ..config.store import ConfigStoreError
    from ..server.app import create_app
    from ..server.generations import GenerationManager
    from .banner import print_banner

    cfg_path = _resolve_config_path(config)
    try:
        booted = boot(cfg_path)
    except (
        StaleYamlError,
        ExplicitConfigMissingError,
        LegacyConfigError,
        MasterKeyError,
        ConfigStoreError,
    ) as exc:
        err_console.print(f"[red]{exc}[/red]")
        raise typer.Exit(2) from None
    resolved = booted.resolved
    for notice in booted.notices:
        err_console.print(f"[yellow]{notice}[/yellow]")
    if resolved.mode == MODE_LEGACY:
        err_console.print("[yellow]配置导入失败，本次以只读方式运行旧 YAML[/yellow]")
    cfg = resolved.config
    if cfg is None:
        for error in resolved.errors:
            loc = ".".join(map(str, error["loc"])) or "<root>"
            err_console.print(f"[red]{loc}: {error['msg']}[/red]")
        err_console.print(
            "[red]存储的配置无效，以 degraded 模式启动（代理接口返回 503）；"
            "请在 Dashboard → Config 或用 `coding-proxy config` 修复[/red]"
        )

    from ..logging import build_log_config

    manager = GenerationManager(boot=booted, build=create_app)
    server_cfg, logging_cfg = manager.server, manager.logging
    if port:
        server_cfg.port = port
        resolved.sources["server.port"] = "cli"
    if host:
        server_cfg.host = host
        resolved.sources["server.host"] = "cli"

    # 自动登录检查（配置无效时跳过）
    if cfg is not None:
        asyncio.run(_auto_login_if_needed(cfg_path, cfg=cfg))

    # 打印启动品牌横幅与带 admin token 的 Dashboard 链接（仅控制台，不进日志）
    print_banner(console, host=server_cfg.host, port=server_cfg.port)
    _print_dashboard_url(server_cfg.host, server_cfg.port, server_cfg.allowed_hosts)

    # 解析文件日志路径：未设置时用默认值；显式空串 = 禁用文件日志（透传 build_log_config）
    _file_path: str | None = (
        logging_cfg.file if logging_cfg.file is not None else ".logs/coding-proxy.log"
    )
    uvicorn.run(
        manager,
        host=server_cfg.host,
        port=server_cfg.port,
        log_config=build_log_config(
            level=logging_cfg.level,
            file_path=_file_path,
            max_bytes=logging_cfg.max_bytes,
            backup_count=logging_cfg.backup_count,
        ),
        proxy_headers=False,
    )


def _dashboard_host(host: str) -> str:
    """监听地址 → 浏览器可访问的主机（通配地址改用 loopback，IPv6 加方括号）."""
    if host in ("", "0.0.0.0"):
        return "127.0.0.1"
    if host == "::":
        return "[::1]"
    return f"[{host}]" if ":" in host else host


def _dashboard_host_rejected(shown: str, allowed_hosts: list[str]) -> bool:
    """打印的主机会被管理面 Host 校验（403）拒绝.

    IP 字面量即监听地址，中间件按本机地址放行；主机名须在 loopback ∪ allowed_hosts 中。
    """
    import ipaddress

    from ..server import security

    parsed = security.split_host(shown)
    if parsed is None:
        return True
    name = parsed[0]
    try:
        ipaddress.ip_address(name)
        return False
    except ValueError:
        pass
    return name not in security.LOOPBACK_HOSTS | security.normalize_allowed_hosts(
        allowed_hosts
    )


def _print_dashboard_url(
    host: str, port: int, allowed_hosts: list[str] | None = None
) -> None:
    """``#admin=`` fragment 不随请求发送，token 不会出现在服务端访问日志."""
    import json

    from ..config.home import load_or_create_admin_token
    from ..config.store import ConfigStoreError

    shown = _dashboard_host(host)
    url = f"http://{shown}:{port}/dashboard?tab=config"
    try:
        url += f"#admin={load_or_create_admin_token()}"
    except (OSError, ConfigStoreError) as exc:
        err_console.print(
            f"[yellow]admin token 不可用（{type(exc).__name__}），"
            "Config 页需执行 `coding-proxy config token` 后解锁[/yellow]"
        )
    console.print(f"Dashboard: {url}", markup=False, highlight=False, soft_wrap=True)
    allowed = list(allowed_hosts or [])
    if _dashboard_host_rejected(shown, allowed):
        value = json.dumps([*allowed, shown.strip("[]")], ensure_ascii=False)
        err_console.print(
            f"主机 {shown} 不在 server.allowed_hosts 中，Dashboard 与管理 API 会返回 403；"
            f"请执行 coding-proxy config set server.allowed_hosts '{value}' 后重启，"
            f"或经 SSH 隧道访问 http://127.0.0.1:{port}/dashboard",
            style="yellow",
            markup=False,
            highlight=False,
            soft_wrap=True,
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
    cfg = _offline_config(decrypt=False)
    token_logger = TokenLogger(Path(db_path).expanduser() if db_path else cfg.db_path)
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


# ── Session 子命令 ───────────────────────────────────────────────


@session_app.command("bind")
def session_bind(
    key: str = typer.Option(..., "--key", "-k", help="Session key"),
    vendor: str = typer.Option(
        None, "--vendor", "-v", help="绑定 vendor（逗号分隔多个，与 --model 可组合）"
    ),
    model: str = typer.Option(
        None,
        "--model",
        "-m",
        help="绑定模型（须在 config 的 bindable_models 清单内，与 --vendor 可组合）",
    ),
    port: int = typer.Option(3392, "--port", "-p", help="代理服务端口"),
) -> None:
    """为指定 Session 绑定 vendor 优先级与可选的会话级模型覆盖."""
    import httpx as _httpx

    if not vendor and not model:
        console.print("[red]至少提供 --vendor 或 --model 之一[/red]")
        raise typer.Exit(1)

    payload: dict = {"session_key": key}
    if vendor:
        payload["vendors"] = [v.strip() for v in vendor.split(",") if v.strip()]
    if model:
        payload["model"] = model
    try:
        resp = _httpx.put(
            f"http://127.0.0.1:{port}/api/session-vendor",
            json=payload,
            timeout=5,
        )
        if resp.status_code == 200:
            data = resp.json()
            parts = []
            if data.get("model"):
                parts.append(f"model={data['model']}")
            if data.get("vendors"):
                parts.append(" → ".join(data["vendors"]))
            console.print(
                f"[green]绑定成功:[/] session [cyan]{key[:16]}…[/cyan] → "
                + " ".join(parts)
            )
        else:
            try:
                err = resp.json()
                msg = err.get("error", {}).get("message", resp.text)
            except Exception:
                msg = resp.text
            console.print(f"[red]绑定失败: {msg}[/red]")
    except _httpx.ConnectError:
        console.print("[red]代理服务未运行[/red]")


@session_app.command("unbind")
def session_unbind(
    key: str = typer.Option(..., "--key", "-k", help="Session key"),
    port: int = typer.Option(3392, "--port", "-p", help="代理服务端口"),
) -> None:
    """解除指定 Session 的 vendor 绑定."""
    from urllib.parse import quote

    import httpx as _httpx

    try:
        resp = _httpx.delete(
            f"http://127.0.0.1:{port}/api/session-vendor/{quote(key, safe='')}",
            timeout=5,
        )
        if resp.status_code == 200:
            console.print(f"[green]已解除绑定:[/] session [cyan]{key[:16]}…[/cyan]")
        elif resp.status_code == 404:
            console.print(f"[yellow]未找到绑定:[/] session [cyan]{key[:16]}…[/cyan]")
        else:
            console.print(f"[red]解除失败: {resp.status_code} {resp.text}[/red]")
    except _httpx.ConnectError:
        console.print("[red]代理服务未运行[/red]")


@session_app.command("list")
def session_list(
    port: int = typer.Option(3392, "--port", "-p", help="代理服务端口"),
) -> None:
    """列出所有运行时 Session-Vendor 绑定."""
    import httpx as _httpx

    try:
        resp = _httpx.get(
            f"http://127.0.0.1:{port}/api/session-vendor",
            timeout=5,
        )
        if resp.status_code == 200:
            data = resp.json()
            bindings = data.get("bindings", [])
            if not bindings:
                console.print("[dim]当前无运行时绑定[/dim]")
                return
            for b in bindings:
                key = b.get("session_key", "?")
                vendors = b.get("vendors", [])
                model = b.get("model")
                model_part = f"model={model} " if model else ""
                console.print(
                    f"  [cyan]{key[:24]}…[/cyan] → {model_part}" + " → ".join(vendors)
                )
        else:
            console.print(f"[red]查询失败: {resp.status_code} {resp.text}[/red]")
    except _httpx.ConnectError:
        console.print("[red]代理服务未运行[/red]")


def _resolve_config_path(config: str | Path | None = None) -> Path | None:
    """标准化配置路径输入."""
    if config is None:
        return None
    return config if isinstance(config, Path) else Path(config)


__all__ = ["app", "_build_token_store"]
