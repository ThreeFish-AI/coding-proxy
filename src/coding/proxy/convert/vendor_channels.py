"""供应商跨供应商转换通道 — 源→目标绑定的请求体预处理.

每个通道函数表示一个具体的「源 vendor → 目标 vendor」绑定转换关系，
接收标准化的 Anthropic 格式请求体，返回清理跨供应商产物后的请求体（深拷贝）。

通道注册表 ``VENDOR_TRANSITIONS`` 提供统一的 (source, target) → channel_fn 映射，
executor 层通过 ``get_transition_channel()`` 查表分发，无需感知具体供应商逻辑。

转换矩阵（仅注册需要转换的源→目标对，未注册的不触发任何通道）:
    zhipu → anthropic : prepare_zhipu_to_anthropic  (剥离 thinking + tool pairing)
    zhipu → copilot   : prepare_zhipu_to_copilot    (剥离 thinking + cache_control + tool pairing)
    copilot → zhipu   : prepare_copilot_to_zhipu    (剥离 thinking + cache_control + 移除 thinking 参数 + tool pairing)
    anthropic → zhipu : prepare_anthropic_to_zhipu  (剥离 server_tool_use + thinking + cache_control + 移除 thinking 参数 + tool pairing)
"""

from __future__ import annotations

import copy
import logging
import re
from collections.abc import Callable
from typing import Any

logger = logging.getLogger(__name__)

_THINKING_BLOCK_TYPES = {"thinking", "redacted_thinking"}

# ── Anthropic 工具块 ID 规范 ───────────────────────────────────
_ANTHROPIC_TOOL_USE_ID_RE = re.compile(r"^toolu_[A-Za-z0-9_]+$")
_ANTHROPIC_SERVER_TOOL_USE_ID_RE = re.compile(r"^srvtoolu_[A-Za-z0-9_]+$")

# Zhipu 流式响应中出现的非标准供应商私有 content block 类型.
# Anthropic API 拒绝这些块，需要在跨 vendor 请求体中剥离.
_ZHIPU_VENDOR_BLOCK_TYPES = {"server_tool_use_delta"}

# Zhipu 内联输出非标准 content block 类型的标识（用于源供应商推断）.
_ZHIPU_SERVER_TOOL_USE_TYPES = {"server_tool_use", "server_tool_use_delta"}

# ── 转换通道注册表 ─────────────────────────────────────────────
# (source_vendor, target_vendor) → (body) → (prepared_body, adaptations)
VENDOR_TRANSITIONS: dict[
    tuple[str, str], Callable[[dict[str, Any]], tuple[dict[str, Any], list[str]]]
] = {}


def get_transition_channel(
    source: str, target: str
) -> Callable[[dict[str, Any]], tuple[dict[str, Any], list[str]]] | None:
    """查找源→目标绑定转换通道，不存在时返回 None."""
    return VENDOR_TRANSITIONS.get((source, target))


# ── 共享辅助函数 ──────────────────────────────────────────────


def strip_thinking_blocks(body: dict[str, Any]) -> int:
    """从 assistant 消息中移除 thinking/redacted_thinking 块（就地）.

    Anthropic API 要求 thinking blocks 的 signature 必须是其签发的有效签名。
    跨供应商迁移（如 Zhipu → Anthropic）后，conversation history 中可能包含
    非 Anthropic 签发的 signature，导致 400 invalid_request_error。
    根据 Anthropic 官方文档，thinking blocks 可以被安全省略，不影响模型行为。

    剥离后 content 为空时插入最小占位 text block 以保持消息结构合法性。

    Returns:
        被移除的 thinking block 数量。
    """
    stripped = 0
    for message in body.get("messages", []):
        if not isinstance(message, dict) or message.get("role") != "assistant":
            continue
        content = message.get("content")
        if not isinstance(content, list):
            continue
        original_len = len(content)
        new_content = [
            block
            for block in content
            if not (
                isinstance(block, dict) and block.get("type") in _THINKING_BLOCK_TYPES
            )
        ]
        removed = original_len - len(new_content)
        if removed and not new_content:
            new_content = [{"type": "text", "text": "[thinking]"}]
            logger.info(
                "Inserted placeholder text block after stripping "
                "%d thinking block(s) to avoid empty assistant content",
                removed,
            )
        message["content"] = new_content
        stripped += removed
    return stripped


