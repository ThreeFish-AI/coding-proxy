"""使用统计查询与展示."""

from __future__ import annotations

from typing import TYPE_CHECKING

from rich.console import Console
from rich.table import Table

from .db import TokenLogger

if TYPE_CHECKING:
    from ..pricing import PricingCache


def _format_model_display(model_value: str | None) -> str:
    """格式化模型显示，处理 None 或空值."""
    if not model_value or model_value.strip() == "":
        return "[dim]<未知>[/dim]"
    return model_value


def _format_tokens(n: int) -> str:
    """将 Token 数量格式化为 K/M/B 计量单位显示（最多 2 位小数）."""
    if n >= 1_000_000_000:
        return f"{n / 1_000_000_000:.2f}".rstrip("0").rstrip(".") + "B"
    if n >= 1_000_000:
        return f"{n / 1_000_000:.2f}".rstrip("0").rstrip(".") + "M"
    if n >= 1_000:
        return f"{n / 1_000:.2f}".rstrip("0").rstrip(".") + "K"
    return str(n)


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


async def show_usage(
    logger: TokenLogger,
    days: int = 7,
    backend: str | None = None,
    model: str | None = None,
    pricing_cache: "PricingCache | None" = None,
) -> None:
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
    table.add_column("缓存创建 Token", justify="right", style="dim blue")
    table.add_column("缓存读取 Token", justify="right", style="dim cyan")
    table.add_column("总 Token", justify="right", style="bold white")
    table.add_column("Cost (USD)", justify="right", style="bold green")
    table.add_column("平均耗时(ms)", justify="right")

    for row in rows:
        total_input = row.get("total_input", 0) or 0
        total_output = row.get("total_output", 0) or 0
        total_cache_creation = row.get("total_cache_creation", 0) or 0
        total_cache_read = row.get("total_cache_read", 0) or 0
        total_tokens = total_input + total_output + total_cache_creation + total_cache_read

        backend_name = str(row.get("backend", ""))
        model_served = str(row.get("model_served", ""))
        if pricing_cache is not None:
            cost = pricing_cache.compute_cost(
                backend_name, model_served,
                total_input, total_output, total_cache_creation, total_cache_read,
            )
            cost_str = f"${cost:.4f}" if cost is not None else "-"
        else:
            cost_str = "-"

        table.add_row(
            str(row.get("date", "")),
            backend_name,
            str(row.get("model_requested", "")),
            model_served,
            str(row.get("total_requests", 0)),
            _format_tokens(total_input),
            _format_tokens(total_output),
            _format_tokens(total_cache_creation),
            _format_tokens(total_cache_read),
            _format_tokens(total_tokens),
            cost_str,
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
