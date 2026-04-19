"""``OperationClassifier`` 路径 → 操作名分类的全覆盖单测.

覆盖三家 provider 的关键端点路径，同时校验：

- 前缀 ``/`` / 无前缀双形态等价；
- 未知路径 / 未知 provider → ``unknown``；
- ``is_stream_path`` 仅对 Gemini ``:streamGenerateContent`` 返回 ``True``。
"""

from __future__ import annotations

import pytest

from coding.proxy.native_api.operation import OperationClassifier


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("/v1/chat/completions", "chat"),
        ("v1/chat/completions", "chat"),
        ("/v1/completions", "completion"),
        ("/v1/responses", "responses"),
        ("/v1/responses/resp_abc", "responses"),
        ("/v1/embeddings", "embedding"),
        ("/v1/audio/transcriptions", "audio.transcription"),
        ("/v1/audio/translations", "audio.translation"),
        ("/v1/audio/speech", "audio.speech"),
        ("/v1/images/generations", "image.generation"),
        ("/v1/images/edits", "image.edit"),
        ("/v1/images/variations", "image.variation"),
        ("/v1/moderations", "moderation"),
        ("/v1/models", "model.list"),
        ("/v1/models/gpt-4o-mini", "model.list"),
        ("/v1/files", "file"),
        ("/v1/batches", "batch"),
        ("/v1/fine_tuning/jobs", "finetune"),
        ("/v1/assistants", "assistant"),
        ("/v1/threads", "thread"),
        ("/v1/vector_stores", "vector_store"),
        ("/v1/uploads", "upload"),
    ],
)
def test_classify_openai(path: str, expected: str) -> None:
    assert OperationClassifier.classify("openai", "POST", path) == expected


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("/v1beta/models/gemini-2.0-flash:generateContent", "generate_content"),
        ("v1beta/models/gemini-2.0-flash:generateContent", "generate_content"),
        ("/v1beta/models/gemini-2.0-flash:streamGenerateContent", "generate_content"),
        ("/v1beta/models/gemini-1.5-pro:countTokens", "count_tokens"),
        ("/v1beta/models/text-embedding-004:embedContent", "embedding"),
        ("/v1beta/models/text-embedding-004:batchEmbedContents", "embedding.batch"),
        ("/v1beta/models/imagegeneration@006:predict", "predict"),
        ("/v1beta/cachedContents", "cache"),
        ("/v1beta/cachedContents/cachedContents-xyz", "cache"),
        ("/v1beta/files", "file"),
        ("/v1beta/models", "model.list"),
        ("/v1beta/models/gemini-2.0-flash", "model.retrieve"),
        ("/v1beta/tunedModels", "tuned_model"),
        ("/v1/models/gemini-2.0-flash:generateContent", "generate_content"),
    ],
)
def test_classify_gemini(path: str, expected: str) -> None:
    assert OperationClassifier.classify("gemini", "POST", path) == expected


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("/v1/messages", "messages"),
        ("v1/messages", "messages"),
        ("/v1/messages/count_tokens", "count_tokens"),
        ("/v1/messages/batches", "messages.batch"),
        ("/v1/messages/batches/batch_abc", "messages.batch"),
        ("/v1/messages/batches/batch_abc/results", "messages.batch"),
        ("/v1/models", "model.list"),
        ("/v1/files", "file"),
        ("/v1/organizations", "organization"),
    ],
)
def test_classify_anthropic(path: str, expected: str) -> None:
    assert OperationClassifier.classify("anthropic", "POST", path) == expected


@pytest.mark.parametrize(
    "path",
    [
        "/v99/unknown",
        "/admin/panel",
        "",
    ],
)
def test_classify_unknown_path(path: str) -> None:
    assert OperationClassifier.classify("openai", "POST", path) == "unknown"


def test_classify_unknown_provider() -> None:
    assert OperationClassifier.classify("cohere", "POST", "/v1/chat") == "unknown"


def test_classify_provider_case_insensitive() -> None:
    assert (
        OperationClassifier.classify("OpenAI", "POST", "/v1/chat/completions") == "chat"
    )
    assert (
        OperationClassifier.classify(
            "GEMINI", "POST", "/v1beta/models/m:generateContent"
        )
        == "generate_content"
    )


def test_is_stream_path() -> None:
    assert OperationClassifier.is_stream_path(
        "gemini", "/v1beta/models/gemini-2.0-flash:streamGenerateContent"
    )
    assert OperationClassifier.is_stream_path(
        "gemini", "v1beta/models/gemini-2.0-flash:streamGenerateContent"
    )
    # non-stream Gemini 路径
    assert not OperationClassifier.is_stream_path(
        "gemini", "/v1beta/models/gemini-2.0-flash:generateContent"
    )
    # OpenAI / Anthropic 不走路径判定（以响应 content-type 为准）
    assert not OperationClassifier.is_stream_path("openai", "/v1/chat/completions")
    assert not OperationClassifier.is_stream_path("anthropic", "/v1/messages")
