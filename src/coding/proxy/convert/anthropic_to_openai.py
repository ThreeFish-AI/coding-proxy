"""Anthropic Messages → OpenAI Chat Completions 转换."""

from __future__ import annotations

import json
import logging
import re
from typing import Any

logger = logging.getLogger(__name__)


def convert_request(body: dict[str, Any]) -> dict[str, Any]:
    """转换 Anthropic Messages 请求为 OpenAI chat.completions 负载."""
    result: dict[str, Any] = {
        "model": _translate_model_name(body.get("model", "")),
        "messages": _translate_messages(body.get("messages", []), body.get("system")),
    }

    scalar_mappings = {
        "max_tokens": "max_tokens",
        "temperature": "temperature",
        "top_p": "top_p",
        "stream": "stream",
    }
    for source_key, target_key in scalar_mappings.items():
        value = body.get(source_key)
        if value is not None:
            result[target_key] = value

    stop_sequences = body.get("stop_sequences")
    if stop_sequences is not None:
        result["stop"] = stop_sequences

    # Metadata：user_id 映射到 OpenAI user 字段，其余完整透传
    metadata = body.get("metadata")
    if isinstance(metadata, dict):
        if metadata.get("user_id"):
            result["user"] = metadata["user_id"]
        extra_metadata = {k: v for k, v in metadata.items() if k != "user_id"}
        if extra_metadata:
            result["metadata"] = metadata
            logger.debug(
                "copilot: metadata forwarded with keys: %s",
                list(metadata.keys()),
            )

    request_id = body.get("request_id")
    if isinstance(request_id, str) and request_id:
        result["request_id"] = request_id

    response_format = body.get("response_format")
    if isinstance(response_format, dict) and response_format.get("type"):
        result["response_format"] = response_format

    # Thinking / Extended Thinking → reasoning_effort 映射
    thinking_params = _translate_thinking(body)
    if thinking_params:
        result.update(thinking_params)
        logger.debug("copilot: thinking params mapped: %s", thinking_params)

    tools = body.get("tools")
    if tools:
        result["tools"] = [_translate_tool(tool) for tool in tools]

    tool_choice = body.get("tool_choice")
    translated_tool_choice = _translate_tool_choice(tool_choice)
    if translated_tool_choice is not None:
        result["tool_choice"] = translated_tool_choice

    if body.get("stream"):
        result["stream_options"] = {"include_usage": True}

    return result


def _translate_model_name(model: str) -> str:
    """精细化模型名映射.

    Copilot 可用格式: claude-{family}-{major}[.{minor}]
    Anthropic 请求格式: claude-{family}-{major}-YYYYMMDD 或 claude-{family}-{major}.{minor}-YYYYMMDD
    """
    # 已是 Copilot 原生格式（含可选 minor 版本）直接透传
    copilot_pattern = re.match(r"^claude-(sonnet|opus|haiku)-\d+(\.\d+)?$", model)
    if copilot_pattern:
        logger.debug("copilot: model name already in Copilot format: %s", model)
        return model

    # 现有逻辑：去除日期后缀（4.x 无 minor 版本）
    if model.startswith("claude-sonnet-4-"):
        return "claude-sonnet-4"
    if model.startswith("claude-opus-4-"):
        return "claude-opus-4"
    if model.startswith("claude-haiku-4-"):
        return "claude-haiku-4"

    # 新增：处理带 minor 版本的 Anthropic 格式
    # 例如 claude-sonnet-4.6-20250514 -> claude-sonnet-4.6
    versioned_match = re.match(
        r"^(claude-(?:sonnet|opus|haiku))-(\d+\.\d+)-\d+$", model
    )
    if versioned_match:
        family = versioned_match.group(1)
        version = versioned_match.group(2)
        normalized = f"{family}-{version}"
        logger.debug("copilot: model name normalized: %s -> %s", model, normalized)
        return normalized

    return model


