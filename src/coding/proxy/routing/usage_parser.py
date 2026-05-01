"""SSE 流式响应用量解析工具.

从 Anthropic / OpenAI / Zhipu 兼容格式的 SSE chunk 中提取 token 用量信息，
支持多源归一化与 evidence 追踪。
"""

from __future__ import annotations

import json
import logging
from typing import Any

from ..vendors.base import UsageInfo

logger = logging.getLogger(__name__)


def _set_if_nonzero(usage: dict, key: str, value: int) -> None:
    """仅在 value 非零时设置，避免后续 chunk 的 0 值覆盖已提取的非零值.

    同时处理 None 值，确保数据类型正确性。
    """
    if value is not None and value != 0:
        usage[key] = value


def _append_usage_evidence(
    usage: dict[str, Any],
    *,
    evidence_kind: str,
    raw_usage: dict[str, Any],
    request_id: str | None = None,
    model_served: str | None = None,
) -> None:
    entries = usage.setdefault("_usage_evidence", [])
    if not isinstance(entries, list):
        return
    entries.append(
        {
            "evidence_kind": evidence_kind,
            "raw_usage": raw_usage,
            "request_id": request_id or "",
            "model_served": model_served or "",
            "source_field_map": {
                "input_tokens": next(
                    (
                        key
                        for key in (
                            "input_tokens",
                            "prompt_tokens",
                            "promptTokenCount",
                        )
                        if key in raw_usage
                    ),
                    "",
                ),
                "output_tokens": next(
                    (
                        key
                        for key in (
                            "output_tokens",
                            "completion_tokens",
                            "candidatesTokenCount",
                        )
                        if key in raw_usage
                    ),
                    "",
                ),
                "cache_creation_tokens": next(
                    (
                        key
                        for key in ("cache_creation_input_tokens",)
                        if key in raw_usage
                    ),
                    "",
                ),
                "cache_read_tokens": next(
                    (
                        key
                        for key in (
                            "cache_read_input_tokens",
                            "cached_tokens",
                            "cachedContentTokenCount",
                        )
                        if key in raw_usage
                    ),
                    "",
                ),
            },
            "cache_signal_present": any(
                key in raw_usage
                for key in (
                    "cache_creation_input_tokens",
                    "cache_read_input_tokens",
                    "cached_tokens",
                    "cachedContentTokenCount",
                )
            ),
        }
    )


def build_usage_evidence_records(
    usage: dict[str, Any],
    *,
    vendor: str,
    model_served: str,
    request_id: str,
) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    entries = usage.get("_usage_evidence", [])
    if not isinstance(entries, list):
        return records

    for entry in entries:
        if not isinstance(entry, dict):
            continue
        raw_usage = entry.get("raw_usage")
        if not isinstance(raw_usage, dict):
            continue
        source_field_map = entry.get("source_field_map")
        if not isinstance(source_field_map, dict):
            source_field_map = {}
        records.append(
            {
                "vendor": vendor,
                "request_id": str(entry.get("request_id") or request_id or ""),
                "model_served": str(entry.get("model_served") or model_served or ""),
                "evidence_kind": str(entry.get("evidence_kind") or "stream_usage"),
                "raw_usage_json": json.dumps(
                    raw_usage, ensure_ascii=False, sort_keys=True
                ),
                "parsed_input_tokens": usage.get("input_tokens", 0),
                "parsed_output_tokens": usage.get("output_tokens", 0),
                "parsed_cache_creation_tokens": usage.get("cache_creation_tokens", 0),
                "parsed_cache_read_tokens": usage.get("cache_read_tokens", 0),
                "cache_signal_present": bool(entry.get("cache_signal_present")),
                "source_field_map_json": json.dumps(
                    source_field_map, ensure_ascii=False, sort_keys=True
                ),
            }
        )
    return records


