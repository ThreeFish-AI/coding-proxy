"""使用统计查询与展示."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import TYPE_CHECKING

from rich.console import Console
from rich.table import Table

from .db import TimePeriod, TokenLogger

if TYPE_CHECKING:
    from ..pricing import PricingTable


# ── 时间维度 → 表格标题 ──────────────────────────────────────

_PERIOD_TITLES: dict[TimePeriod, str] = {
    TimePeriod.DAY: "日",
    TimePeriod.WEEK: "周",
    TimePeriod.MONTH: "月",
    TimePeriod.TOTAL: "全部",
}


def _week_date_range(count: int) -> str:
    """计算最近第 count 周的周一～周日日期范围字符串.

    count=1 表示本周，count=2 表示上周，以此类推。

    Returns:
        格式为 ``YYYY-MM-DD ～ YYYY-MM-DD`` 的日期范围字符串。
    """
    today = datetime.now().date()
    # 本周周一
    this_monday = today - timedelta(days=today.weekday())
    # 目标周的周一
    target_monday = this_monday - timedelta(weeks=count - 1)
    target_sunday = target_monday + timedelta(days=6)
    return (
        f"{target_monday.strftime('%Y-%m-%d')} ～ {target_sunday.strftime('%Y-%m-%d')}"
    )


def _build_title(period: TimePeriod, count: int) -> str:
    """根据时间维度构建表格标题.

    WEEK 维度会附加具体日期范围（如 ``2026-04-07 ～ 2026-04-13``），
    其他维度仅显示统计周期标签。
    """
    if period is TimePeriod.TOTAL:
        return "Token 使用统计（全部）"
    label = _PERIOD_TITLES[period]
    base = f"Token 使用统计（最近 {count} {label}"
    if period is TimePeriod.WEEK:
        base += f"：{_week_date_range(count)}"
    return base + "）"


# ── 格式化工具 ───────────────────────────────────────────────


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


# ── 日期列名 ─────────────────────────────────────────────────

_PERIOD_DATE_LABELS: dict[TimePeriod, str] = {
    TimePeriod.DAY: "日期",
    TimePeriod.WEEK: "周",
    TimePeriod.MONTH: "月",
    TimePeriod.TOTAL: "维度",
}


# ── 主展示函数 ───────────────────────────────────────────────


async def show_usage(
    logger: TokenLogger,
    *,
    vendor: str | list[str] | None = None,
    model: str | list[str] | None = None,
    pricing_table: PricingTable | None = None,
    period: TimePeriod = TimePeriod.DAY,
    count: int = 7,
) -> None:
    """展示 Token 使用统计."""
    console = Console()
    rows = await logger.query_usage(
        period=period, count=count, vendor=vendor, model=model
    )

    if not rows:
        console.print("[yellow]暂无使用记录[/yellow]")
        return

    table = Table(title=_build_title(period, count))
    date_label = _PERIOD_DATE_LABELS[period]
    table.add_column(date_label, style="cyan")
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

    # ── 汇总累计变量 ──────────────────────────────────────────
    sum_requests = 0
    sum_input = 0
    sum_output = 0
    sum_cache_creation = 0
    sum_cache_read = 0
    weighted_duration_sum = 0.0  # Σ(avg_duration_ms × total_requests)
    cost_totals: dict = {}  # currency → float

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
        cost_value = None
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

        # 累加汇总
        total_requests_row = row.get("total_requests", 0) or 0
        sum_requests += total_requests_row
        sum_input += total_input
        sum_output += total_output
        sum_cache_creation += total_cache_creation
        sum_cache_read += total_cache_read
        weighted_duration_sum += (
            row.get("avg_duration_ms", 0) or 0
        ) * total_requests_row
        if cost_value is not None:
            cur = cost_value.currency
            cost_totals[cur] = cost_totals.get(cur, 0.0) + cost_value.amount

        date_value = row.get("date") or ""
        table.add_row(
            str(date_value),
            vendor_name,
            _format_model_display(row.get("model_requested")),
            model_served,
            str(total_requests_row),
            _format_tokens(total_input),
            _format_tokens(total_output),
            _format_tokens(total_cache_creation),
            _format_tokens(total_cache_read),
            _format_tokens(total_tokens),
            cost_str,
            str(int(row.get("avg_duration_ms", 0) or 0)),
        )

    # ── 汇总行 ───────────────────────────────────────────────
    table.add_section()

    sum_tokens = sum_input + sum_output + sum_cache_creation + sum_cache_read
    avg_duration = int(weighted_duration_sum / sum_requests) if sum_requests else 0

    if cost_totals:
        total_cost_str = " + ".join(
            f"{cur.symbol}{amt:.4f}" for cur, amt in cost_totals.items()
        )
    else:
        total_cost_str = "-"

    table.add_row(
        "[bold]总计[/bold]",
        "",
        "",
        "",
        f"[bold]{sum_requests}[/bold]",
        f"[bold]{_format_tokens(sum_input)}[/bold]",
        f"[bold]{_format_tokens(sum_output)}[/bold]",
        f"[bold]{_format_tokens(sum_cache_creation)}[/bold]",
        f"[bold]{_format_tokens(sum_cache_read)}[/bold]",
        f"[bold]{_format_tokens(sum_tokens)}[/bold]",
        f"[bold]{total_cost_str}[/bold]",
        f"[bold]{avg_duration}[/bold]",
    )

    console.print(table)

    # 故障转移来源汇总（使用与主查询相同的时间范围）
    failover_days = _period_to_days(period, count)
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


def _period_to_days(period: TimePeriod, count: int) -> int | None:
    """将 TimePeriod + count 近似转换为天数（供 query_failover_stats 使用）.

    Returns:
        天数，或 ``None`` 表示全量查询。
    """
    if period is TimePeriod.TOTAL:
        return None
    if period is TimePeriod.MONTH:
        return count * 31  # 粗略近似，保证覆盖范围
    if period is TimePeriod.WEEK:
        return count * 7
    return max(1, count)  # DAY
