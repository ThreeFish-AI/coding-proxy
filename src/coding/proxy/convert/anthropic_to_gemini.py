"""Anthropic Messages API 请求 → Google Gemini 格式转换."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

_ROLE_MAP = {"assistant": "model", "user": "user"}
_SEARCH_TOOL_NAMES = {
    "web_search",
    "google_search",
    "web_search_20250305",
    "google_search_retrieval",
    "builtin_web_search",
}
_TOOL_CHOICE_MODE = {
    "auto": "AUTO",
    "any": "ANY",
    "required": "ANY",
}


@dataclass
class ConversionResult:
    """转换结果与适配诊断."""

    body: dict[str, Any]
    adaptations: list[str] = field(default_factory=list)


def convert_request(
    anthropic_body: dict[str, Any],
    *,
    model: str | None = None,
) -> ConversionResult:
    """将 Anthropic Messages API 请求体转换为 Gemini 格式."""
    adaptations: list[str] = []
    tool_name_by_id: dict[str, str] = {}
    result: dict[str, Any] = {}

    system_instruction = _convert_system(anthropic_body.get("system"))
    if system_instruction is not None:
        result["systemInstruction"] = system_instruction

    messages = anthropic_body.get("messages", [])
    result["contents"] = _convert_messages(messages, tool_name_by_id, adaptations)

    generation_config = _build_generation_config(anthropic_body, model=model, adaptations=adaptations)
    if generation_config:
        result["generationConfig"] = generation_config

    tools, tool_config = _build_tools(anthropic_body, adaptations)
    if tools:
        result["tools"] = tools
    if tool_config:
        result["toolConfig"] = tool_config

    if "metadata" in anthropic_body:
        metadata = anthropic_body.get("metadata") or {}
        if isinstance(metadata, dict) and metadata.get("user_id"):
            adaptations.append("metadata_user_id_not_forwarded")
        else:
            adaptations.append("metadata_ignored")

    return ConversionResult(body=result, adaptations=_dedupe(adaptations))


def _dedupe(items: list[str]) -> list[str]:
    return list(dict.fromkeys(items))


def _convert_system(system: str | list[dict] | None) -> dict[str, Any] | None:
    if system is None:
        return None
    if isinstance(system, str):
        return {"parts": [{"text": system}]}
    parts = []
    for block in system:
        if isinstance(block, dict) and block.get("type") == "text":
            parts.append({"text": block["text"]})
    return {"parts": parts} if parts else None


def _convert_messages(
    messages: list[dict[str, Any]],
    tool_name_by_id: dict[str, str],
    adaptations: list[str],
) -> list[dict[str, Any]]:
    contents: list[dict[str, Any]] = []
    for msg in messages:
        role = _ROLE_MAP.get(msg.get("role", "user"), "user")
        parts = _convert_content(msg.get("content", ""), tool_name_by_id, adaptations)
        if parts:
            contents.append({"role": role, "parts": parts})
    return contents


def _convert_content(
    content: str | list[dict[str, Any]],
    tool_name_by_id: dict[str, str],
    adaptations: list[str],
) -> list[dict[str, Any]]:
    if isinstance(content, str):
        return [{"text": content}] if content else []

    parts: list[dict[str, Any]] = []
    for block in content:
        block_type = block.get("type", "")
        if block_type == "text":
            text = block.get("text", "")
            if text:
                parts.append({"text": text})
        elif block_type == "thinking":
            text = block.get("thinking", "")
            if text:
                part: dict[str, Any] = {"text": text, "thought": True}
                signature = block.get("signature")
                if signature:
                    part["thoughtSignature"] = signature
                else:
                    adaptations.append("thinking_signature_missing")
                parts.append(part)
        elif block_type == "redacted_thinking":
            data = block.get("data", "")
            if data:
                parts.append({"text": f"[Redacted Thinking: {data}]", "thought": True})
                adaptations.append("redacted_thinking_downgraded")
        elif block_type == "image":
            source = block.get("source", {})
            if source.get("type") == "base64":
                parts.append({
                    "inlineData": {
                        "mimeType": source.get("media_type", "image/png"),
                        "data": source.get("data", ""),
                    }
                })
        elif block_type == "tool_use":
            name = block.get("name", "")
            tool_id = block.get("id", "")
            if tool_id and name:
                tool_name_by_id[tool_id] = name
            part = {
                "functionCall": {
                    "name": name,
                    "args": block.get("input", {}),
                    "id": tool_id or None,
                }
            }
            signature = block.get("signature")
            if signature:
                part["thoughtSignature"] = signature
            parts.append(part)
        elif block_type == "tool_result":
            tool_use_id = block.get("tool_use_id", "")
            tool_content = block.get("content", "")
            text = _stringify_tool_content(tool_content)
            parts.append({
                "functionResponse": {
                    "name": tool_name_by_id.get(tool_use_id, tool_use_id),
                    "response": {"result": text},
                    "id": tool_use_id or None,
                }
            })
            if tool_use_id and tool_use_id not in tool_name_by_id:
                adaptations.append("tool_result_name_fallback_to_tool_use_id")
        else:
            logger.debug("跳过不支持的内容块类型: %s", block_type)
    return parts


def _stringify_tool_content(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        chunks: list[str] = []
        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "text" and isinstance(block.get("text"), str):
                chunks.append(block["text"])
            elif block.get("type") == "image":
                chunks.append("[image]")
        return "\n".join(chunks)
    return str(content)


def _build_generation_config(
    body: dict[str, Any],
    *,
    model: str | None,
    adaptations: list[str],
) -> dict[str, Any]:
    config: dict[str, Any] = {}

    if "max_tokens" in body:
        config["maxOutputTokens"] = body["max_tokens"]
    if "temperature" in body:
        config["temperature"] = body["temperature"]
    if "top_p" in body:
        config["topP"] = body["top_p"]
    if "top_k" in body:
        config["topK"] = body["top_k"]
    if "stop_sequences" in body:
        config["stopSequences"] = body["stop_sequences"]

    thinking_cfg = body.get("thinking") or body.get("extended_thinking")
    if thinking_cfg:
        config["thinkingConfig"] = {
            "includeThoughts": True,
        }
        if isinstance(thinking_cfg, dict):
            budget = thinking_cfg.get("budget_tokens")
            if isinstance(budget, int) and budget > 0:
                config["thinkingConfig"]["thinkingBudget"] = budget
            effort = thinking_cfg.get("effort")
            if isinstance(effort, str) and effort:
                config["thinkingConfig"]["thinkingLevel"] = effort

    has_tools = bool(body.get("tools"))
    has_tool_use = any(
        isinstance(msg.get("content"), list)
        and any(
            isinstance(block, dict) and block.get("type") == "tool_use"
            for block in msg["content"]
        )
        for msg in body.get("messages", [])
    )
    if config.get("thinkingConfig") and has_tools and has_tool_use and model and not model.startswith("gemini-"):
        del config["thinkingConfig"]
        adaptations.append("thinking_disabled_for_tool_call_compatibility")

    return config


def _build_tools(
    body: dict[str, Any],
    adaptations: list[str],
) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
    source_tools = body.get("tools") or []
    if not source_tools:
        if body.get("tool_choice"):
            adaptations.append("tool_choice_ignored_without_tools")
        return [], None

    function_declarations: list[dict[str, Any]] = []
    include_search = False
    for tool in source_tools:
        if not isinstance(tool, dict):
            continue
        tool_name = str(tool.get("name") or tool.get("type") or "")
        if tool_name in _SEARCH_TOOL_NAMES:
            include_search = True
            adaptations.append("search_tool_mapped_to_google_search")
            continue
        declaration: dict[str, Any] = {"name": tool_name}
        description = tool.get("description")
        if isinstance(description, str) and description:
            declaration["description"] = description
        input_schema = tool.get("input_schema")
        if isinstance(input_schema, dict):
            declaration["parameters"] = input_schema
        function_declarations.append(declaration)

    tools: list[dict[str, Any]] = []
    if function_declarations:
        tools.append({"functionDeclarations": function_declarations})
    if include_search:
        tools.append({"googleSearch": {}})

    tool_config: dict[str, Any] | None = None
    tool_choice = body.get("tool_choice")
    if tool_choice and function_declarations:
        mode = "AUTO"
        allowed_names: list[str] | None = None
        if isinstance(tool_choice, str):
            mode = _TOOL_CHOICE_MODE.get(tool_choice, "AUTO")
        elif isinstance(tool_choice, dict):
            choice_type = str(tool_choice.get("type", "")).lower()
            mode = _TOOL_CHOICE_MODE.get(choice_type, "AUTO")
            if choice_type == "tool":
                name = tool_choice.get("name")
                if isinstance(name, str) and name:
                    mode = "ANY"
                    allowed_names = [name]
                    adaptations.append("tool_choice_tool_mapped_to_allowed_function_names")
        tool_config = {"functionCallingConfig": {"mode": mode}}
        if allowed_names:
            tool_config["functionCallingConfig"]["allowedFunctionNames"] = allowed_names

    return tools, tool_config