def _translate_thinking(body: dict[str, Any]) -> dict[str, Any] | None:
    """将 Anthropic thinking/extended_thinking 映射为 OpenAI 推理参数.

    映射策略：
    - extended_thinking.effort ("low"/"medium"/"high") → reasoning_effort 同值
    - thinking: True / {type:"enabled"} → reasoning_effort "medium"
    - budget_tokens → 记录 DEBUG 日志（OpenAI 无直接对应字段）
    """
    # 优先检查 extended_thinking（Claude Code 主要使用方式）
    extended = body.get("extended_thinking")
    if isinstance(extended, dict):
        effort = extended.get("effort", "")
        budget = extended.get("budget_tokens")
        result: dict[str, Any] = {}
        if effort:
            result["reasoning_effort"] = effort
        if isinstance(budget, int) and budget > 0:
            logger.debug(
                "copilot: extended_thinking.budget_tokens=%d "
                "(OpenAI 无直接对应字段, 记录供调试)",
                budget,
            )
        return result if result else None

    # 回退到简单 thinking 布尔标志 / 字典型式（任意非空 dict 均视为启用）
    thinking = body.get("thinking")
    if thinking is True or isinstance(thinking, dict):
        return {"reasoning_effort": "medium"}

    return None


def _translate_messages(
    messages: list[dict[str, Any]],
    system: str | list[dict[str, Any]] | None,
) -> list[dict[str, Any]]:
    translated: list[dict[str, Any]] = []
    translated.extend(_translate_system(system))
    for message in messages:
        role = message.get("role")
        if role == "user":
            translated.extend(_translate_user_message(message))
        elif role == "assistant":
            translated.extend(_translate_assistant_message(message))
    return translated


def _translate_system(
    system: str | list[dict[str, Any]] | None,
) -> list[dict[str, Any]]:
    """转换 system prompt，保留 cache_control 边界信息（通过 DEBUG 日志）.

    OpenAI 的 system role message 不原生支持 cache_control block。
    策略：提取所有 text 内容并拼接，检测 cache_control 时记录日志供调试。
    """
    if not system:
        return []
    if isinstance(system, str):
        return [{"role": "system", "content": system}]

    parts: list[str] = []
    cache_control_count = 0
    for block in system:
        if not isinstance(block, dict):
            continue
        if block.get("type") == "text":
            text = block.get("text", "")
            if text:
                parts.append(text)
            if "cache_control" in block:
                cache_control_count += 1

    if cache_control_count > 0:
        text = "\n\n".join(part for part in parts if part)
        logger.debug(
            "copilot: system prompt had %d cache_control block(s), "
            "collapsed into single system message (%d chars)",
            cache_control_count,
            len(text),
        )

    text = "\n\n".join(part for part in parts if part)
    return [{"role": "system", "content": text}] if text else []


def _translate_user_message(message: dict[str, Any]) -> list[dict[str, Any]]:
    content = message.get("content")
    if not isinstance(content, list):
        return [{"role": "user", "content": content or ""}]

    translated: list[dict[str, Any]] = []
    tool_results = [
        block
        for block in content
        if isinstance(block, dict) and block.get("type") == "tool_result"
    ]
    other_blocks = [
        block
        for block in content
        if isinstance(block, dict) and block.get("type") != "tool_result"
    ]

    for block in tool_results:
        tool_result_content = _map_block_content(block.get("content", ""))
        is_error = block.get("is_error", False)
        if is_error:
            logger.debug(
                "copilot: tool_result is_error=True for tool_use_id=%s "
                "(OpenAI 不原生支持 is_error, 注入 [ERROR] 前缀到 content)",
                block.get("tool_use_id", ""),
            )
            tool_result_content = f"[ERROR]\n{tool_result_content}"
        translated.append(
            {
                "role": "tool",
                "tool_call_id": block.get("tool_use_id", ""),
                "content": tool_result_content,
            }
        )

    if other_blocks:
        translated.append(
            {
                "role": "user",
                "content": _map_block_content(other_blocks),
            }
        )
    return translated


