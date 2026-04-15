"""入站 Anthropic Messages 请求规范化."""

from __future__ import annotations

import copy
import logging
import re
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

# ── 跨请求日志去重：记录已报告过的 misplaced tool_use_id ──────────
# 同一 tool_use_id 仅首次输出 WARNING（含完整因果上下文），
# 后续在同一会话中重复出现时降级为 DEBUG，避免日志噪声。
_LOGGED_MISPLACED_TOOL_IDS: set[str] = set()
_LOGGED_MISPLACED_TOOL_IDS_MAX = 500

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
    """清洗供应商私有块，尽量恢复为合法 Anthropic Messages 请求.

    处理策略：
    1. 移除供应商私有块（如 server_tool_use_delta）
    2. 重写无效/非标准的 tool_use / tool_result ID
    3. **重定位错位的 tool_result 块**：Anthropic API 要求 ``tool_result`` 只能出现在
       ``user`` 消息中。当检测到非 user 消息中存在 ``tool_result`` 时，
       将其重定位到紧邻的下一个 user 消息中，以保持 ``tool_use`` / ``tool_result``
       配对关系，防止上游返回 ``400 invalid_request_error``。
    4. **修复孤儿 tool_use 块**：当 assistant 消息中的 ``tool_use`` 在紧邻的 user 消息中
       没有对应的 ``tool_result`` 时（如跨供应商降级导致对话结构不完整），
       合成一个 ``is_error=true`` 的占位 ``tool_result`` 以满足 API 约束。
    """
    normalized = copy.deepcopy(body)
    adaptations: list[str] = []
    fatal_reasons: list[str] = []
    tool_id_map: dict[str, str] = {}
    normalized_counter = 0

    def next_tool_id() -> str:
        nonlocal normalized_counter
        normalized_counter += 1
        return f"toolu_normalized_{normalized_counter}"

    # 收集本轮被重定位的 misplaced tool_result 块及日志信息
    relocated_results: list[tuple[int, dict[str, Any]]] = []  # (source_msg_idx, block)
    relocated_log_info: list[
        tuple[str, int, int, str]
    ] = []  # (role, msg_idx, blk_idx, tool_use_id)

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

        if block_type == "tool_result":
            if message_role == "user":
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

            # tool_result 出现在非 user 消息中（如 assistant）—— 重定位到紧邻的 user 消息。
            # 典型触发场景：跨供应商降级时（如 Zhipu GLM → Anthropic），
            # GLM-5 在 assistant 响应中同时包含 tool_use 和 tool_result 内容块，
            # Claude Code 将此响应当作对话历史存储后，tool_result 出现在 assistant 角色消息中。
            # 直接剥离会导致 tool_use 成为孤儿块（无配对 tool_result），触发上游 400 错误。
            # 因此改为重定位：将 tool_result 移至紧邻的下一个 user 消息中。
            normalized_block = dict(block)
            tool_use_id = normalized_block.get("tool_use_id")
            if isinstance(tool_use_id, str) and tool_use_id in tool_id_map:
                normalized_block["tool_use_id"] = tool_id_map[tool_use_id]
                adaptations.append("tool_result_tool_use_id_rewritten")
            adaptations.append("misplaced_tool_result_relocated")
            relocated_results.append((message_index, normalized_block))
            relocated_log_info.append(
                (
                    message_role,
                    message_index,
                    block_index,
                    normalized_block.get("tool_use_id", "N/A"),
                )
            )
            return None

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

    # ── 重定位 misplaced tool_result 到紧邻的 user 消息 ──────────
    # 按源消息索引降序处理，避免插入新消息时索引偏移。
    messages_list = normalized.get("messages", [])
    for source_idx, result_block in sorted(
        relocated_results, key=lambda x: x[0], reverse=True
    ):
        target_user_idx = None
        for j in range(source_idx + 1, len(messages_list)):
            if (
                isinstance(messages_list[j], dict)
                and messages_list[j].get("role") == "user"
            ):
                target_user_idx = j
                break
        if target_user_idx is not None:
            # 追加到已有 user 消息的 content 末尾
            target_content = messages_list[target_user_idx].get("content")
            if isinstance(target_content, list):
                target_content.append(result_block)
            elif isinstance(target_content, str):
                # string content 转为 text block 后追加，避免丢失原始文本
                messages_list[target_user_idx]["content"] = [
                    {"type": "text", "text": target_content},
                    result_block,
                ]
            else:
                messages_list[target_user_idx]["content"] = [result_block]
        else:
            # 无后续 user 消息：插入一条合成 user 消息
            messages_list.insert(
                source_idx + 1,
                {
                    "role": "user",
                    "content": [result_block],
                },
            )

    # ── 汇总日志：misplaced tool_result 重定位 ──────────────────
    if relocated_log_info:
        _emit_misplaced_tool_result_summary(relocated_log_info)

    # ── 修复通道：为孤儿 tool_use 合成 tool_result ──────────────
    # Anthropic API 严格要求每个 tool_use 必须在紧邻的 user 消息中有对应的 tool_result。
    # 当 tool_result 完全缺失时（如跨供应商降级导致对话结构不完整），
    # 合成一个 is_error=true 的占位 tool_result 以满足 API 约束。
    repaired = _repair_orphaned_tool_use(messages_list)
    if repaired:
        adaptations.append("orphaned_tool_use_repaired")
        total_synthesized = sum(repaired.values())
        logger.warning(
            "Vendor degradation adaptation: synthesized %d tool_result block(s) "
            "for orphaned tool_use to satisfy Anthropic pairing constraint. "
            "Affected tool_use_ids: %s",
            total_synthesized,
            ", ".join(sorted(repaired)),
        )

    return NormalizationResult(
        body=normalized,
        adaptations=sorted(set(adaptations)),
        fatal_reasons=fatal_reasons,
    )


