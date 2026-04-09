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
    3. **迁移错位的 tool_result 块**：Anthropic API 要求 ``tool_result`` 只能出现在
       ``user`` 消息中。当检测到非 user 消息中存在 ``tool_result`` 时，
       自动将其提取并挂载到最近的前置 user 消息（或创建新的 user 消息），
       防止上游返回 ``400 invalid_request_error`` 导致全链路降级失败。
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

    # 收集本轮被剥离的 misplaced tool_result 信息（用于汇总日志）
    stripped_misplaced: list[tuple[str, int, int, str]] = []  # (role, msg_idx, blk_idx, tool_use_id)

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

            # tool_result 出现在非 user 消息中（如 assistant）—— 收集信息，稍后汇总日志。
            # 典型触发场景：跨供应商降级时（如 Zhipu GLM → Anthropic），
            # GLM-5 在 assistant 响应中同时包含 tool_use 和 tool_result 内容块，
            # Claude Code 将此响应当作对话历史存储后，tool_result 出现在 assistant 角色消息中。
            # Anthropic API 严格要求 tool_result 只能出现在 user 消息中，因此必须剥离。
            adaptations.append("misplaced_tool_result_stripped")
            stripped_misplaced.append(
                (message_role, message_index, block_index, block.get("tool_use_id", "N/A"))
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

    # ── 汇总日志：misplaced tool_result 剥离 ──────────────────
    # 将逐块的 WARNING 合并为单条日志，并对同一 tool_use_id 跨请求去重：
    # 首次出现 → WARNING（含因果上下文），后续 → DEBUG。
    if stripped_misplaced:
        _emit_misplaced_tool_result_summary(stripped_misplaced)

    # ── 后处理：迁移错位的 tool_result 块 ──────────────────────
    # Anthropic API 强制要求 tool_result 仅存在于 user 消息中。
    # 多 vendor 场景下（尤其是降级恢复后的对话历史），可能出现
    # tool_result 残留在 assistant / system 等非 user 消息中的情况，
    # 导致 Anthropic 返回 400 invalid_request_error 并触发全链路降级。
    relocated = _relocate_misplaced_tool_results(normalized, adaptations)
    if relocated > 0:
        adaptations.append(f"tool_result_relocated_from_non_user_messages({relocated})")

    return NormalizationResult(
        body=normalized,
        adaptations=sorted(set(adaptations)),
        fatal_reasons=fatal_reasons,
    )


def _emit_misplaced_tool_result_summary(
    stripped: list[tuple[str, int, int, str]],
) -> None:
    """为被剥离的 misplaced tool_result 输出汇总日志.

    策略：
    - 将同一次请求中的多个剥离事件合并为单条日志
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
        to_keep = sorted(_LOGGED_MISPLACED_TOOL_IDS)[_LOGGED_MISPLACED_TOOL_IDS_MAX // 2:]
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
            "Vendor degradation adaptation: stripped %d misplaced tool_result block(s) "
            "from non-user message(s). Cause: cross-vendor conversation history contains "
            "tool_result blocks in assistant messages (typical when GLM-5 includes tool "
            "results inline in responses). Anthropic API strictly requires tool_result "
            "only in user messages, so these blocks are stripped to prevent 400 "
            "invalid_request_error. Affected: %s. Subsequent occurrences of these "
            "tool_use_ids will be logged at DEBUG level.",
            new_count,
            positions,
        )

    if known_id_set:
        # 已报告过的：DEBUG
        known_count = sum(1 for _, _, _, tid in stripped if tid in known_id_set)
        logger.debug(
            "Normalization: stripped %d previously reported misplaced tool_result "
            "block(s) (tool_use_ids: %s)",
            known_count,
            ", ".join(sorted(known_id_set)),
        )


def _relocate_misplaced_tool_results(
    body: dict[str, Any],
    adaptations: list[str],
) -> int:
    """检测并将非 user 消息中的 tool_result 块迁移到合法位置.

    策略：
    1. 扫描所有消息，识别非 user 消息中的 tool_result 块
    2. 将这些块从原消息中移除
    3. 将它们挂载到最近的前置 user 消息末尾（或创建新 user 消息）

    Returns:
        被迁移的 tool_result 块数量。
    """
    messages = body.get("messages", [])
    if not messages:
        return 0

    displaced_results: list[tuple[int, dict[str, Any]]] = []  # (msg_idx, block)

    for msg_idx, message in enumerate(messages):
        if not isinstance(message, dict):
            continue
        role = message.get("role")
        if role == "user":
            continue
        content = message.get("content")
        if not isinstance(content, list):
            continue

        cleaned_content: list[Any] = []
        for block in content:
            if (
                isinstance(block, dict)
                and block.get("type") == "tool_result"
                and block.get("tool_use_id")
            ):
                displaced_results.append((msg_idx, dict(block)))
                logger.debug(
                    "发现错位 tool_result: messages[%d], tool_use_id=%s, "
                    "将迁移至最近的 user 消息",
                    msg_idx,
                    block.get("tool_use_id", ""),
                )
            else:
                cleaned_content.append(block)
        message["content"] = cleaned_content

    if not displaced_results:
        return 0

    # 查找或创建目标 user 消息：优先选择离错位块最近的前置 user 消息
    first_displaced_idx = displaced_results[0][0]
    target_msg_idx = _find_nearest_user_message(messages, first_displaced_idx)

    if target_msg_idx is None:
        # 无前置 user 消息：在消息列表头部插入一个新的 user 消息
        messages.insert(
            0,
            {
                "role": "user",
                "content": [block for _, block in displaced_results],
            },
        )
        logger.info(
            "已创建新 user 消息（索引 0）以容纳 %d 个错位 tool_result 块",
            len(displaced_results),
        )
    else:
        target_msg = messages[target_msg_idx]
        target_content = target_msg.get("content")
        if not isinstance(target_content, list):
            target_content = []
            target_msg["content"] = target_content
        for _, block in displaced_results:
            target_content.append(block)
        logger.info(
            "已将 %d 个错位 tool_result 块迁移至 messages[%d] (role=user)",
            len(displaced_results),
            target_msg_idx,
        )

    return len(displaced_results)


def _find_nearest_user_message(
    messages: list[dict[str, Any]],
    from_index: int,
) -> int | None:
    """查找离指定索引最近的前置 user 消息.

    Args:
        messages: 消息列表
        from_index: 起始搜索位置（不包含此位置）

    Returns:
        最近的前置 user 消息索引，若无则返回 None。
    """
    for idx in range(from_index - 1, -1, -1):
        msg = messages[idx]
        if isinstance(msg, dict) and msg.get("role") == "user":
            return idx
    return None
