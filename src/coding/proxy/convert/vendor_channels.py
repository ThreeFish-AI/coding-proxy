"""供应商跨供应商转换通道 — 源→目标绑定的请求体预处理.

每个通道函数表示一个具体的「源 vendor → 目标 vendor」绑定转换关系，
接收标准化的 Anthropic 格式请求体，返回清理跨供应商产物后的请求体（深拷贝）。

通道注册表 ``VENDOR_TRANSITIONS`` 提供统一的 (source, target) → channel_fn 映射，
executor 层通过 ``get_transition_channel()`` 查表分发，无需感知具体供应商逻辑。

转换矩阵（仅注册需要转换的源→目标对，未注册的不触发任何通道）:
    zhipu → anthropic : prepare_zhipu_to_anthropic  (剥离 thinking + tool pairing)
    zhipu → copilot   : prepare_zhipu_to_copilot    (剥离 thinking + cache_control + tool pairing)
    copilot → zhipu   : prepare_copilot_to_zhipu    (剥离 thinking + cache_control + 移除 thinking 参数 + tool pairing)
"""

from __future__ import annotations

import copy
import logging
from collections.abc import Callable
from typing import Any

logger = logging.getLogger(__name__)

_THINKING_BLOCK_TYPES = {"thinking", "redacted_thinking"}

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


def _strip_thinking_blocks_inplace(body: dict[str, Any]) -> int:
    """从 assistant 消息中移除 thinking/redacted_thinking 块（就地）.

    GLM-5 等 Anthropic 兼容端点不支持非 Anthropic 签发的 thinking signature，
    跨供应商场景中必须剥离以避免 400 invalid_request_error。

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
        message["content"] = new_content
        stripped += removed
    return stripped


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
    stripped = _strip_thinking_blocks_inplace(prepared)
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
    from ..server.request_normalizer import enforce_anthropic_tool_pairing

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

    # Step 1: 剥离 thinking/redacted_thinking 块
    stripped = _strip_thinking_blocks_inplace(prepared)
    if stripped:
        adaptations.append(f"stripped_{stripped}_thinking_blocks")

    # Step 2: 移除 cache_control 字段
    removed_cc = _strip_cache_control(prepared)
    if removed_cc:
        adaptations.append(f"removed_{removed_cc}_cache_control_fields")

    # Step 3: 强制 tool_use/tool_result 配对
    from ..server.request_normalizer import enforce_anthropic_tool_pairing

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
    - 每个 tool_use 必须在紧随的 user 消息中有对应 tool_result
    - thinking blocks 的 signature 必须是 Anthropic 签发（zhipu 签发的无效）

    此通道执行两项变换:
    1. enforce_anthropic_tool_pairing: 单遍正向扫描修复配对
    2. strip_thinking_blocks: 移除非 Anthropic 签发的 thinking 块

    两项变换均为幂等操作，安全地在已清理的请求体上重复执行。

    Returns:
        (prepared_body, adaptations) — adaptations 为应用的变换描述列表。
    """
    from ..server.request_normalizer import (
        enforce_anthropic_tool_pairing,
        strip_thinking_blocks,
    )

    prepared = copy.deepcopy(body)
    adaptations: list[str] = []

    # Step 1: 强制 tool_use/tool_result 配对
    pairing_fixes = enforce_anthropic_tool_pairing(prepared.get("messages", []))
    if pairing_fixes:
        adaptations.extend(pairing_fixes)

    # Step 2: 剥离 thinking blocks（zhipu signature 无效）
    stripped = strip_thinking_blocks(prepared)
    if stripped:
        adaptations.append(f"stripped_{stripped}_thinking_blocks")

    return prepared, adaptations


# ── 注册所有转换通道 ──────────────────────────────────────────────

VENDOR_TRANSITIONS[("zhipu", "anthropic")] = prepare_zhipu_to_anthropic
VENDOR_TRANSITIONS[("zhipu", "copilot")] = prepare_zhipu_to_copilot
VENDOR_TRANSITIONS[("copilot", "zhipu")] = prepare_copilot_to_zhipu