def enforce_anthropic_tool_pairing(
    messages_list: list[dict[str, Any]],
) -> list[str]:
    """为跨供应商场景强制保证 Anthropic tool_use/tool_result 配对约束.

    单次正向遍历所有消息，对每个 assistant 消息执行：

    1. 剥离所有 tool_result 块（跨供应商产物，如 GLM-5 内联的 tool_result）
    2. 收集所有 tool_use ID
    3. 确保紧邻的下一条消息是 user 消息且包含所有必需的 tool_result
    4. 将剥离的 tool_result 重定位到正确的 user 消息
    5. 为仍缺失的 tool_result 合成 ``is_error=True`` 的占位块

    此函数是一个**自包含的单遍处理**，不依赖 Phase 1 收集的 misplaced 信息。

    Args:
        messages_list: 消息列表（就地修改）。

    Returns:
        新增的 adaptation 标签列表。
    """
    adaptations: list[str] = []
    relocated_count = 0
    synthesized_ids: list[str] = []

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

        # A. 从 assistant 消息中剥离所有 tool_result 块
        extracted_tool_results: dict[str, dict[str, Any]] = {}
        retained_content: list[Any] = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "tool_result":
                tid = block.get("tool_use_id")
                if tid:
                    extracted_tool_results[tid] = block
                    relocated_count += 1
            else:
                retained_content.append(block)

        if extracted_tool_results:
            msg["content"] = retained_content

        # B. 收集所有 tool_use ID
        tool_use_ids: list[str] = [
            b["id"]
            for b in (
                msg.get("content") if isinstance(msg.get("content"), list) else []
            )
            if isinstance(b, dict) and b.get("type") == "tool_use" and b.get("id")
        ]
        if not tool_use_ids:
            current_content = msg.get("content")
            if isinstance(current_content, list) and not current_content:
                msg["content"] = [{"type": "text", "text": ""}]
            i += 1
            continue

        # C. 确保 messages[i+1] 是 user 消息
        next_idx = i + 1
        if (
            next_idx < len(messages_list)
            and isinstance(messages_list[next_idx], dict)
            and messages_list[next_idx].get("role") == "user"
        ):
            user_msg = messages_list[next_idx]
        else:
            user_msg: dict[str, Any] = {"role": "user", "content": []}
            messages_list.insert(next_idx, user_msg)

        # D. 确保 user_msg.content 是 list
        user_content = user_msg.get("content")
        if isinstance(user_content, str):
            user_msg["content"] = [{"type": "text", "text": user_content}]
        elif not isinstance(user_content, list):
            user_msg["content"] = []

        # E. 收集 user 消息中已有的 tool_result IDs
        existing_result_ids: set[str] = {
            b["tool_use_id"]
            for b in user_msg["content"]
            if isinstance(b, dict)
            and b.get("type") == "tool_result"
            and b.get("tool_use_id")
        }

        # F. 为每个 tool_use_id 确保 tool_result 存在
        for uid in tool_use_ids:
            if uid in existing_result_ids:
                continue
            if uid in extracted_tool_results:
                user_msg["content"].append(extracted_tool_results[uid])
            else:
                user_msg["content"].append(
                    {
                        "type": "tool_result",
                        "tool_use_id": uid,
                        "content": "",
                        "is_error": True,
                    }
                )
                synthesized_ids.append(uid)

        i += 1

    if relocated_count:
        adaptations.append("misplaced_tool_result_relocated")
    if synthesized_ids:
        adaptations.append("orphaned_tool_use_repaired")
        logger.warning(
            "Vendor degradation adaptation: synthesized %d tool_result block(s) "
            "for orphaned tool_use to satisfy Anthropic pairing constraint. "
            "Affected tool_use_ids: %s",
            len(synthesized_ids),
            ", ".join(synthesized_ids),
        )

    return adaptations