def _emit_misplaced_tool_result_summary(
    stripped: list[tuple[str, int, int, str]],
) -> None:
    """为被重定位的 misplaced tool_result 输出汇总日志.

    策略：
    - 将同一次请求中的多个重定位事件合并为单条日志
    - 首次出现的 tool_use_id → WARNING（含完整因果上下文）
    - 同一 tool_use_id 在后续请求中再次出现 → DEBUG（避免日志噪声）

    注意：此函数运行在 asyncio 事件循环的主线程中，set 操作无需加锁。

    Args:
        stripped: 每个元素为 (message_role, message_index, block_index, tool_use_id)
    """
    # 提取去重后的 tool_use_id 集合
    unique_tool_ids = {tid for _, _, _, tid in stripped}

    # 区分首次出现 vs 已报告过的
    new_id_set = unique_tool_ids - _LOGGED_MISPLACED_TOOL_IDS
    known_id_set = unique_tool_ids & _LOGGED_MISPLACED_TOOL_IDS

    # 更新已报告集合（防止无限增长：保留最近一半条目）
    _LOGGED_MISPLACED_TOOL_IDS.update(unique_tool_ids)
    if len(_LOGGED_MISPLACED_TOOL_IDS) > _LOGGED_MISPLACED_TOOL_IDS_MAX:
        to_keep = sorted(_LOGGED_MISPLACED_TOOL_IDS)[
            _LOGGED_MISPLACED_TOOL_IDS_MAX // 2 :
        ]
        _LOGGED_MISPLACED_TOOL_IDS.clear()
        _LOGGED_MISPLACED_TOOL_IDS.update(to_keep)

    if new_id_set:
        # 首次出现：WARNING + 完整因果上下文
        positions = ", ".join(
            f"messages.{mi}.content.{bi} (role={r}, tool_use_id={tid})"
            for r, mi, bi, tid in stripped
            if tid in new_id_set
        )
        new_count = sum(1 for _, _, _, tid in stripped if tid in new_id_set)
        logger.warning(
            "Vendor degradation adaptation: relocated %d misplaced tool_result block(s) "
            "from non-user message(s) to adjacent user message(s). Cause: cross-vendor "
            "conversation history contains tool_result blocks in assistant messages "
            "(typical when GLM-5 includes tool results inline in responses). Anthropic "
            "API requires tool_result only in user messages, so these blocks are "
            "relocated to maintain tool_use/tool_result pairing. Affected: %s. "
            "Subsequent occurrences of these tool_use_ids will be logged at DEBUG level.",
            new_count,
            positions,
        )

    if known_id_set:
        # 已报告过的：DEBUG
        known_count = sum(1 for _, _, _, tid in stripped if tid in known_id_set)
        logger.debug(
            "Normalization: relocated %d previously reported misplaced tool_result "
            "block(s) (tool_use_ids: %s)",
            known_count,
            ", ".join(sorted(known_id_set)),
        )


