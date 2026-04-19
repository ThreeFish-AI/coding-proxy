"""OpenAI 原生 API usage 抽取器.

覆盖 chat / completion / responses / embeddings / audio / image / moderation 等端点。
字段映射规范：

- ``usage.prompt_tokens`` / ``input_tokens``           → ``input_tokens``
- ``usage.completion_tokens`` / ``output_tokens``      → ``output_tokens``
- ``usage.prompt_tokens_details.cached_tokens``        → ``cache_read_tokens``
- ``usage.completion_tokens_details.reasoning_tokens`` → ``extra_usage.reasoning_tokens``
- ``usage.completion_tokens_details.audio_tokens``     → ``extra_usage.audio_output_tokens``
- ``usage.prompt_tokens_details.audio_tokens``         → ``extra_usage.audio_input_tokens``
- accepted / rejected prediction tokens                → ``extra_usage.*``
"""

from __future__ import annotations

from typing import Any

from ..usage_registry import ExtractionResult, register_extractor


def _common_meta(body: dict[str, Any], result: ExtractionResult) -> None:
    if isinstance(body.get("id"), str):
        result.request_id = body["id"]
    if isinstance(body.get("model"), str):
        result.model_served = body["model"]


def _extract_chat_like(body: dict[str, Any]) -> ExtractionResult:
    """Chat Completions / Completions 共用抽取逻辑."""
    result = ExtractionResult(evidence_kind="native_openai_usage")
    usage = body.get("usage")
    if not isinstance(usage, dict):
        _common_meta(body, result)
        return result

    result.raw_usage = dict(usage)
    result.input_tokens = int(usage.get("prompt_tokens") or 0)
    result.output_tokens = int(usage.get("completion_tokens") or 0)
    result.source_field_map = {
        "input_tokens": "prompt_tokens" if "prompt_tokens" in usage else "",
        "output_tokens": "completion_tokens" if "completion_tokens" in usage else "",
    }

    # prompt_tokens_details — cached / audio input
    pdetails = usage.get("prompt_tokens_details")
    if isinstance(pdetails, dict):
        cached = int(pdetails.get("cached_tokens") or 0)
        if cached > 0:
            result.cache_read_tokens = cached
            result.source_field_map["cache_read_tokens"] = (
                "prompt_tokens_details.cached_tokens"
            )
        audio_in = int(pdetails.get("audio_tokens") or 0)
        if audio_in > 0:
            result.extra_usage["audio_input_tokens"] = audio_in

    # completion_tokens_details — reasoning / audio / prediction
    cdetails = usage.get("completion_tokens_details")
    if isinstance(cdetails, dict):
        for src_key, dst_key in (
            ("reasoning_tokens", "reasoning_tokens"),
            ("audio_tokens", "audio_output_tokens"),
            ("accepted_prediction_tokens", "accepted_prediction_tokens"),
            ("rejected_prediction_tokens", "rejected_prediction_tokens"),
        ):
            val = cdetails.get(src_key)
            if isinstance(val, int) and val > 0:
                result.extra_usage[dst_key] = val

    # total_tokens 作为审计字段
    total = usage.get("total_tokens")
    if isinstance(total, int) and total > 0:
        result.extra_usage.setdefault("total_tokens", total)

    _common_meta(body, result)
    return result


@register_extractor("openai", "chat")
def _chat(
    body: dict[str, Any], status: int, headers: dict[str, str]
) -> ExtractionResult:
    _ = status, headers
    return _extract_chat_like(body)


@register_extractor("openai", "completion")
def _completion(
    body: dict[str, Any], status: int, headers: dict[str, str]
) -> ExtractionResult:
    _ = status, headers
    return _extract_chat_like(body)


