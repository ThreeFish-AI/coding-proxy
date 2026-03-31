"""Anthropic Messages → OpenAI Chat Completions 转换."""

from __future__ import annotations

import json
from typing import Any


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

    metadata = body.get("metadata")
    if isinstance(metadata, dict) and metadata.get("user_id"):
        result["user"] = metadata["user_id"]

    tools = body.get("tools")
    if tools:
        result["tools"] = [_translate_tool(tool) for tool in tools]

    tool_choice = body.get("tool_choice")
    translated_tool_choice = _translate_tool_choice(tool_choice)
    if translated_tool_choice is not None:
        result["tool_choice"] = translated_tool_choice

    return result


def _translate_model_name(model: str) -> str:
    if model.startswith("claude-sonnet-4-"):
        return "claude-sonnet-4"
    if model.startswith("claude-opus-4-"):
        return "claude-opus-4"
    return model


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


def _translate_system(system: str | list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    if not system:
        return []
    if isinstance(system, str):
        return [{"role": "system", "content": system}]
    parts = [block.get("text", "") for block in system if isinstance(block, dict) and block.get("type") == "text"]
    text = "\n\n".join(part for part in parts if part)
    return [{"role": "system", "content": text}] if text else []


def _translate_user_message(message: dict[str, Any]) -> list[dict[str, Any]]:
    content = message.get("content")
    if not isinstance(content, list):
        return [{"role": "user", "content": content or ""}]

    translated: list[dict[str, Any]] = []
    tool_results = [block for block in content if isinstance(block, dict) and block.get("type") == "tool_result"]
    other_blocks = [block for block in content if isinstance(block, dict) and block.get("type") != "tool_result"]

    for block in tool_results:
        translated.append({
            "role": "tool",
            "tool_call_id": block.get("tool_use_id", ""),
            "content": _map_block_content(block.get("content", "")),
        })

    if other_blocks:
        translated.append({
            "role": "user",
            "content": _map_block_content(other_blocks),
        })
    return translated


def _translate_assistant_message(message: dict[str, Any]) -> list[dict[str, Any]]:
    content = message.get("content")
    if not isinstance(content, list):
        return [{"role": "assistant", "content": content or ""}]

    tool_uses = [block for block in content if isinstance(block, dict) and block.get("type") == "tool_use"]
    text_parts: list[str] = []
    for block in content:
        if not isinstance(block, dict):
            continue
        if block.get("type") == "text":
            text_parts.append(block.get("text", ""))
        elif block.get("type") == "thinking":
            text_parts.append(block.get("thinking", ""))

    if tool_uses:
        return [{
            "role": "assistant",
            "content": "\n\n".join(part for part in text_parts if part) or None,
            "tool_calls": [
                {
                    "id": block.get("id", ""),
                    "type": "function",
                    "function": {
                        "name": block.get("name", ""),
                        "arguments": json.dumps(block.get("input", {}), ensure_ascii=False),
                    },
                }
                for block in tool_uses
            ],
        }]

    return [{
        "role": "assistant",
        "content": _map_block_content(content),
    }]


def _map_block_content(content: Any) -> Any:
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return None

    has_image = any(isinstance(block, dict) and block.get("type") == "image" for block in content)
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
            translated.append({
                "type": "image_url",
                "image_url": {
                    "url": f"data:{source.get('media_type', 'image/png')};base64,{source.get('data', '')}",
                },
            })
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


def _translate_tool_choice(tool_choice: dict[str, Any] | None) -> str | dict[str, Any] | None:
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
