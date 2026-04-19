"""Gemini 原生 API usage 抽取器.

字段映射规范：

- ``usageMetadata.promptTokenCount``        → ``input_tokens``
- ``usageMetadata.candidatesTokenCount``    → ``output_tokens``
- ``usageMetadata.cachedContentTokenCount`` → ``cache_read_tokens``
- ``usageMetadata.thoughtsTokenCount``      → ``extra_usage.thoughts_tokens``
- ``usageMetadata.toolUsePromptTokenCount`` → ``extra_usage.tool_use_prompt_tokens``

``responseId`` / ``modelVersion`` 作为 request_id / model_served 的元数据来源。
"""

from __future__ import annotations

from typing import Any

from ..usage_registry import ExtractionResult, register_extractor


def _common_meta(body: dict[str, Any], result: ExtractionResult) -> None:
    if isinstance(body.get("responseId"), str):
        result.request_id = body["responseId"]
    elif isinstance(body.get("id"), str):
        result.request_id = body["id"]
    if isinstance(body.get("modelVersion"), str):
        result.model_served = body["modelVersion"]
    elif isinstance(body.get("model"), str):
        result.model_served = body["model"]


def _from_usage_metadata(body: dict[str, Any]) -> ExtractionResult:
    result = ExtractionResult(evidence_kind="native_gemini_usage_metadata")
    um = body.get("usageMetadata")
    if not isinstance(um, dict):
        _common_meta(body, result)
        return result

    result.raw_usage = dict(um)
    result.input_tokens = int(um.get("promptTokenCount") or 0)
    result.output_tokens = int(um.get("candidatesTokenCount") or 0)
    result.cache_read_tokens = int(um.get("cachedContentTokenCount") or 0)
    result.source_field_map = {
        "input_tokens": "promptTokenCount" if "promptTokenCount" in um else "",
        "output_tokens": "candidatesTokenCount" if "candidatesTokenCount" in um else "",
        "cache_read_tokens": "cachedContentTokenCount"
        if "cachedContentTokenCount" in um
        else "",
    }

    thoughts = um.get("thoughtsTokenCount")
    if isinstance(thoughts, int) and thoughts > 0:
        result.extra_usage["thoughts_tokens"] = thoughts
    tool_use = um.get("toolUsePromptTokenCount")
    if isinstance(tool_use, int) and tool_use > 0:
        result.extra_usage["tool_use_prompt_tokens"] = tool_use

    total = um.get("totalTokenCount")
    if isinstance(total, int) and total > 0:
        result.extra_usage.setdefault("total_token_count", total)

    _common_meta(body, result)
    return result


@register_extractor("gemini", "generate_content")
@register_extractor("gemini", "embedding")
@register_extractor("gemini", "embedding.batch")
@register_extractor("gemini", "predict")
def _generate(
    body: dict[str, Any], status: int, headers: dict[str, str]
) -> ExtractionResult:
    _ = status, headers
    return _from_usage_metadata(body)


@register_extractor("gemini", "count_tokens")
def _count_tokens(
    body: dict[str, Any], status: int, headers: dict[str, str]
) -> ExtractionResult:
    """countTokens 端点仅返回 totalTokens（无计费，作审计记录）."""
    _ = status, headers
    result = ExtractionResult(evidence_kind="native_gemini_usage_metadata")
    total = body.get("totalTokens")
    if isinstance(total, int) and total > 0:
        result.input_tokens = total
        result.raw_usage = {"totalTokens": total}
        result.source_field_map = {"input_tokens": "totalTokens"}
    # Gemini countTokens 也可能带 usageMetadata（新版 API）
    if "usageMetadata" in body:
        # 覆盖掉仅 totalTokens 的骨架，保留更完整的 usageMetadata 形态
        return _from_usage_metadata(body)
    _common_meta(body, result)
    return result


@register_extractor("gemini", "cache")
def _cache(
    body: dict[str, Any], status: int, headers: dict[str, str]
) -> ExtractionResult:
    """cachedContents CRUD — 无 token 计费，记 expireTime/model 作 extra."""
    _ = status, headers
    result = ExtractionResult(evidence_kind="native_gemini_usage_metadata")
    for key in ("expireTime", "ttl", "displayName"):
        val = body.get(key)
        if isinstance(val, (str, int, float)):
            result.extra_usage[key] = val
    _common_meta(body, result)
    return result


__all__: list[str] = []
