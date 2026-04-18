"""供应商专属转换通道 — 跨供应商故障转移时的请求体预处理.

每个通道函数接收标准化的 Anthropic 格式请求体，返回针对目标供应商
调整后的请求体（深拷贝）。通道函数之间无依赖关系，可独立测试。

通道注册表 ``VENDOR_CHANNELS`` 提供统一的 tier_name → channel_fn 映射，
executor 层通过 ``get_channel()`` 查表分发，无需感知具体供应商。

通道矩阵:
    anthropic : prepare_for_anthropic  (tool pairing + 条件 thinking strip)
    zhipu     : prepare_for_zhipu      (剥离 thinking + cache_control + tool pairing + 移除 thinking 参数)
    copilot   : prepare_for_copilot    (剥离 thinking + cache_control + tool pairing)
"""

from __future__ import annotations

import copy
import logging
from collections.abc import Callable
from typing import Any

logger = logging.getLogger(__name__)

_THINKING_BLOCK_TYPES = {"thinking", "redacted_thinking"}

# ── 通道注册表 ────────────────────────────────────────────────
# tier_name → (body) → (prepared_body, adaptations)
VENDOR_CHANNELS: dict[
    str, Callable[[dict[str, Any]], tuple[dict[str, Any], list[str]]]
] = {}


def get_channel(tier_name: str) -> Callable | None:
    """查找供应商专属通道函数，不存在时返回 None."""
    return VENDOR_CHANNELS.get(tier_name)


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


# ── zhipu 专属转换通道 ────────────────────────────────────────


def prepare_for_zhipu(body: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    """zhipu 专属转换通道: 清理 GLM-5 不兼容的 Anthropic 协议扩展.

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


# ── copilot 专属转换通道 ──────────────────────────────────────


def prepare_for_copilot(body: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    """copilot 专属转换通道: 清理 Anthropic 格式请求以确保 OpenAI 转换器稳定.

    Copilot 内部的 convert_openai_request() 处理 Anthropic→OpenAI 格式转换，
    但某些跨供应商产物可能导致转换器输出异常:
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


# ── anthropic 专属转换通道 ─────────────────────────────────────


def prepare_for_anthropic(body: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    """anthropic 专属转换通道: tool pairing + 条件 thinking strip.

    Anthropic API 要求:
    - 每个 tool_use 必须在紧随的 user 消息中有对应 tool_result
    - thinking blocks 的 signature 必须是 Anthropic 签发（跨供应商后无效）

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

    # Step 2: 剥离 thinking blocks（跨供应商 signature 无效）
    stripped = strip_thinking_blocks(prepared)
    if stripped:
        adaptations.append(f"stripped_{stripped}_thinking_blocks")

    return prepared, adaptations


# ── 注册所有通道 ──────────────────────────────────────────────

VENDOR_CHANNELS["anthropic"] = prepare_for_anthropic
VENDOR_CHANNELS["zhipu"] = prepare_for_zhipu
VENDOR_CHANNELS["copilot"] = prepare_for_copilot
