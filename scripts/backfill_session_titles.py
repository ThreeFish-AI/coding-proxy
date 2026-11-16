#!/usr/bin/env python3
"""历史 Session 标题回填 — 基于 Claude Code 转录重算 session_meta.title.

代理不存储请求体，历史会话的真实用户输入位于 Claude Code 本地转录
``~/.claude/projects/*/<session_id>.jsonl``。本脚本将转录中的 user 消息
喂给与请求路径共用的 ``_derive_title_from_user_texts``（同一提取逻辑，
含注入式模板载荷剥离），重算标题并回写 ``session_meta``。

用法::

    # 干跑（默认）：仅输出统计与抽样 diff，不写库
    uv run python scripts/backfill_session_titles.py --db ~/.coding-proxy/usage.db

    # 落库：先经 sqlite3 backup API 生成带时间戳的备份，再执行更新
    uv run python scripts/backfill_session_titles.py --db ~/.coding-proxy/usage.db --apply

安全边界：只 UPDATE / INSERT ``session_meta`` 一张表，不触碰 ``usage_log``；
无转录的会话（无数据源）与命中保护前缀的会话（标题参与供应商绑定）跳过。
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from datetime import UTC, datetime
from pathlib import Path

# 复用请求路径的标题推导逻辑（单一事实源）
from coding.proxy.config.importer import load_offline_config
from coding.proxy.config.loader import load_config
from coding.proxy.routing.executor import _derive_title_from_user_texts

PROJECTS_DIR = Path(
    os.path.expanduser(os.environ.get("BACKFILL_CC_PROJECTS", "~/.claude/projects"))
)
MAX_USER_TEXTS_PER_SESSION = 30


def build_transcript_index(projects_dir: Path) -> dict[str, Path]:
    """session_id → 转录路径索引；同一 id 多文件时取最新修改者."""
    index: dict[str, Path] = {}
    for path in projects_dir.glob("*/*.jsonl"):
        sid = path.stem
        existing = index.get(sid)
        if existing is None or path.stat().st_mtime > existing.stat().st_mtime:
            index[sid] = path
    return index


def _is_local_command_record(text: str, *, prev_is_meta_caveat: bool) -> bool:
    """识别 CC 本地 slash 命令记录（运行时请求路径不可见的行）。

    本地三行记录（caveat / command-name / stdout）中只有 caveat 行带 isMeta
    标记；实测转录中所有本地 command-name/command-message 行的前驱条目均为
    isMeta 的 caveat 行，以此作判别特征。API-sent 的 slash 命令展开无此前驱，
    须放行给 ``_sanitize_user_text`` 合成「命令 + 参数」标题，不能误丢。"""
    stripped = text.lstrip()
    if stripped.startswith("<local-command"):
        return True
    return prev_is_meta_caveat and (
        stripped.startswith("<command-name>")
        or stripped.startswith("<command-message>")
    )


def load_user_texts(path: Path) -> list[str]:
    """依时间序抽取真实用户输入（排除 isMeta / sidechain / 本地命令记录 /
    非 text 块）；上限按有效候选计数而非原始行数。"""
    texts: list[str] = []
    prev_is_meta_caveat = False
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                if len(texts) >= MAX_USER_TEXTS_PER_SESSION:
                    break
                try:
                    entry = json.loads(line)
                except (json.JSONDecodeError, ValueError):
                    continue
                if entry.get("type") == "user":
                    content = (entry.get("message") or {}).get("content")
                    if entry.get("isMeta"):
                        serialized = (
                            content
                            if isinstance(content, str)
                            else json.dumps(content, ensure_ascii=False)
                        )
                        prev_is_meta_caveat = "local-command-caveat" in serialized
                        continue
                    if entry.get("isSidechain"):
                        prev_is_meta_caveat = False
                        continue
                    candidates: list[str] = []
                    if isinstance(content, str):
                        candidates = [content]
                    elif isinstance(content, list):
                        candidates = [
                            block.get("text", "")
                            for block in content
                            if isinstance(block, dict) and block.get("type") == "text"
                        ]
                    texts.extend(
                        t
                        for t in candidates
                        if t.strip()
                        and not _is_local_command_record(
                            t, prev_is_meta_caveat=prev_is_meta_caveat
                        )
                    )
                prev_is_meta_caveat = False
    except OSError as exc:
        print(f"  ! 读取转录失败 {path}: {exc}", file=sys.stderr)
    return texts


def parse_session_id(session_key: str) -> str:
    """从 session_key 解析 CC session_id。

    JSON dict 形态取 ``session_id`` 字段；JSON 标量（如纯数字 user_id）与
    非 JSON 裸 UUID 形态原样返回——裸 UUID 本身即转录文件 stem，可直接命中
    索引（对齐 dashboard 前端 parseSessionKey 的 catch 回退契约）。"""
    try:
        obj = json.loads(session_key)
    except (json.JSONDecodeError, ValueError):
        return session_key
    if isinstance(obj, dict):
        return obj.get("session_id") or ""
    return session_key


def backup_database(db_path: Path) -> Path:
    """经 sqlite3 backup API 生成一致性备份（对运行中的代理写入安全）."""
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    backup_path = db_path.with_name(f"{db_path.name}.backup-{stamp}")
    src = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        dst = sqlite3.connect(str(backup_path))
        try:
            src.backup(dst)
        finally:
            dst.close()
    finally:
        src.close()
    return backup_path


def main() -> int:
    parser = argparse.ArgumentParser(description="基于 CC 转录回填 session_meta.title")
    parser.add_argument("--db", required=True, help="usage.db 路径")
    parser.add_argument("--projects", default=str(PROJECTS_DIR), help="CC 转录根目录")
    parser.add_argument(
        "--config",
        default=None,
        help="显式指定 legacy YAML 配置覆盖（默认与运行时代理同源：config.db 优先，"
        "未迁移实例回退 YAML，即 load_offline_config 语义）",
    )
    parser.add_argument("--apply", action="store_true", help="落库（默认干跑）")
    parser.add_argument(
        "--protect-prefix",
        action="append",
        default=[],
        help="额外保护前缀（现有标题以此开头则跳过；配置的绑定前缀已默认并入）",
    )
    parser.add_argument("--sample", type=int, default=15, help="抽样 diff 展示条数")
    args = parser.parse_args()

    db_path = Path(args.db).expanduser()
    if not db_path.exists():
        print(f"数据库不存在: {db_path}", file=sys.stderr)
        return 2

    # 与请求路径同一配置语义：豁免前缀透传给推导器；绑定前缀默认并入保护集，
    # 防止改写参与 title_vendor_bindings 的现有标题（仪表盘「前缀=绑定意图」语义）。
    # 配置源与离线命令（usage / auth）同源：load_offline_config 读 config.db
    # （运行时权威源，Dashboard 热编辑即时生效），未迁移实例回退 legacy YAML。
    exempt_prefixes: list[str] = []
    if args.config:
        cfg = load_config(Path(args.config).expanduser())
        hint = None
    else:
        cfg, hint = load_offline_config(decrypt=False)
        if hint:
            print(f"配置提示: {hint}")
    exempt_prefixes = list(cfg.session_policies.title_exempt_prefixes)
    protect = list(args.protect_prefix)
    protect.extend(b.prefix for b in cfg.session_policies.title_vendor_bindings)
    args.protect_prefix = protect
    print(
        f"配置源: {'--config ' + args.config if args.config else 'config.db/YAML 离线解析'}"
    )
    print(f"豁免前缀 {exempt_prefixes}，保护前缀 {args.protect_prefix}")

    index = build_transcript_index(Path(args.projects).expanduser())
    print(f"转录索引: {len(index)} 个 session_id（{args.projects}）")

    con = sqlite3.connect(
        f"file:{db_path}?mode=ro" if not args.apply else str(db_path),
        uri=not args.apply,
    )
    con.row_factory = sqlite3.Row

    rows = con.execute(
        """SELECT s.session_key, m.title AS title
           FROM (SELECT DISTINCT session_key FROM usage_log WHERE session_key != '') s
           LEFT JOIN session_meta m ON m.session_key = s.session_key"""
    ).fetchall()

    stats = {
        "updated": 0,
        "inserted": 0,
        "kept": 0,
        "no_transcript": 0,
        "protected": 0,
        "no_candidate": 0,
    }
    changes: list[tuple[str, str, str]] = []
    for row in rows:
        key, old = row["session_key"], row["title"] or ""
        if any(old.startswith(p) for p in args.protect_prefix):
            stats["protected"] += 1
            continue
        sid = parse_session_id(key)
        path = index.get(sid)
        if path is None:
            stats["no_transcript"] += 1
            continue
        derived = _derive_title_from_user_texts(
            load_user_texts(path), exempt_prefixes=exempt_prefixes
        )
        if not derived:
            stats["no_candidate"] += 1
            continue
        if derived == old:
            stats["kept"] += 1
            continue
        changes.append((key, old, derived))
        if old:
            stats["updated"] += 1
        else:
            stats["inserted"] += 1

    print("\n统计:", json.dumps(stats, ensure_ascii=False))
    print(f"\n抽样变更（前 {args.sample} 条）:")
    for key, old, new in changes[: args.sample]:
        print(f"  session={parse_session_id(key)[:8]}…")
        print(f"    旧: {old[:110] or '(空)'}")
        print(f"    新: {new[:110]}")

    if not args.apply:
        print("\n干跑结束，未写库。确认后加 --apply 执行。")
        return 0

    if not changes:
        print("\n无变更，未写库。")
        return 0

    backup_path = backup_database(db_path)
    print(f"\n已备份: {backup_path}")
    con.execute("BEGIN")
    con.executemany(
        """INSERT INTO session_meta (session_key, title, created_at)
           VALUES (?, ?, strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
           ON CONFLICT(session_key) DO UPDATE SET title = excluded.title""",
        [(key, new) for key, _, new in changes],
    )
    con.commit()
    print(f"已回写 {len(changes)} 条标题到 session_meta。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