def _strip_cache_control(body: dict[str, Any]) -> int:
    """从 system/messages/tools 中移除 cache_control 字段（就地）.

    部分供应商（GLM-5、OpenAI）不支持 Anthropic 的 cache_control 扩展，
    保留该字段可能导致请求被拒绝或产生意外行为。

    Returns:
        被移除的 cache_control 字段数量。
    """
    removed = 0

    # System prompt blocks
    system = body.get("system")
    if isinstance(system, list):
        for block in system:
            if isinstance(block, dict) and "cache_control" in block:
                del block["cache_control"]
                removed += 1

    # Message content blocks
    for message in body.get("messages", []):
        if not isinstance(message, dict):
            continue
        content = message.get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            if isinstance(block, dict) and "cache_control" in block:
                del block["cache_control"]
                removed += 1

    # Tools
    for tool in body.get("tools", []):
        if isinstance(tool, dict) and "cache_control" in tool:
            del tool["cache_control"]
            removed += 1

    return removed


def _remove_vendor_blocks(body: dict[str, Any], block_types: set[str]) -> int:
    """从 messages[].content[] 中就地移除指定 type 的内容块.

    用于剥离 vendor 私有 content block 类型（如 zhipu 的 ``server_tool_use_delta``），
    Anthropic API 会拒绝这些非标准块。

    Returns:
        被移除的块数量。
    """
    removed = 0
    for message in body.get("messages", []):
        if not isinstance(message, dict):
            continue
        content = message.get("content")
        if not isinstance(content, list):
            continue
        new_content: list[Any] = []
        for block in content:
            if isinstance(block, dict) and block.get("type") in block_types:
                removed += 1
                continue
            new_content.append(block)
        if content != new_content:
            if not new_content:
                new_content = [{"type": "text", "text": "[vendor_block_removed]"}]
                logger.info(
                    "Inserted placeholder text block after stripping "
                    "vendor blocks to avoid empty message content",
                )
            message["content"] = new_content
    return removed


def _rewrite_srvtoolu_ids(body: dict[str, Any]) -> tuple[int, dict[str, str]]:
    """将 zhipu 的 ``server_tool_use`` + ``srvtoolu_*`` ID 改写为标准 Anthropic 形式.

    Anthropic API 要求 tool_use 类型与 ``toolu_*`` 格式的 ID。Zhipu 的
    ``server_tool_use`` + ``srvtoolu_*`` 在上游 Anthropic 兼容端点可用，但无法
    透传至其他供应商；同时还需重写紧随其后 user 消息中 ``tool_result.tool_use_id``
    引用，保持配对关系。

    Returns:
        (rewritten_count, id_map) — 重写次数与 {原 ID: 新 ID} 映射。
    """
    id_map: dict[str, str] = {}
    counter = 0

    def next_id() -> str:
        nonlocal counter
        counter += 1
        return f"toolu_normalized_{counter}"

    for message in body.get("messages", []):
        if not isinstance(message, dict):
            continue
        content = message.get("content")
        if not isinstance(content, list):
            continue
        role = message.get("role")
        for block in content:
            if not isinstance(block, dict):
                continue
            block_type = block.get("type")
            block_id = block.get("id")

            # Case A: assistant 消息里的 server_tool_use / srvtoolu_* → 改写
            if role == "assistant" and block_type in {"tool_use", "server_tool_use"}:
                if isinstance(block_id, str) and _ANTHROPIC_SERVER_TOOL_USE_ID_RE.match(
                    block_id
                ):
                    new_id = next_id()
                    id_map[block_id] = new_id
                    block["id"] = new_id
                    block["type"] = "tool_use"
                elif (
                    isinstance(block_id, str)
                    and block_id
                    and not _ANTHROPIC_TOOL_USE_ID_RE.match(block_id)
                    and block.get("name")
                ):
                    # 非标准 ID（非 toolu_ / srvtoolu_），且具备 name 可改写
                    new_id = next_id()
                    id_map[block_id] = new_id
                    block["id"] = new_id
                    block["type"] = "tool_use"
                elif block_type == "server_tool_use" and isinstance(block_id, str):
                    # 兜底: 类型是 server_tool_use 但 ID 已是标准 toolu_ 形式，仅纠正类型
                    block["type"] = "tool_use"

            # Case B: user 消息里的 tool_result.tool_use_id 同步重写
            if block_type == "tool_result":
                tool_use_id = block.get("tool_use_id")
                if isinstance(tool_use_id, str) and tool_use_id in id_map:
                    block["tool_use_id"] = id_map[tool_use_id]

    return len(id_map), id_map


