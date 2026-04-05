"""入站 Anthropic Messages 请求规范化."""

from __future__ import annotations

import copy
import re
from dataclasses import dataclass, field
from typing import Any

_ANTHROPIC_TOOL_USE_ID_RE = re.compile(r"^toolu_[A-Za-z0-9_]+$")
_ANTHROPIC_SERVER_TOOL_USE_ID_RE = re.compile(r"^srvtoolu_[A-Za-z0-9_]+$")
_VENDOR_TOOL_BLOCK_TYPES = {
    "server_tool_use_delta",
}


@dataclass
class NormalizationResult:
    """请求规范化结果."""

    body: dict[str, Any]
    adaptations: list[str] = field(default_factory=list)
    fatal_reasons: list[str] = field(default_factory=list)

    @property
    def recoverable(self) -> bool:
        return not self.fatal_reasons


def normalize_anthropic_request(body: dict[str, Any]) -> NormalizationResult:
    """清洗供应商私有块，尽量恢复为合法 Anthropic Messages 请求."""
    normalized = copy.deepcopy(body)
    adaptations: list[str] = []
    fatal_reasons: list[str] = []
    tool_id_map: dict[str, str] = {}
    normalized_counter = 0

    def next_tool_id() -> str:
        nonlocal normalized_counter
        normalized_counter += 1
        return f"toolu_normalized_{normalized_counter}"

    def normalize_content_block(
        block: Any,
        *,
        message_role: str,
        message_index: int,
        block_index: int,
    ) -> dict[str, Any] | None:
        if not isinstance(block, dict):
            return None

        block_type = block.get("type")
        if block_type in _VENDOR_TOOL_BLOCK_TYPES:
            adaptations.append(f"vendor_block_removed:{block_type}")
            return None

        if message_role == "assistant" and block_type in {
            "tool_use",
            "server_tool_use",
        }:
            normalized_block = dict(block)
            tool_id = normalized_block.get("id")
            if isinstance(tool_id, str) and _ANTHROPIC_SERVER_TOOL_USE_ID_RE.match(
                tool_id
            ):
                new_id = next_tool_id()
                tool_id_map[tool_id] = new_id
                normalized_block["id"] = new_id
                normalized_block["type"] = "tool_use"
                adaptations.append("server_tool_use_id_rewritten_for_anthropic")
            elif isinstance(tool_id, str) and _ANTHROPIC_TOOL_USE_ID_RE.match(tool_id):
                normalized_block["type"] = "tool_use"
            elif isinstance(tool_id, str) and tool_id:
                if "name" in normalized_block:
                    new_id = next_tool_id()
                    tool_id_map[tool_id] = new_id
                    normalized_block["id"] = new_id
                    normalized_block["type"] = "tool_use"
                    adaptations.append("invalid_tool_use_id_rewritten_for_anthropic")
                else:
                    fatal_reasons.append(
                        f"messages.{message_index}.content.{block_index}: tool block missing name for id rewrite"
                    )
                    return None
            else:
                fatal_reasons.append(
                    f"messages.{message_index}.content.{block_index}: tool block missing id"
                )
                return None
            return normalized_block

        if message_role == "user" and block_type == "tool_result":
            normalized_block = dict(block)
            tool_use_id = normalized_block.get("tool_use_id")
            if isinstance(tool_use_id, str) and tool_use_id in tool_id_map:
                normalized_block["tool_use_id"] = tool_id_map[tool_use_id]
                adaptations.append("tool_result_tool_use_id_rewritten")
            elif isinstance(tool_use_id, str) and (
                _ANTHROPIC_TOOL_USE_ID_RE.match(tool_use_id)
                or _ANTHROPIC_SERVER_TOOL_USE_ID_RE.match(tool_use_id)
            ):
                # 保持原样。对 server_tool_use_id 的用户结果，若未在当前请求体中出现，
                # 交由上游决定是否接受，避免错误猜测跨轮次关联。
                return normalized_block
            elif isinstance(tool_use_id, str) and tool_use_id:
                fatal_reasons.append(
                    f"messages.{message_index}.content.{block_index}: tool_result references unknown tool_use_id"
                )
                return None
            else:
                fatal_reasons.append(
                    f"messages.{message_index}.content.{block_index}: tool_result missing tool_use_id"
                )
                return None
            return normalized_block

        return dict(block)

    for message_index, message in enumerate(normalized.get("messages", [])):
        if not isinstance(message, dict):
            continue
        content = message.get("content")
        if not isinstance(content, list):
            continue
        role = str(message.get("role") or "")
        new_content: list[Any] = []
        for block_index, block in enumerate(content):
            normalized_block = normalize_content_block(
                block,
                message_role=role,
                message_index=message_index,
                block_index=block_index,
            )
            if normalized_block is not None:
                new_content.append(normalized_block)
        message["content"] = new_content

    return NormalizationResult(
        body=normalized,
        adaptations=sorted(set(adaptations)),
        fatal_reasons=fatal_reasons,
    )
