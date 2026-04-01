"""使用统计查询与展示."""

from __future__ import annotations

from rich.console import Console
from rich.table import Table

from .db import TokenLogger


def _format_model_display(model_value: str | None) -> str:
    """格式化模型显示，处理 None 或空值."""
    if not model_value or model_value.strip() == "":
        return "[dim]<未知>[/dim]"
    return model_value


def _detect_model_variants(failover_stats: list[dict]) -> bool:
    """检测是否存在模型差异，用于决定是否建议详细模式."""
    if not failover_stats or "model_requested" not in failover_stats[0]:
        return False

    # 计算唯一的模型对
    model_pairs = {
        (stat.get("model_requested", ""), stat.get("model_served", ""))
        for stat in failover_stats
    }
    # 检查是否存在模型映射（请求模型与实际模型不同）
    return any(
        pair[0] != pair[1]
        for pair in model_pairs
        if pair[0] and pair[1]
    )


async def show_usage(logger: TokenLogger, days: int = 7, backend: str | None = None,
                     model: str | None = None) -> None:
    """展示 Token 使用统计."""
    console = Console()
    rows = await logger.query_daily(days=days, backend=backend, model=model)

    if not rows:
        console.print("[yellow]暂无使用记录[/yellow]")
        return

    table = Table(title=f"Token 使用统计（最近 {days} 天）")
    table.add_column("日期", style="cyan")
    table.add_column("后端", style="green")
    table.add_column("请求模型", style="magenta")
    table.add_column("实际模型", style="yellow")
    table.add_column("请求数", justify="right")
    table.add_column("输入 Token", justify="right", style="blue")
    table.add_column("输出 Token", justify="right", style="blue")
    table.add_column("平均耗时(ms)", justify="right")

    for row in rows:
        table.add_row(
            str(row.get("date", "")),
            str(row.get("backend", "")),
            str(row.get("model_requested", "")),
            str(row.get("model_served", "")),
            str(row.get("total_requests", 0)),
            str(row.get("total_input", 0)),
            str(row.get("total_output", 0)),
            str(int(row.get("avg_duration_ms", 0) or 0)),
        )

    console.print(table)

    # 故障转移来源汇总
    failover_stats = await logger.query_failover_stats(days=days)
    if failover_stats:
        console.print()
        ft_table = Table(title="故障转移来源明细")
        ft_table.add_column("来源", style="yellow")
        ft_table.add_column("目标", style="green")
        ft_table.add_column("次数", justify="right", style="red")
        for stat in failover_stats:
            source = stat.get("failover_from") or "unknown"
            target = stat.get("backend", "")
            count = stat.get("count", 0)
            ft_table.add_row(source, target, str(count))
        console.print(ft_table)