def parse_usage_from_chunk(
    chunk: bytes, usage: dict, *, vendor_label: str | None = None
) -> None:
    """从 SSE chunk 提取 token 用量.

    同时支持 Anthropic 原生格式和 OpenAI/Zhipu 兼容格式：
    - Anthropic: data.message.usage.input_tokens / data.usage.output_tokens
    - OpenAI/Zhipu: 顶层 data.usage.prompt_tokens / data.usage.completion_tokens

    :param vendor_label: 上游 Vendor 标签（如 "Anthropic"、"OpenAI"、"Gemini"），
                          用于日志标注实际来源协议，由调用方根据 tier.name 传入。
    """
    text = chunk.decode("utf-8", errors="ignore")
    for line in text.split("\n"):
        if not line.startswith("data: "):
            continue
        payload = line[6:].strip()
        if not payload or payload == "[DONE]":
            continue
        try:
            data = json.loads(payload)
        except json.JSONDecodeError:
            continue

        # Anthropic 格式: message_start 事件 (data.message.usage)
        msg = data.get("message", {})
        u = msg.get("usage") if isinstance(msg, dict) else None
        if isinstance(u, dict):
            input_tokens = u.get("input_tokens", 0) or u.get("prompt_tokens", 0)
            _set_if_nonzero(usage, "input_tokens", input_tokens)
            _set_if_nonzero(
                usage, "cache_creation_tokens", u.get("cache_creation_input_tokens", 0)
            )
            _set_if_nonzero(
                usage, "cache_read_tokens", u.get("cache_read_input_tokens", 0)
            )
            if "id" in msg:
                usage["request_id"] = msg["id"]
            if "model" in msg:
                usage["model_served"] = msg["model"]
            _append_usage_evidence(
                usage,
                evidence_kind="message_usage",
                raw_usage=dict(u),
                request_id=msg.get("id"),
                model_served=msg.get("model"),
            )

        # Anthropic message_delta / OpenAI 最后一个 chunk (data.usage)
        u = data.get("usage")
        if isinstance(u, dict):
            output_tokens = u.get("output_tokens", 0) or u.get("completion_tokens", 0)
            input_tokens = u.get("input_tokens", 0) or u.get("prompt_tokens", 0)
            cache_creation_tokens = u.get("cache_creation_input_tokens", 0)
            cache_read_tokens = u.get("cache_read_input_tokens", 0)

            _set_if_nonzero(usage, "output_tokens", output_tokens)
            _set_if_nonzero(usage, "input_tokens", input_tokens)
            _set_if_nonzero(usage, "cache_creation_tokens", cache_creation_tokens)
            _set_if_nonzero(usage, "cache_read_tokens", cache_read_tokens)
            _append_usage_evidence(
                usage,
                evidence_kind="data_usage",
                raw_usage=dict(u),
                request_id=data.get("id"),
                model_served=data.get("model"),
            )
            model_name = data.get("model")
            if model_name:
                usage["model_served"] = model_name

        # Gemini SSE 格式: data.usageMetadata.{promptTokenCount, candidatesTokenCount, cachedContentTokenCount, thoughtsTokenCount, toolUsePromptTokenCount}
        # Gemini 的流式响应在最后一帧（或每一帧）携带 usageMetadata；字段命名与
        # OpenAI / Anthropic 完全不同，但同一事件数据结构稳定可靠。
        if "usageMetadata" in data:
            um = data["usageMetadata"]
            if isinstance(um, dict):
                prompt_tc = int(um.get("promptTokenCount", 0) or 0)
                cand_tc = int(um.get("candidatesTokenCount", 0) or 0)
                cached_tc = int(um.get("cachedContentTokenCount", 0) or 0)
                thoughts_tc = int(um.get("thoughtsTokenCount", 0) or 0)
                tool_use_tc = int(um.get("toolUsePromptTokenCount", 0) or 0)

                _set_if_nonzero(usage, "input_tokens", prompt_tc)
                _set_if_nonzero(usage, "output_tokens", cand_tc)
                _set_if_nonzero(usage, "cache_read_tokens", cached_tc)

                # 非规范字段以 extra_usage 字典暂存,后续由 UsageRecorder 序列化到 extra_usage_json
                if thoughts_tc > 0 or tool_use_tc > 0:
                    extra = usage.setdefault("extra_usage", {})
                    if isinstance(extra, dict):
                        if thoughts_tc > 0:
                            extra["thoughts_tokens"] = thoughts_tc
                        if tool_use_tc > 0:
                            extra["tool_use_prompt_tokens"] = tool_use_tc

                _append_usage_evidence(
                    usage,
                    evidence_kind="gemini_usage_metadata",
                    raw_usage=dict(um),
                    request_id=data.get("responseId") or data.get("id"),
                    model_served=data.get("modelVersion") or data.get("model"),
                )
                model_name = data.get("modelVersion") or data.get("model")
                if model_name:
                    usage["model_served"] = model_name

        # request_id fallback (OpenAI 格式下 id 在顶层, Gemini 顶层为 responseId)
        if not usage.get("request_id"):
            if "id" in data:
                usage["request_id"] = data["id"]
            elif "responseId" in data:
                usage["request_id"] = data["responseId"]


def has_missing_input_usage_signals(info: UsageInfo) -> bool:
    """判断流式请求是否缺失可解释的输入 usage 信号."""
    if info.output_tokens <= 0:
        return False
    if info.input_tokens > 0:
        return False
    return info.cache_creation_tokens <= 0 and info.cache_read_tokens <= 0