def infer_source_vendor_from_body(body: dict[str, Any]) -> str | None:
    """从请求 body 内容推断源供应商（仅在无会话上下文时作为兜底）.

    启发式（按置信度排序）:
    - 出现 ``srvtoolu_*`` 格式的 ID → zhipu
    - 出现 ``server_tool_use_delta`` 类型的 content block → zhipu
    - 出现 ``server_tool_use`` 块 + ``toolu_*`` ID → anthropic（beta 功能产物）

    原则: 只读扫描不修改 body；无匹配返回 None（视作纯净无需跨供应商清洗）。

    Args:
        body: Anthropic Messages 请求体。

    Returns:
        推断的源供应商名称（``"zhipu"`` 或 ``"anthropic"``），无法推断返回 None。
    """
    for message in body.get("messages", []):
        if not isinstance(message, dict):
            continue
        content = message.get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, dict):
                continue
            block_type = block.get("type")
            block_id = block.get("id")
            tool_use_id = block.get("tool_use_id")

            # Zhipu: server_tool_use_delta 是 zhipu 私有流式块（无歧义）
            if block_type == "server_tool_use_delta":
                return "zhipu"

            # srvtoolu_* ID（无论 block type）→ zhipu
            if isinstance(block_id, str) and _ANTHROPIC_SERVER_TOOL_USE_ID_RE.match(
                block_id
            ):
                return "zhipu"
            if isinstance(tool_use_id, str) and _ANTHROPIC_SERVER_TOOL_USE_ID_RE.match(
                tool_use_id
            ):
                return "zhipu"

            # server_tool_use 块 + toolu_* ID → Anthropic beta 功能
            if (
                block_type == "server_tool_use"
                and isinstance(block_id, str)
                and _ANTHROPIC_TOOL_USE_ID_RE.match(block_id)
            ):
                return "anthropic"

            # server_tool_use 块 + 非 toolu_/srvtoolu_ ID → 按类型兜底归 zhipu
            if block_type == "server_tool_use":
                return "zhipu"

    return None


# ── copilot → zhipu 转换通道 ─────────────────────────────────────


def prepare_copilot_to_zhipu(
    body: dict[str, Any],
) -> tuple[dict[str, Any], list[str]]:
    """copilot → zhipu 转换: 清理 copilot 产物以适配 GLM-5.

    GLM-5 的 Anthropic 兼容端点对以下特性支持不完整:
    - thinking / redacted_thinking 块 (signature 由非 Anthropic 签发)
    - cache_control 字段
    - 跨供应商产物 (misplaced tool_result, 非标准 tool_use ID)
    - 顶层 thinking / extended_thinking 参数

    Returns:
        (prepared_body, adaptations) — adaptations 为应用的变换描述列表。
    """
    prepared = copy.deepcopy(body)
    adaptations: list[str] = []

    # Step 1: 剥离 thinking/redacted_thinking 块
    stripped = strip_thinking_blocks(prepared)
    if stripped:
        adaptations.append(f"stripped_{stripped}_thinking_blocks")

    # Step 2: 移除 cache_control 字段
    removed_cc = _strip_cache_control(prepared)
    if removed_cc:
        adaptations.append(f"removed_{removed_cc}_cache_control_fields")

    # Step 3: 移除顶层 thinking/extended_thinking 参数（GLM-5 不支持）
    for param in ("thinking", "extended_thinking"):
        if param in prepared:
            del prepared[param]
            adaptations.append(f"removed_{param}_param")

    # Step 4: 强制 tool_use/tool_result 配对
    pairing_fixes = enforce_anthropic_tool_pairing(prepared.get("messages", []))
    if pairing_fixes:
        adaptations.extend(pairing_fixes)

    return prepared, adaptations


