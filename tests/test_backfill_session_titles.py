"""回填脚本 loader 单元测试.

覆盖 :mod:`scripts.backfill_session_titles` 的转录抽取契约：
- isMeta / sidechain 行过滤
- CC 本地 slash 命令记录（caveat 行之外的 command-name / stdout 行）丢弃
- 有效候选计数上限（包装行不占额度）
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

_SCRIPT = (
    Path(__file__).resolve().parent.parent / "scripts" / "backfill_session_titles.py"
)
_spec = importlib.util.spec_from_file_location("backfill_session_titles", _SCRIPT)
backfill = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(backfill)


def _write_transcript(tmp_path: Path, entries: list[dict]) -> Path:
    path = tmp_path / "session.jsonl"
    path.write_text(
        "\n".join(json.dumps(e, ensure_ascii=False) for e in entries), encoding="utf-8"
    )
    return path


def _user(content, **flags) -> dict:
    entry = {"type": "user", "message": {"content": content}}
    entry.update(flags)
    return entry


class TestLoadUserTexts:
    def test_filters_is_meta_and_sidechain(self, tmp_path):
        path = _write_transcript(
            tmp_path,
            [
                _user("meta 行", isMeta=True),
                _user("sidechain 行", isSidechain=True),
                _user("真实输入"),
            ],
        )
        assert backfill.load_user_texts(path) == ["真实输入"]

    def test_drops_local_command_records(self, tmp_path):
        """command-name / local-command-stdout 行不带 isMeta,需按内容识别丢弃."""
        path = _write_transcript(
            tmp_path,
            [
                _user(
                    "<local-command-caveat>not sent to you as a request</local-command-caveat>",
                    isMeta=True,
                ),
                _user(
                    "<command-name>/model</command-name>\n<command-args>opus</command-args>"
                ),
                _user("<local-command-stdout>ok</local-command-stdout>"),
                _user("真实任务输入"),
            ],
        )
        assert backfill.load_user_texts(path) == ["真实任务输入"]

    def test_collects_text_blocks_from_list_content(self, tmp_path):
        path = _write_transcript(
            tmp_path,
            [
                _user(
                    [
                        {"type": "text", "text": "第一段"},
                        {
                            "type": "tool_result",
                            "tool_use_id": "t1",
                            "content": "ignored",
                        },
                        {"type": "text", "text": "第二段"},
                    ]
                ),
            ],
        )
        assert backfill.load_user_texts(path) == ["第一段", "第二段"]

    def test_effective_candidate_cap_excludes_wrappers(self, tmp_path):
        """上限计有效候选：30 条包装行不挤占真实输入额度."""
        entries = [
            _user("<local-command-stdout>x</local-command-stdout>") for _ in range(40)
        ]
        entries.append(_user("真实任务输入"))
        assert backfill.load_user_texts(_write_transcript(tmp_path, entries)) == [
            "真实任务输入"
        ]

    def test_empty_and_unreadable_paths(self, tmp_path):
        empty = tmp_path / "empty.jsonl"
        empty.write_text("", encoding="utf-8")
        assert backfill.load_user_texts(empty) == []
        assert backfill.load_user_texts(tmp_path / "missing.jsonl") == []


class TestIsLocalCommandRecord:
    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            ("<command-name>/model</command-name>", True),
            ("  <local-command-stdout>ok</local-command-stdout>", True),
            ("<command-args>opus</command-args>", False),
            ("普通用户消息", False),
            ("", False),
        ],
    )
    def test_patterns(self, text, expected):
        assert backfill._is_local_command_record(text) is expected
