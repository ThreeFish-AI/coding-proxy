"""使用统计查询与展示."""

from __future__ import annotations

from typing import TYPE_CHECKING

from rich.console import Console
from rich.table import Table

from .db import TimePeriod, TokenLogger

if TYPE_CHECKING:
    from ..pricing import PricingTable


# ── 时间维度 → 显示配置 ───────────────────────────────────────

_PERIOD_LABEL: dict[TimePeriod, tuple[str, str]] = {
    # (date_column_header, period_unit_label)
    TimePeriod.DAY: ("日期", "天"),
    TimePeriod.WEEK: ("周", "周"),
    TimePeriod.MONTH: ("月份", "月"),
    TimePeriod.TOTAL: ("", ""),
}

_PERIOD_DEFAULT_COUNT: dict[TimePeriod, int] = {
    TimePeriod.DAY: 7,
    TimePeriod.WEEK: 4,
    TimePeriod.MONTH: 3,
    TimePeriod.TOTAL: 0,
}


# ── 格式化工具 ────────────────────────────────────────────────


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
    return any(pair[0] != pair[1] for pair in model_pairs if pair[0] and pair[1])


# ── 核心展示函数 ──────────────────────────────────────────────


async def show_usage(
    logger: TokenLogger,
    days: int | None = 7,
    vendor: str | None = None,
    model: str | None = None,
    pricing_table: PricingTable | None = None,
    *,
    period: TimePeriod = TimePeriod.DAY,
    count: int | None = None,
) -> None:
    """展示 Token 使用统计.

    Args:
        logger: Token 日志记录器。
        days: 向后兼容参数 — 等价于 ``period=DAY, count=days``。
        vendor: 过滤供应商。
        model: 过滤请求模型。
        pricing_table: 定价表（用于费用计算）。
        period: 时间维度（日/周/月/全量）。
        count: ``period`` 维度下的数量。为 ``None`` 时取维度默认值。
    """
    console = Console()

    if count is None:
        count = days if period is TimePeriod.DAY else _PERIOD_DEFAULT_COUNT[period]

    rows = await logger.query_usage(
        period=period, count=count, vendor=vendor, model=model
    )

    if not rows:
        console.print("[yellow]暂无使用记录[/yellow]")
        return

    date_col, unit = _PERIOD_LABEL[period]

    # 构建表头
    title = (
        "Token 使用统计（全部）"
        if period is TimePeriod.TOTAL
        else f"Token 使用统计（最近 {count} {unit}）"
    )
    table = Table(title=title)
    if date_col:
        table.add_column(date_col, style="cyan")
    table.add_column("供应商", style="green")
    table.add_column("请求模型", style="magenta")
    table.add_column("实际模型", style="yellow")
    table.add_column("请求数", justify="right")
    table.add_column("输入 Token", justify="right", style="blue")
    table.add_column("输出 Token", justify="right", style="blue")
    table.add_column("缓存创建 Token", justify="right", style="dim blue")
    table.add_column("缓存读取 Token", justify="right", style="dim cyan")
    table.add_column("总 Token", justify="right", style="bold white")
    table.add_column("Cost", justify="right", style="bold green")
    table.add_column("平均耗时(ms)", justify="right")

    for row in rows:
        total_input = row.get("total_input", 0) or 0
        total_output = row.get("total_output", 0) or 0
        total_cache_creation = row.get("total_cache_creation", 0) or 0
        total_cache_read = row.get("total_cache_read", 0) or 0
        total_tokens = (
            total_input + total_output + total_cache_creation + total_cache_read
        )

        vendor_name = str(row.get("vendor", ""))
        model_served = str(row.get("model_served", ""))
        if pricing_table is not None:
            cost_value = pricing_table.compute_cost(
                vendor_name,
                model_served,
                total_input,
                total_output,
                total_cache_creation,
                total_cache_read,
            )
            cost_str = cost_value.format() if cost_value is not None else "-"
        else:
            cost_str = "-"

        row_data: list[str] = []
        if date_col:
            row_data.append(str(row.get("date", "") or ""))
        row_data.extend(
            [
                vendor_name,
                _format_model_display(row.get("model_requested")),
                model_served,
                str(row.get("total_requests", 0)),
                _format_tokens(total_input),
                _format_tokens(total_output),
                _format_tokens(total_cache_creation),
                _format_tokens(total_cache_read),
                _format_tokens(total_tokens),
                cost_str,
                str(int(row.get("avg_duration_ms", 0) or 0)),
            ]
        )
        table.add_row(*row_data)

    console.print(table)

    # 故障转移来源汇总
    failover_days = count if period is TimePeriod.DAY else None
    failover_stats = await logger.query_failover_stats(days=failover_days)
    if failover_stats:
        console.print()
        ft_table = Table(title="故障转移来源明细")
        ft_table.add_column("来源", style="yellow")
        ft_table.add_column("目标", style="green")
        ft_table.add_column("次数", justify="right", style="red")
        for stat in failover_stats:
            source = stat.get("failover_from") or "unknown"
            target = stat.get("vendor", "")
            count_val = stat.get("count", 0)
            ft_table.add_row(source, target, str(count_val))
        console.print(ft_table)