# ── anthropic → zhipu 转换通道 ────────────────────────────────────

# Anthropic beta 特有的 server_tool_use 块类型（web search, computer use 等）.
# 这些块在 Anthropic API 中有效，但 zhipu GLM-5 的兼容端点不支持。
# 注意: 这与 zhipu 自己的 server_tool_use（使用 srvtoolu_* ID）是不同的概念，
# 但它们共用同一个 type 名称 "server_tool_use"。
_ANTHROPIC_BETA_BLOCK_TYPES = {"server_tool_use"}


def prepare_anthropic_to_zhipu(
    body: dict[str, Any],
) -> tuple[dict[str, Any], list[str]]:
    """anthropic → zhipu 转换: 清理 anthropic 产物以适配 GLM-5.

    Anthropic API 可能产生的非兼容产物:
    - ``server_tool_use`` blocks（web search / computer use 等 beta 功能）
    - ``thinking`` / ``redacted_thinking`` blocks（含 Anthropic 签发的 signature）
    - ``cache_control`` 字段
    - 顶层 ``thinking`` / ``extended_thinking`` 参数

    Returns:
        (prepared_body, adaptations) — adaptations 为应用的变换描述列表。
    """
    prepared = copy.deepcopy(body)
    adaptations: list[str] = []

    # Step 1: 剥离 anthropic 的 server_tool_use blocks（web search, computer use 等）
    removed_stu = _remove_vendor_blocks(prepared, _ANTHROPIC_BETA_BLOCK_TYPES)
    if removed_stu:
        adaptations.append(f"removed_{removed_stu}_server_tool_use_blocks")

    # Step 2: 剥离 thinking/redacted_thinking blocks
    stripped = strip_thinking_blocks(prepared)
    if stripped:
        adaptations.append(f"stripped_{stripped}_thinking_blocks")

    # Step 3: 移除 cache_control 字段
    removed_cc = _strip_cache_control(prepared)
    if removed_cc:
        adaptations.append(f"removed_{removed_cc}_cache_control_fields")

    # Step 4: 移除顶层 thinking/extended_thinking 参数（GLM-5 不支持）
    for param in ("thinking", "extended_thinking"):
        if param in prepared:
            del prepared[param]
            adaptations.append(f"removed_{param}_param")

    # Step 5: 强制 tool_use/tool_result 配对
    pairing_fixes = enforce_anthropic_tool_pairing(prepared.get("messages", []))
    if pairing_fixes:
        adaptations.extend(pairing_fixes)

    return prepared, adaptations


# ── zhipu → copilot 转换通道 ─────────────────────────────────────


def prepare_zhipu_to_copilot(
    body: dict[str, Any],
) -> tuple[dict[str, Any], list[str]]:
    """zhipu → copilot 转换: 清理 zhipu 产物以确保 OpenAI 转换器稳定.

    Copilot 内部的 convert_openai_request() 处理 Anthropic→OpenAI 格式转换，
    但 zhipu 的跨供应商产物可能导致转换器输出异常:
    - 非 Anthropic 签发的 thinking signature
    - cache_control 字段（OpenAI 协议不支持）
    - 错位的 tool_result blocks

    注意: 不移除顶层 thinking 参数，由 copilot converter 自行映射。

    Returns:
        (prepared_body, adaptations) — adaptations 为应用的变换描述列表。
    """
    prepared = copy.deepcopy(body)
    adaptations: list[str] = []

    # Step 1: 剥离 zhipu 私有 content block 类型
    removed_vendor_blocks = _remove_vendor_blocks(prepared, _ZHIPU_VENDOR_BLOCK_TYPES)
    if removed_vendor_blocks:
        adaptations.append(f"removed_{removed_vendor_blocks}_zhipu_vendor_blocks")

    # Step 2: 改写 srvtoolu_* ID 与 server_tool_use 类型
    rewritten, _ = _rewrite_srvtoolu_ids(prepared)
    if rewritten:
        adaptations.append(f"rewritten_{rewritten}_srvtoolu_ids")

    # Step 3: 剥离 thinking/redacted_thinking 块
    stripped = strip_thinking_blocks(prepared)
    if stripped:
        adaptations.append(f"stripped_{stripped}_thinking_blocks")

    # Step 4: 移除 cache_control 字段
    removed_cc = _strip_cache_control(prepared)
    if removed_cc:
        adaptations.append(f"removed_{removed_cc}_cache_control_fields")

    # Step 5: 强制 tool_use/tool_result 配对
    pairing_fixes = enforce_anthropic_tool_pairing(prepared.get("messages", []))
    if pairing_fixes:
        adaptations.extend(pairing_fixes)

    return prepared, adaptations