def _translate_assistant_message(message: dict[str, Any]) -> list[dict[str, Any]]:
    content = message.get("content")
    if not isinstance(content, list):
        return [{"role": "assistant", "content": content or ""}]

    tool_uses = [
        block
        for block in content
        if isinstance(block, dict) and block.get("type") == "tool_use"
    ]
    text_parts: list[str] = []
    thinking_parts: list[str] = []

    for block in content:
        if not isinstance(block, dict):
            continue
        if block.get("type") == "text":
            text_parts.append(block.get("text", ""))
        elif block.get("type") == "thinking":
            # 不再合并到文本，而是独立收集
            thinking_content = block.get("thinking", "")
            if thinking_content:
                thinking_parts.append(thinking_content)

    # 构建最终内容：根据 thinking 和 text 的组合情况决定策略
    final_text_parts: list[str] = []
    if thinking_parts and not text_parts and not tool_uses:
        # 只有 thinking 没有 text 也没有工具调用时，用 thinking 作为 content（降级方案）
        final_text_parts = thinking_parts
    elif thinking_parts and text_parts:
        # 同时存在时，在 text 前加上 thinking 标记（让模型知道上下文）
        logger.debug(
            "copilot: assistant message has both thinking (%d blocks) and text (%d blocks), "
            "thinking will be prepended as [Thinking]...[/Thinking] context",
            len(thinking_parts),
            len(text_parts),
        )
        final_text_parts = [
            f"[Thinking]\n{''.join(thinking_parts)}\n[/Thinking]\n\n",
            *text_parts,
        ]
    else:
        final_text_parts = text_parts

    if tool_uses:
        tool_calls: list[dict[str, Any]] = []
        for block in tool_uses:
            raw_input = block.get("input")
            if not isinstance(raw_input, dict):
                logger.debug(
                    "copilot: tool_use id=%s name=%s has non-dict input (type=%s), "
                    "defaulting to empty dict",
                    block.get("id", ""),
                    block.get("name", ""),
                    type(raw_input).__name__,
                )
                raw_input = {}
            tool_calls.append(
                {
                    "id": block.get("id", ""),
                    "type": "function",
                    "function": {
                        "name": block.get("name", ""),
                        "arguments": json.dumps(raw_input, ensure_ascii=False),
                    },
                }
            )
        return [
            {
                "role": "assistant",
                "content": "\n\n".join(part for part in final_text_parts if part)
                or None,
                "tool_calls": tool_calls,
            }
        ]

    return [
        {
            "role": "assistant",
            "content": _map_block_content(content)
            if not thinking_parts and not tool_uses
            else "\n\n".join(part for part in final_text_parts if part) or "",
        }
    ]


def _map_block_content(content: Any) -> Any:
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return None

    has_image = any(
        isinstance(block, dict) and block.get("type") == "image" for block in content
    )
    if not has_image:
        parts: list[str] = []
        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "text":
                parts.append(block.get("text", ""))
            elif block.get("type") == "thinking":
                parts.append(block.get("thinking", ""))
        return "\n\n".join(part for part in parts if part)

    translated: list[dict[str, Any]] = []
    for block in content:
        if not isinstance(block, dict):
            continue
        if block.get("type") == "text":
            translated.append({"type": "text", "text": block.get("text", "")})
        elif block.get("type") == "thinking":
            translated.append({"type": "text", "text": block.get("thinking", "")})
        elif block.get("type") == "image":
            source = block.get("source", {})
            translated.append(
                {
                    "type": "image_url",
                    "image_url": {
                        "url": f"data:{source.get('media_type', 'image/png')};base64,{source.get('data', '')}",
                    },
                }
            )
    return translated


def _translate_tool(tool: dict[str, Any]) -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": tool.get("name", ""),
            "description": tool.get("description"),
            "parameters": tool.get("input_schema", {}),
        },
    }


def _translate_tool_choice(
    tool_choice: dict[str, Any] | None,
) -> str | dict[str, Any] | None:
    if not isinstance(tool_choice, dict):
        return None
    choice_type = tool_choice.get("type")
    if choice_type == "auto":
        return "auto"
    if choice_type == "any":
        return "required"
    if choice_type == "none":
        return "none"
    if choice_type == "tool" and tool_choice.get("name"):
        return {
            "type": "function",
            "function": {"name": tool_choice["name"]},
        }
    return None
