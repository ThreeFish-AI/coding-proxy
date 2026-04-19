"""Anthropic 原生 API usage 抽取器.

字段映射规范（非流式 /v1/messages 响应体）：

- ``usage.input_tokens``                  → ``input_tokens``
- ``usage.output_tokens``                 → ``output_tokens``
- ``usage.cache_creation_input_tokens``   → ``cache_creation_tokens``
- ``usage.cache_read_input_tokens``       → ``cache_read_tokens``
- ``usage.cache_creation.ephemeral_5m_input_tokens`` → ``extra_usage.cache_5m_tokens``
- ``usage.cache_creation.ephemeral_1h_input_tokens`` → ``extra_usage.cache_1h_tokens``
- ``usage.server_tool_use.*``             → ``extra_usage.server_tool_use_*``

流式 SSE 走 ``StreamingUsageAccumulator`` + ``parse_usage_from_chunk``，不在此抽取。
"""

from __future__ import annotations

from typing import Any

from ..usage_registry import ExtractionResult, register_extractor


def _common_meta(body: dict[str, Any], result: ExtractionResult) -> None:
    if isinstance(body.get("id"), str):
        result.request_id = body["id"]
    if isinstance(body.get("model"), str):
        result.model_served = body["model"]


@register_extractor("anthropic", "messages")
def _messages(
    body: dict[str, Any], status: int, headers: dict[str, str]
) -> ExtractionResult:
    _ = status, headers
    result = ExtractionResult(evidence_kind="native_anthropic_usage")
    usage = body.get("usage")
    if not isinstance(usage, dict):
        _common_meta(body, result)
        return result

    result.raw_usage = dict(usage)
    result.input_tokens = int(usage.get("input_tokens") or 0)
    result.output_tokens = int(usage.get("output_tokens") or 0)
    result.cache_creation_tokens = int(usage.get("cache_creation_input_tokens") or 0)
    result.cache_read_tokens = int(usage.get("cache_read_input_tokens") or 0)
    result.source_field_map = {
        "input_tokens": "input_tokens" if "input_tokens" in usage else "",
        "output_tokens": "output_tokens" if "output_tokens" in usage else "",
        "cache_creation_tokens": "cache_creation_input_tokens"
        if "cache_creation_input_tokens" in usage
        else "",
        "cache_read_tokens": "cache_read_input_tokens"
        if "cache_read_input_tokens" in usage
        else "",
    }

    # 分层 cache 计费（5m / 1h ephemeral）
    cache_creation = usage.get("cache_creation")
    if isinstance(cache_creation, dict):
        for src_key, dst_key in (
            ("ephemeral_5m_input_tokens", "cache_5m_tokens"),
            ("ephemeral_1h_input_tokens", "cache_1h_tokens"),
        ):
            val = cache_creation.get(src_key)
            if isinstance(val, int) and val > 0:
                result.extra_usage[dst_key] = val

    # server_tool_use（web_search_requests 等）
    server_tool_use = usage.get("server_tool_use")
    if isinstance(server_tool_use, dict):
        for k, v in server_tool_use.items():
            if isinstance(v, int) and v > 0:
                result.extra_usage[f"server_tool_use_{k}"] = v

    _common_meta(body, result)
    return result


@register_extractor("anthropic", "count_tokens")
def _count_tokens(
    body: dict[str, Any], status: int, headers: dict[str, str]
) -> ExtractionResult:
    """count_tokens 端点仅返回 input_tokens（无计费，仅计数）."""
    _ = status, headers
    result = ExtractionResult(evidence_kind="native_anthropic_usage")
    if isinstance(body.get("input_tokens"), int):
        result.input_tokens = int(body["input_tokens"])
        result.raw_usage = {"input_tokens": result.input_tokens}
        result.source_field_map = {"input_tokens": "input_tokens"}
    _common_meta(body, result)
    return result


@register_extractor("anthropic", "messages.batch")
def _messages_batch(
    body: dict[str, Any], status: int, headers: dict[str, str]
) -> ExtractionResult:
    """Batches 创建/查询响应 — 首版不抽取 per-item usage，仅记元数据."""
    _ = status, headers
    result = ExtractionResult(evidence_kind="native_anthropic_usage")
    for key in ("id", "processing_status", "request_counts", "type"):
        val = body.get(key)
        if isinstance(val, (str, int, dict, list)):
            # 仅保留简单可序列化字段
            if isinstance(val, (str, int)):
                result.extra_usage[f"batch_{key}"] = val
    _common_meta(body, result)
    return result


__all__: list[str] = []
