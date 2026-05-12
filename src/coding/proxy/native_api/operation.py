"""路径 → 规范化操作名分类器.

将 ``(provider, method, path)`` 映射为稳定的 ``operation`` 字符串，
作为 ``usage_log.operation`` 列与 ``NativeUsageExtractor`` 注册表的共同键值。

**单一字符串规范源**：任何新端点接入都应同时在本模块注册模式与在 extractor 中
使用相同的 ``operation`` 字符串。

.. note::
    规则匹配顺序：自上而下，首个匹配即返回；未命中返回 ``"unknown"``。
    规则是纯字符串正则，不依赖 HTTP method（因 LLM 端点多数为 POST，少量 GET 列表类
    已按路径独立区分）。
"""

from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class _Rule:
    pattern: re.Pattern[str]
    operation: str


# ── OpenAI ────────────────────────────────────────────────────────
_OPENAI_RULES: tuple[_Rule, ...] = (
    _Rule(re.compile(r"^/?v1/chat/completions/?$"), "chat"),
    _Rule(re.compile(r"^/?v1/completions/?$"), "completion"),
    _Rule(re.compile(r"^/?v1/responses(/.*)?$"), "responses"),
    _Rule(re.compile(r"^/?v1/embeddings/?$"), "embedding"),
    _Rule(re.compile(r"^/?v1/audio/transcriptions/?$"), "audio.transcription"),
    _Rule(re.compile(r"^/?v1/audio/translations/?$"), "audio.translation"),
    _Rule(re.compile(r"^/?v1/audio/speech/?$"), "audio.speech"),
    _Rule(re.compile(r"^/?v1/images/generations/?$"), "image.generation"),
    _Rule(re.compile(r"^/?v1/images/edits/?$"), "image.edit"),
    _Rule(re.compile(r"^/?v1/images/variations/?$"), "image.variation"),
    _Rule(re.compile(r"^/?v1/moderations/?$"), "moderation"),
    _Rule(re.compile(r"^/?v1/models(/.*)?$"), "model.list"),
    _Rule(re.compile(r"^/?v1/files(/.*)?$"), "file"),
    _Rule(re.compile(r"^/?v1/batches(/.*)?$"), "batch"),
    _Rule(re.compile(r"^/?v1/fine_tuning/.*$"), "finetune"),
    _Rule(re.compile(r"^/?v1/assistants(/.*)?$"), "assistant"),
    _Rule(re.compile(r"^/?v1/threads(/.*)?$"), "thread"),
    _Rule(re.compile(r"^/?v1/vector_stores(/.*)?$"), "vector_store"),
    _Rule(re.compile(r"^/?v1/uploads(/.*)?$"), "upload"),
)

# ── Gemini ────────────────────────────────────────────────────────
# Gemini 的方法动词作为路径后缀（``:generateContent``），通过正则提取
_GEMINI_RULES: tuple[_Rule, ...] = (
    _Rule(
        re.compile(r"^/?v1(?:beta)?/models/[^/]+(?:%3A|:)streamGenerateContent/?$"),
        "generate_content",
    ),
    _Rule(
        re.compile(r"^/?v1(?:beta)?/models/[^/]+(?:%3A|:)generateContent/?$"),
        "generate_content",
    ),
    _Rule(
        re.compile(r"^/?v1(?:beta)?/models/[^/]+(?:%3A|:)countTokens/?$"),
        "count_tokens",
    ),
    _Rule(
        re.compile(r"^/?v1(?:beta)?/models/[^/]+(?:%3A|:)embedContent/?$"),
        "embedding",
    ),
    _Rule(
        re.compile(r"^/?v1(?:beta)?/models/[^/]+(?:%3A|:)batchEmbedContents/?$"),
        "embedding.batch",
    ),
    _Rule(
        re.compile(r"^/?v1(?:beta)?/models/[^/]+(?:%3A|:)predict/?$"),
        "predict",
    ),
    _Rule(
        re.compile(r"^/?v1(?:beta)?/cachedContents(/.*)?$"),
        "cache",
    ),
    _Rule(
        re.compile(r"^/?v1(?:beta)?/files(/.*)?$"),
        "file",
    ),
    _Rule(
        re.compile(r"^/?v1(?:beta)?/models/?$"),
        "model.list",
    ),
    _Rule(
        re.compile(r"^/?v1(?:beta)?/models/[^/:]+/?$"),
        "model.retrieve",
    ),
    _Rule(
        re.compile(r"^/?v1(?:beta)?/tunedModels(/.*)?$"),
        "tuned_model",
    ),
)

# ── Anthropic ─────────────────────────────────────────────────────
_ANTHROPIC_RULES: tuple[_Rule, ...] = (
    _Rule(
        re.compile(r"^/?v1/messages/count_tokens/?$"),
        "count_tokens",
    ),
    _Rule(
        re.compile(r"^/?v1/messages/batches(/.*)?$"),
        "messages.batch",
    ),
    _Rule(
        re.compile(r"^/?v1/messages/?$"),
        "messages",
    ),
    _Rule(
        re.compile(r"^/?v1/models(/.*)?$"),
        "model.list",
    ),
    _Rule(
        re.compile(r"^/?v1/files(/.*)?$"),
        "file",
    ),
    _Rule(
        re.compile(r"^/?v1/organizations(/.*)?$"),
        "organization",
    ),
)

_RULES_BY_PROVIDER: dict[str, tuple[_Rule, ...]] = {
    "openai": _OPENAI_RULES,
    "gemini": _GEMINI_RULES,
    "anthropic": _ANTHROPIC_RULES,
}


class OperationClassifier:
    """``(provider, method, path) → operation`` 分类器.

    - ``path`` 通常为 ``rest:path`` 捕获段，形如 ``v1/chat/completions`` 或 ``/v1/...``；
      两种前缀形式均接受。
    - 查询参数（``?key=xxx``）应先被剥除后再传入。
    """

    @staticmethod
    def classify(provider: str, method: str, path: str) -> str:
        rules = _RULES_BY_PROVIDER.get(provider.lower())
        if not rules:
            return "unknown"
        normalized = path if path.startswith("/") else f"/{path}"
        for rule in rules:
            if rule.pattern.match(normalized):
                return rule.operation
        return "unknown"

    @staticmethod
    def is_stream_path(provider: str, path: str) -> bool:
        """Gemini 的 ``:streamGenerateContent`` 路径强制 SSE；其他 provider 以
        响应 ``content-type`` 为准（此处仅为 Gemini 快速判定钩子）."""
        if provider.lower() != "gemini":
            return False
        normalized = path if path.startswith("/") else f"/{path}"
        return bool(
            re.match(
                r"^/?v1(?:beta)?/models/[^/]+(?:%3A|:)streamGenerateContent/?$",
                normalized,
            )
        )


__all__ = ["OperationClassifier"]
