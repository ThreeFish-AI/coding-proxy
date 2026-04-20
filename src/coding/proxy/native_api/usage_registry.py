"""Native API usage 抽取器注册表 + 通用兜底扫描 + 流式累加器.

Registry Pattern — ``(provider, operation) → ExtractorFn`` 的 O(1) 查表，
未命中时退回 ``_scan_usage_like`` 兜底扫描（仅扫顶层 ``usage`` / ``usageMetadata``
/ ``tokenUsage`` 三个 literal key 与其直接子字段中的 int 值）。

**设计要点**：

- 抽取器仅在**响应体**上工作，上游 4xx / 5xx 非 JSON 时返回空结果；
- ``ExtractionResult`` 中 ``input_tokens`` / ``output_tokens`` 等为规范化核心字段，
  ``extra_usage`` 承载非规范字段（reasoning_tokens、audio_tokens、thoughts_tokens 等）；
- ``StreamingUsageAccumulator`` 统一复用 ``routing.usage_parser.parse_usage_from_chunk``，
  Anthropic / OpenAI / Gemini 三家 SSE 共享同一入口，避免三套解析分叉。
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from ..routing.usage_parser import (
    build_usage_evidence_records,
    parse_usage_from_chunk,
)

logger = logging.getLogger(__name__)


@dataclass
class ExtractionResult:
    """单次响应用量抽取结果.

    ``input_tokens`` / ``output_tokens`` 等可为 ``None`` 表示「未提取到该字段」，
    以便上游区分「确实是 0」与「端点不提供」。``extra_usage`` 承载非规范字段。
    """

    input_tokens: int = 0
    output_tokens: int = 0
    cache_creation_tokens: int = 0
    cache_read_tokens: int = 0
    request_id: str = ""
    model_served: str = ""
    extra_usage: dict[str, Any] = field(default_factory=dict)
    # 原始上游 usage 字段块（写入 usage_evidence.raw_usage_json）
    raw_usage: dict[str, Any] = field(default_factory=dict)
    # 规范字段到上游字段名的映射（写入 usage_evidence.source_field_map_json）
    source_field_map: dict[str, str] = field(default_factory=dict)
    evidence_kind: str = ""

    def has_tokens(self) -> bool:
        return any(
            (
                self.input_tokens > 0,
                self.output_tokens > 0,
                self.cache_creation_tokens > 0,
                self.cache_read_tokens > 0,
            )
        )


ExtractorFn = Callable[[dict[str, Any], int, dict[str, str]], ExtractionResult]

# 注册表 — ``(provider, operation) → ExtractorFn``
_REGISTRY: dict[tuple[str, str], ExtractorFn] = {}


def register_extractor(provider: str, operation: str):
    """装饰器：注册 ``(provider, operation)`` 抽取器.

    Example::

        @register_extractor("openai", "chat")
        def _chat_extractor(body, status, headers):
            usage = body.get("usage", {})
            return ExtractionResult(
                input_tokens=int(usage.get("prompt_tokens", 0) or 0),
                output_tokens=int(usage.get("completion_tokens", 0) or 0),
                ...
            )
    """
    key = (provider.lower(), operation)

    def _deco(fn: ExtractorFn) -> ExtractorFn:
        _REGISTRY[key] = fn
        return fn

    return _deco


def extract_usage(
    provider: str,
    operation: str,
    body: dict[str, Any] | None,
    status: int,
    headers: dict[str, str] | None = None,
) -> ExtractionResult:
    """主入口：按 ``(provider, operation)`` 查表并抽取.

    - ``body`` 为 ``None`` 或非 dict → 返回空 ``ExtractionResult``；
    - 注册表未命中 → 走 ``_scan_usage_like`` 兜底扫描；
    - 任何异常均被吞下并记录 WARN，返回空结果（防御性设计保护主链路）。
    """
    if not isinstance(body, dict):
        return ExtractionResult()
    hdrs = headers or {}
    try:
        fn = _REGISTRY.get((provider.lower(), operation))
        if fn is not None:
            return fn(body, status, hdrs)
        return _scan_usage_like(body, provider=provider)
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning(
            "extract_usage failed provider=%s operation=%s: %s",
            provider,
            operation,
            exc,
        )
        return ExtractionResult()


# ── 兜底扫描 ─────────────────────────────────────────────────────

# 仅扫这三个 literal key —— 不递归深入业务字段，避免误判
_USAGE_KEYS = ("usage", "usageMetadata", "tokenUsage")

# 规范字段候选名（按 provider 常见别名顺序）
_INPUT_ALIASES = (
    "input_tokens",
    "prompt_tokens",
    "promptTokenCount",
    "inputTokenCount",
)
_OUTPUT_ALIASES = (
    "output_tokens",
    "completion_tokens",
    "candidatesTokenCount",
    "outputTokenCount",
)
_CACHE_CREATION_ALIASES = (
    "cache_creation_input_tokens",
    "cacheCreationInputTokens",
)
_CACHE_READ_ALIASES = (
    "cache_read_input_tokens",
    "cached_tokens",
    "cachedContentTokenCount",
    "cacheReadInputTokens",
)


def _scan_usage_like(body: dict[str, Any], *, provider: str = "") -> ExtractionResult:
    """通用兜底扫描 — 未注册端点的最后一道 usage 抽取防线.

    只扫顶层 ``usage`` / ``usageMetadata`` / ``tokenUsage`` 三个键，
    从中按别名匹配规范字段，其余所有 int 字段塞入 ``extra_usage``。
    """
    result = ExtractionResult(evidence_kind="native_generic_scan")
    for top_key in _USAGE_KEYS:
        block = body.get(top_key)
        if not isinstance(block, dict):
            continue
        result.raw_usage = dict(block)
        # canonical 字段提取
        result.input_tokens = _first_int(block, _INPUT_ALIASES)
        result.output_tokens = _first_int(block, _OUTPUT_ALIASES)
        result.cache_creation_tokens = _first_int(block, _CACHE_CREATION_ALIASES)
        result.cache_read_tokens = _first_int(block, _CACHE_READ_ALIASES)
        result.source_field_map = {
            "input_tokens": _first_key(block, _INPUT_ALIASES),
            "output_tokens": _first_key(block, _OUTPUT_ALIASES),
            "cache_creation_tokens": _first_key(block, _CACHE_CREATION_ALIASES),
            "cache_read_tokens": _first_key(block, _CACHE_READ_ALIASES),
        }
        # 非规范 int 字段 → extra_usage
        canonical = set(
            _INPUT_ALIASES
            + _OUTPUT_ALIASES
            + _CACHE_CREATION_ALIASES
            + _CACHE_READ_ALIASES
        )
        for k, v in block.items():
            if k in canonical:
                continue
            if isinstance(v, int) and not isinstance(v, bool):
                result.extra_usage[k] = v
        break  # 命中第一个顶层块即返回
    # 顶层常见 id / model 字段作为元数据（OpenAI 风格）
    if isinstance(body.get("id"), str):
        result.request_id = body["id"]
    elif isinstance(body.get("responseId"), str):
        result.request_id = body["responseId"]
    if isinstance(body.get("model"), str):
        result.model_served = body["model"]
    elif isinstance(body.get("modelVersion"), str):
        result.model_served = body["modelVersion"]
    _ = provider  # 当前未按 provider 分叉，保留参数便于未来差异化
    return result


def _first_int(d: dict[str, Any], keys: tuple[str, ...]) -> int:
    for k in keys:
        v = d.get(k)
        if isinstance(v, int) and not isinstance(v, bool) and v > 0:
            return v
        if isinstance(v, float) and v > 0:
            return int(v)
    return 0


def _first_key(d: dict[str, Any], keys: tuple[str, ...]) -> str:
    for k in keys:
        if k in d:
            return k
    return ""


# ── 流式累加器 ───────────────────────────────────────────────────


class StreamingUsageAccumulator:
    """SSE 分片流累加器 — 复用 ``routing.usage_parser.parse_usage_from_chunk``.

    Anthropic / OpenAI / Gemini 三家 SSE 的 usage 字段形态差异在 ``parse_usage_from_chunk``
    内部已收敛（见 ``routing/usage_parser.py``），此处只负责维护累加状态并在终结时
    生成 ExtractionResult + evidence 记录。
    """

    def __init__(self, vendor_label: str = "") -> None:
        self._usage: dict[str, Any] = {}
        self._vendor_label = vendor_label or None

    def feed(self, chunk: bytes) -> None:
        if not chunk:
            return
        try:
            parse_usage_from_chunk(chunk, self._usage, vendor_label=self._vendor_label)
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning("streaming usage parse failed: %s", exc)

    def snapshot(self) -> dict[str, Any]:
        """返回当前累加的原始 usage dict（内部状态的拷贝）."""
        return dict(self._usage)

    def finalize(
        self, *, vendor: str, model_served: str, request_id: str = ""
    ) -> tuple[ExtractionResult, list[dict[str, Any]]]:
        """终结并生成 ExtractionResult + evidence 记录列表."""
        u = self._usage
        extra = u.get("extra_usage", {})
        if not isinstance(extra, dict):
            extra = {}
        result = ExtractionResult(
            input_tokens=int(u.get("input_tokens", 0) or 0),
            output_tokens=int(u.get("output_tokens", 0) or 0),
            cache_creation_tokens=int(u.get("cache_creation_tokens", 0) or 0),
            cache_read_tokens=int(u.get("cache_read_tokens", 0) or 0),
            request_id=str(u.get("request_id") or request_id or ""),
            model_served=str(u.get("model_served") or model_served or ""),
            extra_usage=dict(extra),
            evidence_kind="stream_usage",
        )
        evidence = build_usage_evidence_records(
            u,
            vendor=vendor,
            model_served=result.model_served or model_served,
            request_id=result.request_id or request_id,
        )
        return result, evidence


__all__ = [
    "ExtractionResult",
    "ExtractorFn",
    "StreamingUsageAccumulator",
    "extract_usage",
    "register_extractor",
]
