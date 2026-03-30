"""使用统计查询与展示."""

from __future__ import annotations

from rich.console import Console
from rich.table import Table

from .db import TokenLogger


async def show_usage(logger: TokenLogger, days: int = 7, backend: str | None = None) -> None:
    """展示 Token 使用统计."""
    console = Console()
    rows = await logger.query_daily(days=days, backend=backend)

    if not rows:
        console.print("[yellow]暂无使用记录[/yellow]")
        return

    table = Table(title=f"Token 使用统计（最近 {days} 天）")
    table.add_column("日期", style="cyan")
    table.add_column("后端", style="green")
    table.add_column("请求数", justify="right")
    table.add_column("输入 Token", justify="right", style="blue")
    table.add_column("输出 Token", justify="right", style="blue")
    table.add_column("故障转移", justify="right", style="red")
    table.add_column("平均耗时(ms)", justify="right")

    for row in rows:
        table.add_row(
            str(row.get("date", "")),
            str(row.get("backend", "")),
            str(row.get("total_requests", 0)),
            str(row.get("total_input", 0)),
            str(row.get("total_output", 0)),
            str(row.get("total_failovers", 0)),
            str(int(row.get("avg_duration_ms", 0) or 0)),
        )

    console.print(table)