# ── zhipu → anthropic 转换通道 ────────────────────────────────────


def prepare_zhipu_to_anthropic(
    body: dict[str, Any],
) -> tuple[dict[str, Any], list[str]]:
    """zhipu → anthropic 转换: 清理 zhipu 产物以适配 Anthropic API.

    Anthropic API 要求:
    - tool_use 类型与 ``toolu_*`` 格式 ID（zhipu 的 ``server_tool_use``/``srvtoolu_*`` 不兼容）
    - 每个 tool_use 必须在紧随的 user 消息中有对应 tool_result
    - thinking blocks 的 signature 必须是 Anthropic 签发（zhipu 签发的无效）
    - 不接受 ``server_tool_use_delta`` 等 zhipu 私有流式块类型

    此通道按顺序执行:
    1. 剥离 zhipu 私有 block 类型（``server_tool_use_delta``）
    2. 改写 ``srvtoolu_*`` ID 与 ``server_tool_use`` 类型为标准 Anthropic 形式
    3. 强制 tool_use/tool_result 配对（单遍正向扫描）
    4. 剥离 thinking blocks（signature 无效）

    所有变换均为幂等操作，安全地在已清理的请求体上重复执行。

    Returns:
        (prepared_body, adaptations) — adaptations 为应用的变换描述列表。
    """
    prepared = copy.deepcopy(body)
    adaptations: list[str] = []

    # Step 1: 剥离 zhipu 私有 content block 类型（如 server_tool_use_delta）
    removed_vendor_blocks = _remove_vendor_blocks(prepared, _ZHIPU_VENDOR_BLOCK_TYPES)
    if removed_vendor_blocks:
        adaptations.append(f"removed_{removed_vendor_blocks}_zhipu_vendor_blocks")

    # Step 2: 改写 srvtoolu_* ID 与 server_tool_use 类型
    rewritten, _ = _rewrite_srvtoolu_ids(prepared)
    if rewritten:
        adaptations.append(f"rewritten_{rewritten}_srvtoolu_ids")

    # Step 3: 强制 tool_use/tool_result 配对
    pairing_fixes = enforce_anthropic_tool_pairing(prepared.get("messages", []))
    if pairing_fixes:
        adaptations.extend(pairing_fixes)

    # Step 4: 剥离 thinking blocks（zhipu signature 无效）
    stripped = strip_thinking_blocks(prepared)
    if stripped:
        adaptations.append(f"stripped_{stripped}_thinking_blocks")

    return prepared, adaptations


# ── 注册所有转换通道 ──────────────────────────────────────────────

VENDOR_TRANSITIONS[("zhipu", "anthropic")] = prepare_zhipu_to_anthropic
VENDOR_TRANSITIONS[("zhipu", "copilot")] = prepare_zhipu_to_copilot
VENDOR_TRANSITIONS[("copilot", "zhipu")] = prepare_copilot_to_zhipu
VENDOR_TRANSITIONS[("anthropic", "zhipu")] = prepare_anthropic_to_zhipu