@register_extractor("openai", "responses")
def _responses(
    body: dict[str, Any], status: int, headers: dict[str, str]
) -> ExtractionResult:
    """Responses API — usage 字段命名与 chat 略有差异."""
    _ = status, headers
    result = ExtractionResult(evidence_kind="native_openai_responses_usage")
    usage = body.get("usage")
    if not isinstance(usage, dict):
        _common_meta(body, result)
        return result

    result.raw_usage = dict(usage)
    result.input_tokens = int(usage.get("input_tokens") or 0)
    result.output_tokens = int(usage.get("output_tokens") or 0)
    result.source_field_map = {
        "input_tokens": "input_tokens" if "input_tokens" in usage else "",
        "output_tokens": "output_tokens" if "output_tokens" in usage else "",
    }

    idetails = usage.get("input_tokens_details")
    if isinstance(idetails, dict):
        cached = int(idetails.get("cached_tokens") or 0)
        if cached > 0:
            result.cache_read_tokens = cached
            result.source_field_map["cache_read_tokens"] = (
                "input_tokens_details.cached_tokens"
            )

    odetails = usage.get("output_tokens_details")
    if isinstance(odetails, dict):
        reasoning = int(odetails.get("reasoning_tokens") or 0)
        if reasoning > 0:
            result.extra_usage["reasoning_tokens"] = reasoning

    total = usage.get("total_tokens")
    if isinstance(total, int) and total > 0:
        result.extra_usage.setdefault("total_tokens", total)

    _common_meta(body, result)
    return result


@register_extractor("openai", "embedding")
def _embedding(
    body: dict[str, Any], status: int, headers: dict[str, str]
) -> ExtractionResult:
    _ = status, headers
    result = ExtractionResult(evidence_kind="native_openai_usage")
    usage = body.get("usage")
    if not isinstance(usage, dict):
        _common_meta(body, result)
        return result
    result.raw_usage = dict(usage)
    result.input_tokens = int(usage.get("prompt_tokens") or 0)
    result.source_field_map = {
        "input_tokens": "prompt_tokens" if "prompt_tokens" in usage else "",
    }
    total = usage.get("total_tokens")
    if isinstance(total, int) and total > 0:
        result.extra_usage["total_tokens"] = total
    _common_meta(body, result)
    return result


@register_extractor("openai", "audio.transcription")
@register_extractor("openai", "audio.translation")
def _audio_stt(
    body: dict[str, Any], status: int, headers: dict[str, str]
) -> ExtractionResult:
    """音频 STT — gpt-4o-transcribe 等新模型带 usage 字段，旧 whisper 没有."""
    _ = status, headers
    result = ExtractionResult(evidence_kind="native_openai_usage")
    usage = body.get("usage")
    if isinstance(usage, dict):
        result.raw_usage = dict(usage)
        result.input_tokens = int(usage.get("input_tokens") or 0)
        result.output_tokens = int(usage.get("output_tokens") or 0)
        result.source_field_map = {
            "input_tokens": "input_tokens" if "input_tokens" in usage else "",
            "output_tokens": "output_tokens" if "output_tokens" in usage else "",
        }
    # verbose_json 的 duration 字段 → extra_usage.audio_duration_seconds
    duration = body.get("duration")
    if isinstance(duration, (int, float)) and duration > 0:
        result.extra_usage["audio_duration_seconds"] = float(duration)
    _common_meta(body, result)
    return result


@register_extractor("openai", "audio.speech")
def _audio_tts(
    body: dict[str, Any], status: int, headers: dict[str, str]
) -> ExtractionResult:
    """TTS 无 JSON body；仅构造空骨架（由 handler 层填 endpoint/op）."""
    _ = body, status, headers
    return ExtractionResult(evidence_kind="native_openai_usage")


@register_extractor("openai", "image.generation")
@register_extractor("openai", "image.edit")
@register_extractor("openai", "image.variation")
def _image(
    body: dict[str, Any], status: int, headers: dict[str, str]
) -> ExtractionResult:
    """图像生成/编辑 — gpt-image-1 带 usage 字段（2024 起），DALL-E 无."""
    _ = status, headers
    result = ExtractionResult(evidence_kind="native_openai_usage")
    usage = body.get("usage")
    if isinstance(usage, dict):
        result.raw_usage = dict(usage)
        result.input_tokens = int(usage.get("input_tokens") or 0)
        result.output_tokens = int(usage.get("output_tokens") or 0)
    # 无 usage 时至少记原始 data 长度作为生成图片数
    data = body.get("data")
    if isinstance(data, list):
        result.extra_usage["generated_images"] = len(data)
    _common_meta(body, result)
    return result


@register_extractor("openai", "moderation")
def _moderation(
    body: dict[str, Any], status: int, headers: dict[str, str]
) -> ExtractionResult:
    """Moderation 无 token 计费；仅记 results 数量."""
    _ = status, headers
    result = ExtractionResult(evidence_kind="native_openai_usage")
    results = body.get("results")
    if isinstance(results, list):
        result.extra_usage["moderation_results_count"] = len(results)
    _common_meta(body, result)
    return result


__all__: list[str] = []
