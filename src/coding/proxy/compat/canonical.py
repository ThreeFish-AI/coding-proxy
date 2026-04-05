"""供应商无关的 Claude / Anthropic 语义抽象.

类型定义已迁移至 :mod:`coding.proxy.model.compat`。
本文件保留 ``build_canonical_request()`` 等构建逻辑，类型通过 re-export 提供。

.. deprecated::
    未来版本将移除类型 re-export，请直接从 :mod:`coding.proxy.model.compat` 导入类型。
"""

from __future__ import annotations

import hashlib
import json
import uuid
from typing import Any

from ..model.compat import (  # noqa: F401
    CanonicalMessagePart,
    CanonicalPartType,
    CanonicalRequest,
    CanonicalThinking,
    CanonicalToolCall,
    CompatibilityDecision,
    CompatibilityProfile,
    CompatibilityStatus,
    CompatibilityTrace,
)


def build_canonical_request(
    body: dict[str, Any],
    headers: dict[str, str],
) -> CanonicalRequest:
    """从原始请求体和头部构建规范化的 CanonicalRequest."""
    trace_id = str(uuid.uuid4())
    request_id = _extract_request_id(body, headers, trace_id)
    session_key = _derive_session_key(body, headers)
    thinking = _extract_thinking(body)
    messages = _extract_parts(body.get("messages", []))
    metadata = body.get("metadata") if isinstance(body.get("metadata"), dict) else {}
    tool_names = [
        str(tool.get("name", ""))
        for tool in body.get("tools", [])
        if isinstance(tool, dict) and tool.get("name")
    ]
    response_format = body.get("response_format")

    return CanonicalRequest(
        session_key=session_key,
        trace_id=trace_id,
        request_id=request_id,
        model=str(body.get("model", "")),
        messages=messages,
        thinking=thinking,
        metadata=metadata,
        tool_names=tool_names,
        supports_json_output=(
            isinstance(response_format, dict)
            and str(response_format.get("type", "")).startswith("json")
        ),
    )


def _extract_request_id(
    body: dict[str, Any], headers: dict[str, str], trace_id: str
) -> str:
    for key in ("request_id", "id"):
        value = body.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    for key in ("x-request-id", "request-id"):
        value = headers.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return trace_id


def _derive_session_key(body: dict[str, Any], headers: dict[str, str]) -> str:
    for key in ("x-claude-session-id", "x-session-id", "session-id"):
        value = headers.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()

    metadata = body.get("metadata")
    if isinstance(metadata, dict):
        for key in ("session_id", "conversation_id", "user_id"):
            value = metadata.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()

    digest_body = {
        "model": body.get("model"),
        "system": body.get("system"),
        "tools": body.get("tools"),
        "messages": body.get("messages", [])[-6:],
    }
    digest = hashlib.sha256(
        json.dumps(
            digest_body, ensure_ascii=False, sort_keys=True, default=str
        ).encode()
    ).hexdigest()
    return f"compat_{digest[:24]}"


def _extract_thinking(body: dict[str, Any]) -> CanonicalThinking:
    for source_field in ("thinking", "extended_thinking"):
        value = body.get(source_field)
        if not value:
            continue
        if isinstance(value, dict):
            return CanonicalThinking(
                enabled=True,
                budget_tokens=value.get("budget_tokens")
                if isinstance(value.get("budget_tokens"), int)
                else None,
                effort=str(value.get("effort")) if value.get("effort") else None,
                source_field=source_field,
            )
        return CanonicalThinking(enabled=True, source_field=source_field)
    return CanonicalThinking()


def _extract_parts(messages: list[dict[str, Any]]) -> list[CanonicalMessagePart]:
    parts: list[CanonicalMessagePart] = []
    for message in messages:
        if not isinstance(message, dict):
            continue
        role = str(message.get("role", "user"))
        content = message.get("content")
        if isinstance(content, str):
            parts.append(
                CanonicalMessagePart(
                    type=CanonicalPartType.TEXT, role=role, text=content
                )
            )
            continue
        if not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, dict):
                continue
            block_type = str(block.get("type", ""))
            if block_type == "text":
                parts.append(
                    CanonicalMessagePart(
                        type=CanonicalPartType.TEXT,
                        role=role,
                        text=str(block.get("text", "")),
                        raw_block=block,
                    )
                )
            elif block_type == "thinking":
                parts.append(
                    CanonicalMessagePart(
                        type=CanonicalPartType.THINKING,
                        role=role,
                        text=str(block.get("thinking", "")),
                        raw_block=block,
                    )
                )
            elif block_type == "image":
                parts.append(
                    CanonicalMessagePart(
                        type=CanonicalPartType.IMAGE,
                        role=role,
                        raw_block=block,
                    )
                )
            elif block_type in {"tool_use", "server_tool_use"}:
                parts.append(
                    CanonicalMessagePart(
                        type=CanonicalPartType.TOOL_USE,
                        role=role,
                        tool_call=CanonicalToolCall(
                            tool_id=str(block.get("id", "")),
                            name=str(block.get("name", "")),
                            arguments=block.get("input", {})
                            if isinstance(block.get("input"), dict)
                            else {},
                        ),
                        raw_block=block,
                    )
                )
            elif block_type == "tool_result":
                parts.append(
                    CanonicalMessagePart(
                        type=CanonicalPartType.TOOL_RESULT,
                        role=role,
                        text=_stringify_tool_result_content(block.get("content")),
                        tool_result_id=str(block.get("tool_use_id", "")),
                        raw_block=block,
                    )
                )
            else:
                parts.append(
                    CanonicalMessagePart(
                        type=CanonicalPartType.UNKNOWN,
                        role=role,
                        raw_block=block,
                    )
                )
    return parts


def _stringify_tool_result_content(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        chunks: list[str] = []
        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "text" and isinstance(block.get("text"), str):
                chunks.append(block["text"])
        return "\n".join(chunks)
    return ""
