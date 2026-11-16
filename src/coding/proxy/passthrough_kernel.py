"""Passthrough 共享内核 — 透传通道的公共机制件.

被 :mod:`.native_api`（OpenAI / Gemini / Anthropic 原生 API 透传）与
:mod:`.antigravity_gateway`（Antigravity IDE 客户端透传）共同复用，
保证两条通道的 hop-by-hop 头清洗、URL 组装、SSE tee 中继与 evidence
构建行为单一事实源（SSOT）。

本模块只承载**机制**（与具体协议无关的字节级转发规则）；各通道的
**策略**（provider 配置、路径分类、usage 抽取）留在各自包内。
"""

from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from typing import Any

import httpx

logger = logging.getLogger(__name__)

# RFC 7230 §6.1 hop-by-hop + 常见代理相关头 + accept-encoding（避免 httpx 解压）
HOP_BY_HOP_HEADERS: frozenset[str] = frozenset(
    {
        "connection",
        "content-length",
        "transfer-encoding",
        "keep-alive",
        "proxy-authenticate",
        "proxy-authorization",
        "proxy-connection",
        "te",
        "trailer",
        "upgrade",
        "accept-encoding",
        "host",
    }
)

# 响应侧 hop-by-hop（不含 accept-encoding，但保留 content-length 剥除以免长度错位）
RESPONSE_HOP_BY_HOP_HEADERS: frozenset[str] = frozenset(
    {
        "connection",
        "content-length",
        "transfer-encoding",
        "keep-alive",
        "proxy-authenticate",
        "proxy-authorization",
        "proxy-connection",
        "te",
        "trailer",
        "upgrade",
        "content-encoding",  # 上游若返回 gzip/br, httpx 已解压, 原始编码头需剥除
    }
)

# 上游网络异常集合 — 透传通道统一转 502
UPSTREAM_NETWORK_ERRORS: tuple[type[Exception], ...] = (
    httpx.TimeoutException,
    httpx.ConnectError,
    httpx.ReadError,
    httpx.RemoteProtocolError,
)


@dataclass
class ExtractionResult:
    """单次响应用量抽取结果.

    ``input_tokens`` / ``output_tokens`` 等为规范化核心字段，
    ``extra_usage`` 承载非规范字段（reasoning_tokens、thoughts_tokens 等）。
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


def filter_request_headers(headers: dict[str, str]) -> dict[str, str]:
    """剥除 hop-by-hop / 代理相关头，保留认证与业务头."""
    out: dict[str, str] = {}
    for k, v in headers.items():
        if k.lower() in HOP_BY_HOP_HEADERS:
            continue
        out[k] = v
    return out


def filter_response_headers(headers: dict[str, str]) -> dict[str, str]:
    out: dict[str, str] = {}
    for k, v in headers.items():
        if k.lower() in RESPONSE_HOP_BY_HOP_HEADERS:
            continue
        out[k] = v
    return out


def build_upstream_url(endpoint: str, query_string: str) -> str:
    """endpoint（前导 ``/`` 形式）+ 原样 query string → 上游相对 URL."""
    if query_string:
        return f"{endpoint}?{query_string}"
    return endpoint


def build_502_response_body(reason: str) -> bytes:
    """上游不可达时的统一 JSON 错误体（各通道仅 message 前缀不同）."""
    return json.dumps(
        {"error": {"message": f"upstream unreachable: {reason}", "type": "api_error"}}
    ).encode()


def build_not_enabled_response_body(channel: str) -> bytes:
    """通道未启用时的统一 JSON 错误体."""
    return json.dumps(
        {
            "error": {
                "message": f"{channel} is not enabled",
                "type": "not_found",
            }
        }
    ).encode()


async def iter_sse_tee(
    upstream_resp: httpx.Response,
    feed: Callable[[bytes], None],
    *,
    log_name: str,
) -> AsyncIterator[bytes]:
    """SSE 字节级中继 — 逐 chunk 原样转发并 tee 给 ``feed`` 做旁路解析.

    因请求侧已强制 ``Accept-Encoding: identity``，上游不会压缩响应，
    ``aiter_bytes()`` 与 ``aiter_raw()`` 等价且兼容 MockTransport。
    网络中断降级为 WARN（观测优先，不向客户端抛异常）；finally 确保
    上游连接关闭。
    """
    try:
        async for chunk in upstream_resp.aiter_bytes():
            if chunk:
                feed(chunk)
                yield chunk
    except UPSTREAM_NETWORK_ERRORS as exc:
        logger.warning("%s stream interrupted: %s", log_name, exc)
    finally:
        try:
            await upstream_resp.aclose()
        except Exception:  # pragma: no cover - defensive
            pass


def build_nonstream_evidence_records(
    *, vendor: str, extraction: ExtractionResult
) -> list[dict[str, Any]]:
    """非流式响应的 evidence 记录（仅在抽取到 raw_usage 时生成）."""
    if not extraction.raw_usage:
        return []
    return [
        {
            "vendor": vendor,
            "request_id": extraction.request_id or "",
            "model_served": extraction.model_served or "",
            "evidence_kind": extraction.evidence_kind or "native_generic_scan",
            "raw_usage_json": json.dumps(
                extraction.raw_usage, ensure_ascii=False, sort_keys=True, default=str
            ),
            "parsed_input_tokens": extraction.input_tokens,
            "parsed_output_tokens": extraction.output_tokens,
            "parsed_cache_creation_tokens": extraction.cache_creation_tokens,
            "parsed_cache_read_tokens": extraction.cache_read_tokens,
            "cache_signal_present": extraction.cache_creation_tokens > 0
            or extraction.cache_read_tokens > 0,
            "source_field_map_json": json.dumps(
                extraction.source_field_map,
                ensure_ascii=False,
                sort_keys=True,
                default=str,
            ),
        }
    ]


__all__ = [
    "ExtractionResult",
    "HOP_BY_HOP_HEADERS",
    "RESPONSE_HOP_BY_HOP_HEADERS",
    "UPSTREAM_NETWORK_ERRORS",
    "build_502_response_body",
    "build_not_enabled_response_body",
    "build_nonstream_evidence_records",
    "build_upstream_url",
    "filter_request_headers",
    "filter_response_headers",
    "iter_sse_tee",
]
