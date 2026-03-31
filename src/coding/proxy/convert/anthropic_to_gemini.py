"""Anthropic Messages API 请求 → Google Gemini/Vertex AI 格式转换."""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

# Anthropic → Gemini 角色映射
_ROLE_MAP = {"assistant": "model", "user": "user"}

# 不支持直接转换的 Anthropic 顶层字段（静默剥离并记录 WARNING）
_UNSUPPORTED_FIELDS = frozenset({
    "tools", "tool_choice", "metadata",
    "extended_thinking", "thinking",
})


def convert_request(anthropic_body: dict[str, Any]) -> dict[str, Any]:
    """将 Anthropic Messages API 请求体转换为 Gemini 格式.

    转换映射:
    - messages → contents（角色映射 + 内容块转换）
    - system → systemInstruction
    - max_tokens / temperature / top_p / top_k / stop_sequences → generationConfig
    - stream → 移除（由调用方控制）

    不支持的字段静默剥离并记录 WARNING。
    """
    result: dict[str, Any] = {}

    # 系统提示
    system_instruction = _convert_system(anthropic_body.get("system"))
    if system_instruction is not None:
        result["systemInstruction"] = system_instruction

    # 消息列表
    messages = anthropic_body.get("messages", [])
    result["contents"] = _convert_messages(messages)

    # 生成参数
    gen_config = _build_generation_config(anthropic_body)
    if gen_config:
        result["generationConfig"] = gen_config

    # 记录不支持的字段
    for field in _UNSUPPORTED_FIELDS:
        if field in anthropic_body:
            logger.warning("Antigravity 不支持字段 '%s'，已忽略", field)

    return result


def _convert_system(system: str | list[dict] | None) -> dict[str, Any] | None:
    """转换系统提示: system → systemInstruction."""
    if system is None:
        return None
    if isinstance(system, str):
        return {"parts": [{"text": system}]}
    # 列表形式: [{type: "text", text: "..."}]
    parts = []
    for block in system:
        if isinstance(block, dict) and block.get("type") == "text":
            parts.append({"text": block["text"]})
    return {"parts": parts} if parts else None


def _convert_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """转换消息列表: Anthropic messages → Gemini contents."""
    contents: list[dict[str, Any]] = []
    for msg in messages:
        role = _ROLE_MAP.get(msg.get("role", "user"), "user")
        parts = _convert_content(msg.get("content", ""))
        if parts:
            contents.append({"role": role, "parts": parts})
    return contents


def _convert_content(content: str | list[dict[str, Any]]) -> list[dict[str, Any]]:
    """转换单条消息内容: str 或 content_block[] → parts[]."""
    if isinstance(content, str):
        return [{"text": content}] if content else []

    parts: list[dict[str, Any]] = []
    for block in content:
        block_type = block.get("type", "")
        if block_type == "text":
            text = block.get("text", "")
            if text:
                parts.append({"text": text})
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
            parts.append({
                "functionCall": {
                    "name": block.get("name", ""),
                    "args": block.get("input", {}),
                }
            })
        elif block_type == "tool_result":
            tool_content = block.get("content", "")
            text = tool_content if isinstance(tool_content, str) else str(tool_content)
            parts.append({
                "functionResponse": {
                    "name": block.get("tool_use_id", ""),
                    "response": {"result": text},
                }
            })
        else:
            logger.debug("跳过不支持的内容块类型: %s", block_type)
    return parts


def _build_generation_config(body: dict[str, Any]) -> dict[str, Any]:
    """提取生成参数 → generationConfig."""
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

    return config
