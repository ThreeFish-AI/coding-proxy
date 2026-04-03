"""OpenAI Chat Completions → Anthropic Messages 转换."""

from __future__ import annotations

import json
from typing import Any


def convert_response(response: dict[str, Any]) -> dict[str, Any]:
    """转换 OpenAI chat.completions 响应为 Anthropic message 响应."""
    choices = response.get("choices", [])
    text_blocks: list[dict[str, Any]] = []
    tool_use_blocks: list[dict[str, Any]] = []
    finish_reason = None

    for choice in choices:
        if not isinstance(choice, dict):
            continue
        finish_reason = choice.get("finish_reason") or finish_reason
        message = choice.get("message", {})
        reasoning_content = message.get("reasoning_content")
        if isinstance(reasoning_content, str) and reasoning_content:
            text_blocks.append({"type": "thinking", "thinking": reasoning_content})
        content = message.get("content")
        if isinstance(content, str) and content:
            text_blocks.append({"type": "text", "text": content})
        elif isinstance(content, list):
            for part in content:
                if isinstance(part, dict) and part.get("type") == "text" and part.get("text"):
                    text_blocks.append({"type": "text", "text": part["text"]})

        for tool_call in message.get("tool_calls", []) or []:
            if not isinstance(tool_call, dict):
                continue
            function = tool_call.get("function", {})
            arguments = function.get("arguments", "{}")
            try:
                parsed_arguments = json.loads(arguments) if isinstance(arguments, str) else arguments
            except json.JSONDecodeError:
                parsed_arguments = {}
            tool_use_blocks.append({
                "type": "tool_use",
                "id": tool_call.get("id", ""),
                "name": function.get("name", ""),
                "input": parsed_arguments if isinstance(parsed_arguments, dict) else {},
            })

    usage = response.get("usage", {}) or {}
    cached_tokens = ((usage.get("prompt_tokens_details") or {}).get("cached_tokens", 0))
    content_blocks = [*text_blocks, *tool_use_blocks]

    return {
        "id": response.get("request_id", "") or response.get("id", ""),
        "type": "message",
        "role": "assistant",
        "model": response.get("model", ""),
        "content": content_blocks,
        "stop_reason": _map_stop_reason(finish_reason),
        "stop_sequence": None,
        "usage": {
            "input_tokens": max((usage.get("prompt_tokens", 0) or 0) - cached_tokens, 0),
            "output_tokens": usage.get("completion_tokens", 0) or 0,
            **({"cache_read_input_tokens": cached_tokens} if cached_tokens else {}),
        },
    }


def _map_stop_reason(reason: str | None) -> str | None:
    if reason is None:
        return None
    mapping = {
        "stop": "end_turn",
        "length": "max_tokens",
        "tool_calls": "tool_use",
        "content_filter": "end_turn",
    }
    return mapping.get(reason, "end_turn")
