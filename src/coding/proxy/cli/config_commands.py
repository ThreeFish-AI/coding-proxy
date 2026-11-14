"""CLI Config 子命令 — config.db 的路径、导入导出、字段与密钥修改.

所有输出只含字段路径与脱敏信息；密钥值从不回显。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import typer
from rich.console import Console

from ..config.home import (
    admin_token_path,
    config_db_path,
    load_or_create_admin_token,
    master_key_path,
    proxy_home,
    write_private_atomic,
)
from ..config.importer import (
    ImportPlanError,
    LegacyConfigError,
    StoreNotReadyError,
    ensure_store_ready,
    require_migrated_or_empty,
)
from ..config.secrets import MasterKeyError
from ..config.service import (
    ConfigService,
    ConfigStoreMissing,
    ConfigValidationError,
)
from ..config.store import ConfigStoreError, RevisionConflict

app = typer.Typer(
    name="config",
    help="管理配置存储（$CODING_PROXY_HOME/config.db）",
    pretty_exceptions_show_locals=False,
)
console = Console()
err_console = Console(stderr=True)

_EXPECTED_ERRORS = (
    ConfigStoreError,
    ConfigValidationError,
    ConfigStoreMissing,
    ImportPlanError,
    LegacyConfigError,
    MasterKeyError,
    RevisionConflict,
    StoreNotReadyError,
)


def _fail(exc: Exception) -> typer.Exit:
    if isinstance(exc, ConfigValidationError):
        for error in exc.errors:
            loc = ".".join(map(str, error["loc"])) or "<root>"
            err_console.print(f"[red]{loc}: {error['msg']}[/red]")
    else:
        err_console.print(f"[red]{exc}[/red]")
    return typer.Exit(1)


def _print_pinned(paths: list[str]) -> None:
    for path in paths:
        console.print(
            f"[yellow]密钥 {path} 保持为空（不回落到包内默认环境变量）；如需使用，"
            f"请执行 `coding-proxy config set-secret {path} --default-env`[/yellow]"
        )


def _parse_value(raw: str) -> Any:
    """按 JSON 解析；非法 JSON 时按普通字符串处理."""
    try:
        return json.loads(raw)
    except ValueError:
        return raw


@app.command("path")
def config_path() -> None:
    """显示配置相关文件路径."""
    console.print(f"home:        {proxy_home()}")
    console.print(f"config.db:   {config_db_path()}")
    console.print(f"master.key:  {master_key_path()}")
    console.print(f"admin_token: {admin_token_path()}")


@app.command("export")
def config_export(
    out: Path | None = typer.Option(None, "--out", "-o", help="写入文件（0600）"),
) -> None:
    """导出当前有效配置为 YAML（密钥已脱敏）."""
    try:
        require_migrated_or_empty()
        text = ConfigService().export_yaml()
    except _EXPECTED_ERRORS as exc:
        raise _fail(exc) from None
    if out is None:
        sys.stdout.write(text)
        return
    write_private_atomic(out.expanduser(), text.encode("utf-8"))
    console.print(f"[green]已导出到 {out}[/green]")


@app.command("import")
def config_import(
    file: Path = typer.Argument(..., help="YAML 配置文件"),
    dry_run: bool = typer.Option(False, "--dry-run", help="只校验并显示计划，不写入"),
) -> None:
    """从 YAML 导入（整体替换当前覆盖）；``<redacted>`` 表示保留本地已存密钥."""
    if not file.is_file():
        err_console.print(f"[red]文件不存在: {file}[/red]")
        raise typer.Exit(1)
    try:
        summary = ConfigService().import_yaml(file, dry_run=dry_run)
    except _EXPECTED_ERRORS as exc:
        raise _fail(exc) from None
    title = "导入计划（dry-run）" if dry_run else "已导入"
    console.print(f"[bold]{title}[/bold]: {summary['source']}")
    console.print(f"  覆盖 sections: {', '.join(summary['sections']) or '（无）'}")
    if summary["removed_sections"]:
        console.print(f"  恢复默认: {', '.join(summary['removed_sections'])}")
    for path, kind in summary["secrets"].items():
        console.print(f"  密钥 {path}: {kind}")
    for warning in summary["warnings"]:
        console.print(f"  [yellow]{warning}[/yellow]")
    if summary["changes"]:
        verb = "将改变" if dry_run else "已改变"
        console.print(f"  [yellow]相对当前配置{verb}的字段:[/yellow]")
        for path in summary["changes"]:
            console.print(f"    {path}")
    elif not summary["diffs"]:
        console.print("  生效配置与当前一致")
    if summary["diffs"]:
        console.print("[red]与 YAML 语义不一致的字段:[/red]")
        for path in summary["diffs"]:
            console.print(f"  [red]{path}[/red]")
        if dry_run:
            raise typer.Exit(1)


@app.command("set")
def config_set(
    path: str = typer.Argument(..., help="字段路径，如 vendors.zhipu.timeout_ms"),
    value: str = typer.Argument(..., help="JSON 值（非法 JSON 按字符串处理）"),
) -> None:
    """修改单个字段（含 bootstrap 字段）."""
    try:
        ensure_store_ready()
        result, bootstrap = ConfigService().set_path(path, _parse_value(value))
    except _EXPECTED_ERRORS as exc:
        raise _fail(exc) from None
    section = path.split(".")[0]
    console.print(
        f"[green]已保存 {path}[/green]（{section} revision "
        f"{result.revisions.get(section, 0)}）"
    )
    for secret_path in result.cleared_secrets:
        console.print(f"[yellow]目标地址已变更，已清空密钥 {secret_path}[/yellow]")
    _print_pinned(result.pinned_secrets)
    if bootstrap:
        console.print("[yellow]bootstrap 字段需重启生效[/yellow]")


@app.command("reset")
def config_reset(
    section: str = typer.Argument(..., help="要恢复默认的 section"),
) -> None:
    """删除 section 覆盖，恢复包内默认值."""
    try:
        ensure_store_ready()
        result = ConfigService().reset_section(section)
    except _EXPECTED_ERRORS as exc:
        raise _fail(exc) from None
    console.print(f"[green]已恢复默认: {section}[/green]")
    _print_pinned(result.pinned_secrets)


@app.command("set-secret")
def config_set_secret(
    path: str = typer.Argument(..., help="密钥路径，如 vendors.zhipu.api_key"),
    env: str | None = typer.Option(None, "--env", help="绑定到环境变量"),
    stdin: bool = typer.Option(False, "--stdin", help="从标准输入读取密钥（加密保存）"),
    clear: bool = typer.Option(False, "--clear", help="清空（显式为空）"),
    default_env: bool = typer.Option(
        False, "--default-env", help="恢复为包内默认的环境变量"
    ),
) -> None:
    """设置密钥（值从不回显）."""
    chosen = [env is not None, stdin, clear, default_env]
    if sum(chosen) != 1:
        err_console.print(
            "[red]--env / --stdin / --clear / --default-env 必须且只能指定一个[/red]"
        )
        raise typer.Exit(2)
    if env is not None:
        op: dict[str, Any] = {"op": "env", "var": env}
    elif stdin:
        op = {"op": "set", "value": sys.stdin.read().strip()}
    elif clear:
        op = {"op": "clear"}
    else:
        op = {"op": "default_env"}
    try:
        ensure_store_ready()
        ConfigService().set_secret(path, op)
    except _EXPECTED_ERRORS as exc:
        raise _fail(exc) from None
    console.print(f"[green]已更新密钥 {path}（{op['op']}）[/green]")


@app.command("token")
def config_token() -> None:
    """输出 Dashboard 管理 token（不存在时创建）."""
    sys.stdout.write(load_or_create_admin_token() + "\n")


__all__ = ["app"]