def _repair_orphaned_tool_use(
    messages_list: list[dict[str, Any]],
) -> dict[str, int]:
    """为孤儿 tool_use 块合成缺失的 tool_result.

    遍历所有 assistant 消息，检查紧邻的 user 消息是否包含每个 tool_use
    对应的 tool_result。对于缺失的 tool_result，合成一个 ``is_error=true``
    的占位块以满足 Anthropic API 约束。

    Args:
        messages_list: 消息列表（就地修改）。

    Returns:
        dict: 以 tool_use_id 为键、修复次数为值的映射；无修复时返回空 dict。
    """
    repaired: dict[str, int] = {}
    i = 0
    while i < len(messages_list):
        msg = messages_list[i]
        if not isinstance(msg, dict) or msg.get("role") != "assistant":
            i += 1
            continue

        content = msg.get("content")
        if not isinstance(content, list):
            i += 1
            continue

        # 收集当前 assistant 消息中所有 tool_use 的 id
        tool_use_ids: list[str] = [
            b["id"]
            for b in content
            if isinstance(b, dict) and b.get("type") == "tool_use" and b.get("id")
        ]
        if not tool_use_ids:
            i += 1
            continue

        # 检查紧邻的 user 消息中已有的 tool_result
        next_msg = messages_list[i + 1] if i + 1 < len(messages_list) else None
        existing_result_ids: set[str] = set()

        if isinstance(next_msg, dict) and next_msg.get("role") == "user":
            next_content = next_msg.get("content")
            if isinstance(next_content, list):
                existing_result_ids = {
                    b["tool_use_id"]
                    for b in next_content
                    if isinstance(b, dict)
                    and b.get("type") == "tool_result"
                    and b.get("tool_use_id")
                }

        # 找出缺失的 tool_result
        orphan_ids = [uid for uid in tool_use_ids if uid not in existing_result_ids]
        if not orphan_ids:
            i += 1
            continue

        # 为每个孤儿 tool_use 合成 tool_result
        synthetic_blocks = [
            {
                "type": "tool_result",
                "tool_use_id": uid,
                "content": "",
                "is_error": True,
            }
            for uid in orphan_ids
        ]

        if isinstance(next_msg, dict) and next_msg.get("role") == "user":
            next_content = next_msg.get("content")
            if isinstance(next_content, list):
                next_content.extend(synthetic_blocks)
            elif isinstance(next_content, str):
                next_msg["content"] = [
                    {"type": "text", "text": next_content}
                ] + synthetic_blocks
            else:
                next_msg["content"] = synthetic_blocks
        else:
            # 无紧邻 user 消息：插入合成 user 消息
            messages_list.insert(i + 1, {"role": "user", "content": synthetic_blocks})

        for uid in orphan_ids:
            repaired[uid] = repaired.get(uid, 0) + 1

        i += 1
    return repaired
