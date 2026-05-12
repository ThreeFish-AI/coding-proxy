"""Native API 透传核心 — ``NativeProxyHandler``.

设计原则：

- **字节级透传** — 请求 / 响应体、query string、自定义 header 一律原样转发；
- **协议透明** — 上游任意 HTTP 状态码（含 4xx/5xx）原样返回，不强制改写为 Anthropic 错误体；
- **流式统一** — 以响应 ``content-type`` 判定流式；``text/event-stream`` 走 tee-accumulator 抽 usage；
- **Hop-by-Hop 头清洗** — 按 RFC 7230 Section 6.1 剥除 ``connection / content-length /
  transfer-encoding / te / trailer / upgrade / keep-alive / proxy-*`` 及 ``accept-encoding``
  （后者避免 httpx 解压后再转发造成客户端解压失败）；
- **观测优先** — 抽取失败不抛异常，全部降级为 WARN + tokens=0，保证主链路不受影响。
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from collections.abc import AsyncIterator
from typing import TYPE_CHECKING
from urllib.parse import unquote

import httpx

from .operation import OperationClassifier
from .usage_registry import (
    ExtractionResult,
    StreamingUsageAccumulator,
    extract_usage,
)

if TYPE_CHECKING:
    from fastapi import Request
    from starlette.responses import Response as StarletteResponse

    from ..logging.db import TokenLogger
    from ..pricing import PricingTable
    from ..routing.usage_recorder import UsageRecorder
    from .config import NativeApiConfig

logger = logging.getLogger(__name__)

# RFC 7230 §6.1 hop-by-hop + 常见代理相关头 + accept-encoding（避免 httpx 解压）
_HOP_BY_HOP_HEADERS: frozenset[str] = frozenset(
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
_RESPONSE_HOP_BY_HOP_HEADERS: frozenset[str] = frozenset(
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

# vendor 命名 — 与既有 'anthropic' (cc 流量) 区分
_VENDOR_LABEL: dict[str, str] = {
    "openai": "openai",
    "gemini": "gemini",
    "anthropic": "anthropic-native",
}


class NativeProxyHandler:
    """原生 API 透传处理器.

    生命周期与 FastAPI app 同寿命（构造于 ``create_app``，关闭于 lifespan 结束）。
    持有独立 ``httpx.AsyncClient`` 池，**不**共享既有 vendor client。
    """

    def __init__(
        self,
        config: NativeApiConfig,
        token_logger: TokenLogger | None = None,
        pricing_table: PricingTable | None = None,
        usage_recorder: UsageRecorder | None = None,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        from ..routing.usage_recorder import UsageRecorder as _UR  # local import

        self._config = config
        self._token_logger = token_logger
        self._pricing_table = pricing_table
        self._usage_recorder = usage_recorder or _UR(
            token_logger=token_logger, pricing_table=pricing_table
        )
        self._transport = transport
        # 按 provider 缓存 httpx.AsyncClient（每个 provider 独立 base_url / timeout）
        self._clients: dict[str, httpx.AsyncClient] = {}

    # ── 生命周期 ───────────────────────────────────────────────

    def _get_client(self, provider: str) -> httpx.AsyncClient:
        cached = self._clients.get(provider)
        if cached is not None:
            return cached
        cfg = self._config.get(provider)
        if cfg is None:
            raise ValueError(f"unknown native api provider: {provider!r}")
        timeout = httpx.Timeout(
            cfg.timeout_ms / 1000.0, connect=cfg.connect_timeout_ms / 1000.0
        )
        client = httpx.AsyncClient(
            base_url=cfg.base_url.rstrip("/"),
            timeout=timeout,
            transport=self._transport,
            # 强制关闭自动重定向 — 与客户端 SDK 行为一致
            follow_redirects=False,
        )
        self._clients[provider] = client
        return client

    async def aclose(self) -> None:
        for client in self._clients.values():
            try:
                await client.aclose()
            except Exception as exc:  # pragma: no cover - defensive
                logger.warning("native_api client close error: %s", exc)
        self._clients.clear()

    # ── 请求处理入口 ───────────────────────────────────────────

    async def dispatch(
        self,
        provider: str,
        rest_path: str,
        request: Request,
    ) -> StarletteResponse:
        """处理单次原生 API 透传请求.

        - ``provider``：``openai`` / ``gemini`` / ``anthropic``
        - ``rest_path``：catch-all 捕获的剩余路径（如 ``v1/chat/completions``）
        """
        from fastapi.responses import Response as FastAPIResponse
        from fastapi.responses import StreamingResponse

        cfg = self._config.get(provider)
        if cfg is None or not cfg.enabled:
            return FastAPIResponse(
                content=json.dumps(
                    {
                        "error": {
                            "message": f"native api provider '{provider}' is not enabled",
                            "type": "not_found",
                        }
                    }
                ).encode(),
                status_code=404,
                media_type="application/json",
            )

        method = request.method.upper()
        # 防御性 URL 解码：确保 %3A → : 以兼容 Gemini :verb 路径语法。
        # ASGI 规范要求 scope["path"] 已解码，但部分服务器/反向代理对
        # 合法路径字符（如冒号）可能保留编码形态。
        decoded_rest_path = unquote(rest_path)
        operation = OperationClassifier.classify(provider, method, decoded_rest_path)
        endpoint = (
            decoded_rest_path
            if decoded_rest_path.startswith("/")
            else f"/{decoded_rest_path}"
        )

        upstream_headers = _filter_request_headers(dict(request.headers))
        # 强制 identity —— 阻止上游压缩（httpx 默认会自动补 gzip,deflate;
        # 响应端即使剥 content-encoding 也已被 httpx 解压，长度/字节错位）。
        upstream_headers["accept-encoding"] = "identity"
        body_bytes = await request.body()
        query_string = str(request.url.query)

        start_ts = time.perf_counter()
        client = self._get_client(provider)

        # ── Gemini embedding Vertex AI 格式转换 ──────────────────
        # 当上游非官方 Google AI Studio（generativelanguage.googleapis.com）时，
        # litellm 发送的 Google AI Studio 格式（v1beta/models/{model}:batchEmbedContents）
        # 需转换为 Vertex AI 格式（v1beta1/publishers/google/models/{model}:embedContent）。
        vertex_rewrite = (
            provider == "gemini"
            and operation in ("embedding", "embedding.batch")
            and cfg.base_url
            and "generativelanguage.googleapis.com" not in cfg.base_url
        )
        if vertex_rewrite:
            return await self._dispatch_gemini_vertex_embedding(
                client=client,
                operation=operation,
                endpoint=endpoint,
                body_bytes=body_bytes,
                upstream_headers=upstream_headers,
                query_string=query_string,
                provider=provider,
                start_ts=start_ts,
            )

        # 构造上游 URL（保留 query）
        upstream_url = endpoint
        if query_string:
            upstream_url = f"{endpoint}?{query_string}"

        req = client.build_request(
            method=method,
            url=upstream_url,
            content=body_bytes if body_bytes else None,
            headers=upstream_headers,
        )

        # 发送 — 流式方式读取以便 SSE 透传
        try:
            upstream_resp = await client.send(req, stream=True)
        except (
            httpx.TimeoutException,
            httpx.ConnectError,
            httpx.ReadError,
            httpx.RemoteProtocolError,
        ) as exc:
            duration_ms = int((time.perf_counter() - start_ts) * 1000)
            await self._record_failure(
                provider=provider,
                operation=operation,
                endpoint=endpoint,
                duration_ms=duration_ms,
                reason=str(exc),
            )
            return FastAPIResponse(
                content=json.dumps(
                    {
                        "error": {
                            "message": f"upstream unreachable: {exc}",
                            "type": "api_error",
                        }
                    }
                ).encode(),
                status_code=502,
                media_type="application/json",
            )

        content_type = upstream_resp.headers.get("content-type", "").lower()
        resp_headers = _filter_response_headers(dict(upstream_resp.headers))
        status = upstream_resp.status_code
        vendor_label = _VENDOR_LABEL[provider]

        # ── 流式分支：SSE ──────────────────────────────────────
        if "text/event-stream" in content_type:
            return StreamingResponse(
                self._stream_and_accumulate(
                    upstream_resp,
                    provider=provider,
                    vendor_label=vendor_label,
                    operation=operation,
                    endpoint=endpoint,
                    start_ts=start_ts,
                ),
                status_code=status,
                headers=resp_headers,
                media_type=content_type,
            )

        # ── 非流式：读取全量 body，按 content-type 决定是否抽取 ──
        try:
            raw_body = await upstream_resp.aread()
        finally:
            await upstream_resp.aclose()

        duration_ms = int((time.perf_counter() - start_ts) * 1000)

        extraction = ExtractionResult()
        if "application/json" in content_type and raw_body:
            try:
                parsed = json.loads(raw_body.decode("utf-8", errors="replace"))
                if isinstance(parsed, dict):
                    extraction = extract_usage(
                        provider, operation, parsed, status, dict(upstream_resp.headers)
                    )
            except (json.JSONDecodeError, UnicodeDecodeError) as exc:
                logger.debug("native api non-json response ignored for usage: %s", exc)

        await self._record_usage(
            provider=provider,
            operation=operation,
            endpoint=endpoint,
            duration_ms=duration_ms,
            status=status,
            extraction=extraction,
            evidence_records=_build_nonstream_evidence(
                vendor=vendor_label, extraction=extraction
            ),
        )

        return FastAPIResponse(
            content=raw_body,
            status_code=status,
            headers=resp_headers,
            media_type=content_type or None,
        )

    # ── Gemini embedding → Vertex AI 格式转换 ──────────────────

    # Google AI Studio 路径正则：[v1beta/]models/{model}:{verb}
    # 版本段允许缺失以兼容 litellm `_check_custom_proxy` 丢失 v1beta 前缀的 bug。
    _GEMINI_EMBED_PATH_RE = re.compile(
        r"^/?(?:v1(?:beta1?)?/)?models/(?P<model>[^/:]+)(?::|%3A)(?P<verb>embedContent|batchEmbedContents)/?$"
    )

    async def _dispatch_gemini_vertex_embedding(
        self,
        *,
        client: httpx.AsyncClient,
        operation: str,
        endpoint: str,
        body_bytes: bytes,
        upstream_headers: dict[str, str],
        query_string: str,
        provider: str,
        start_ts: float,
    ) -> StarletteResponse:
        """将 Google AI Studio 格式的 embedding 请求转换为 Vertex AI 格式.

        Google AI Studio:
          POST v1beta/models/{model}:batchEmbedContents
          Body: {"requests": [{"model": "models/{model}", "content": {...}}]}

        Vertex AI:
          POST v1beta1/publishers/google/models/{model}:embedContent
          Body: {"content": {...}}
        """
        from fastapi.responses import Response as FastAPIResponse

        match = self._GEMINI_EMBED_PATH_RE.match(endpoint)
        if not match:
            return FastAPIResponse(
                content=json.dumps(
                    {
                        "error": {
                            "message": f"unrecognized gemini embedding path: {endpoint}"
                        }
                    }
                ).encode(),
                status_code=400,
                media_type="application/json",
            )

        model_name = match.group("model")
        verb = match.group("verb")

        # 解析原始请求体
        try:
            body = json.loads(body_bytes) if body_bytes else {}
        except (json.JSONDecodeError, UnicodeDecodeError):
            return FastAPIResponse(
                content=json.dumps(
                    {"error": {"message": "invalid JSON body for embedding request"}}
                ).encode(),
                status_code=400,
                media_type="application/json",
            )

        if verb == "batchEmbedContents":
            return await self._vertex_batch_embed(
                client=client,
                model_name=model_name,
                body=body,
                upstream_headers=upstream_headers,
                query_string=query_string,
                provider=provider,
                operation=operation,
                endpoint=endpoint,
                start_ts=start_ts,
            )

        # 单次 embedContent：直接转换
        content = body.get("content", body)
        return await self._vertex_single_embed(
            client=client,
            model_name=model_name,
            content=content,
            upstream_headers=upstream_headers,
            query_string=query_string,
            provider=provider,
            operation=operation,
            endpoint=endpoint,
            start_ts=start_ts,
        )

    async def _vertex_single_embed(
        self,
        *,
        client: httpx.AsyncClient,
        model_name: str,
        content: dict,
        upstream_headers: dict[str, str],
        query_string: str,
        provider: str,
        operation: str,
        endpoint: str,
        start_ts: float,
    ) -> StarletteResponse:
        """发送单次 Vertex AI embedContent 请求."""
        from fastapi.responses import Response as FastAPIResponse

        vertex_path = f"/v1beta1/publishers/google/models/{model_name}:embedContent"
        vertex_url = vertex_path
        if query_string:
            vertex_url = f"{vertex_path}?{query_string}"

        vertex_body = json.dumps({"content": content}).encode()

        req = client.build_request(
            method="POST",
            url=vertex_url,
            content=vertex_body,
            headers=upstream_headers,
        )

        try:
            upstream_resp = await client.send(req, stream=True)
        except (
            httpx.TimeoutException,
            httpx.ConnectError,
            httpx.ReadError,
            httpx.RemoteProtocolError,
        ) as exc:
            duration_ms = int((time.perf_counter() - start_ts) * 1000)
            await self._record_failure(
                provider=provider,
                operation=operation,
                endpoint=endpoint,
                duration_ms=duration_ms,
                reason=str(exc),
            )
            return FastAPIResponse(
                content=json.dumps(
                    {
                        "error": {
                            "message": f"upstream unreachable: {exc}",
                            "type": "api_error",
                        }
                    }
                ).encode(),
                status_code=502,
                media_type="application/json",
            )

        try:
            raw_body = await upstream_resp.aread()
        finally:
            await upstream_resp.aclose()

        duration_ms = int((time.perf_counter() - start_ts) * 1000)
        status = upstream_resp.status_code
        content_type = upstream_resp.headers.get("content-type", "").lower()
        resp_headers = _filter_response_headers(dict(upstream_resp.headers))

        # 用量抽取
        extraction = ExtractionResult()
        if "application/json" in content_type and raw_body:
            try:
                parsed = json.loads(raw_body.decode("utf-8", errors="replace"))
                if isinstance(parsed, dict):
                    extraction = extract_usage(
                        provider, operation, parsed, status, dict(upstream_resp.headers)
                    )
            except (json.JSONDecodeError, UnicodeDecodeError):
                pass

        vendor_label = _VENDOR_LABEL[provider]
        await self._record_usage(
            provider=provider,
            operation=operation,
            endpoint=endpoint,
            duration_ms=duration_ms,
            status=status,
            extraction=extraction,
            evidence_records=_build_nonstream_evidence(
                vendor=vendor_label, extraction=extraction
            ),
        )

        return FastAPIResponse(
            content=raw_body,
            status_code=status,
            headers=resp_headers,
            media_type=content_type or None,
        )

    async def _vertex_batch_embed(
        self,
        *,
        client: httpx.AsyncClient,
        model_name: str,
        body: dict,
        upstream_headers: dict[str, str],
        query_string: str,
        provider: str,
        operation: str,
        endpoint: str,
        start_ts: float,
    ) -> StarletteResponse:
        """将 batchEmbedContents 拆分为多次 embedContent 调用并聚合响应."""
        from fastapi.responses import Response as FastAPIResponse

        requests_list = body.get("requests", [])
        if not requests_list:
            return FastAPIResponse(
                content=json.dumps(
                    {
                        "error": {
                            "message": "batchEmbedContents requires non-empty 'requests' field"
                        }
                    }
                ).encode(),
                status_code=400,
                media_type="application/json",
            )

        vertex_path = f"/v1beta1/publishers/google/models/{model_name}:embedContent"
        vertex_url = vertex_path
        if query_string:
            vertex_url = f"{vertex_path}?{query_string}"

        # 并发发送所有 embedContent 请求
        async def _single(req_body: dict) -> tuple[dict, int]:
            content = req_body.get("content", req_body)
            vertex_body = json.dumps({"content": content}).encode()
            req = client.build_request(
                method="POST",
                url=vertex_url,
                content=vertex_body,
                headers=upstream_headers,
            )
            resp = await client.send(req, stream=False)
            try:
                return resp.json(), resp.status_code
            except Exception:
                return {"error": {"message": resp.text[:200]}}, resp.status_code

        results = await asyncio.gather(*[_single(r) for r in requests_list])

        # 检查是否有失败的请求
        embeddings = []
        for resp_json, resp_status in results:
            if resp_status != 200:
                # 返回第一个错误
                return FastAPIResponse(
                    content=json.dumps(resp_json).encode(),
                    status_code=resp_status,
                    media_type="application/json",
                )
            embedding_data = resp_json.get("embedding", {})
            embeddings.append(embedding_data)

        # 聚合为 batchEmbedContents 响应格式
        batch_response = {"embeddings": embeddings}
        duration_ms = int((time.perf_counter() - start_ts) * 1000)

        # 用量抽取
        extraction = ExtractionResult()
        for resp_json, _ in results:
            if isinstance(resp_json, dict):
                ext = extract_usage(provider, operation, resp_json, 200, {})
                extraction = ExtractionResult(
                    input_tokens=extraction.input_tokens + ext.input_tokens,
                    output_tokens=extraction.output_tokens + ext.output_tokens,
                    cache_creation_tokens=extraction.cache_creation_tokens
                    + ext.cache_creation_tokens,
                    cache_read_tokens=extraction.cache_read_tokens
                    + ext.cache_read_tokens,
                    request_id=ext.request_id or extraction.request_id,
                    model_served=ext.model_served or extraction.model_served,
                    raw_usage=ext.raw_usage or extraction.raw_usage,
                    source_field_map=ext.source_field_map
                    or extraction.source_field_map,
                    evidence_kind=ext.evidence_kind or extraction.evidence_kind,
                    extra_usage=ext.extra_usage or extraction.extra_usage,
                )

        vendor_label = _VENDOR_LABEL[provider]
        await self._record_usage(
            provider=provider,
            operation=operation,
            endpoint=endpoint,
            duration_ms=duration_ms,
            status=200,
            extraction=extraction,
            evidence_records=_build_nonstream_evidence(
                vendor=vendor_label, extraction=extraction
            ),
        )

        return FastAPIResponse(
            content=json.dumps(batch_response).encode(),
            status_code=200,
            media_type="application/json",
        )

    # ── SSE 流式转发（同时累加 usage） ─────────────────────────

    async def _stream_and_accumulate(
        self,
        upstream_resp: httpx.Response,
        *,
        provider: str,
        vendor_label: str,
        operation: str,
        endpoint: str,
        start_ts: float,
    ) -> AsyncIterator[bytes]:
        acc = StreamingUsageAccumulator(vendor_label=vendor_label)
        try:
            # 因请求侧已强制 ``Accept-Encoding: identity``，上游不会压缩响应，
            # 这里用 ``aiter_bytes()`` 与 ``aiter_raw()`` 等价且兼容 MockTransport。
            async for chunk in upstream_resp.aiter_bytes():
                if chunk:
                    acc.feed(chunk)
                    yield chunk
        except (
            httpx.TimeoutException,
            httpx.ConnectError,
            httpx.ReadError,
            httpx.RemoteProtocolError,
        ) as exc:
            logger.warning(
                "native api stream interrupted provider=%s op=%s: %s",
                provider,
                operation,
                exc,
            )
        finally:
            try:
                await upstream_resp.aclose()
            except Exception:  # pragma: no cover - defensive
                pass

        duration_ms = int((time.perf_counter() - start_ts) * 1000)
        extraction, evidence = acc.finalize(
            vendor=vendor_label, model_served="", request_id=""
        )
        await self._record_usage(
            provider=provider,
            operation=operation,
            endpoint=endpoint,
            duration_ms=duration_ms,
            status=upstream_resp.status_code,
            extraction=extraction,
            evidence_records=evidence,
        )

    # ── 用量记录（走 UsageRecorder 统一入口） ──────────────────

    async def _record_usage(
        self,
        *,
        provider: str,
        operation: str,
        endpoint: str,
        duration_ms: int,
        status: int,
        extraction: ExtractionResult,
        evidence_records: list[dict] | None,
    ) -> None:
        if self._usage_recorder is None:
            return
        vendor = _VENDOR_LABEL[provider]
        usage = self._usage_recorder.build_usage_info(
            {
                "input_tokens": extraction.input_tokens,
                "output_tokens": extraction.output_tokens,
                "cache_creation_tokens": extraction.cache_creation_tokens,
                "cache_read_tokens": extraction.cache_read_tokens,
                "request_id": extraction.request_id,
            }
        )
        model_served = extraction.model_served or "unknown"
        try:
            await self._usage_recorder.record(
                vendor=vendor,
                model_requested=model_served,
                model_served=model_served,
                usage=usage,
                duration_ms=duration_ms,
                success=200 <= status < 400,
                failover=False,
                failover_from=None,
                evidence_records=evidence_records,
                client_category="api",
                operation=operation,
                endpoint=endpoint,
                extra_usage=extraction.extra_usage or None,
            )
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning(
                "native api record_usage failed provider=%s op=%s: %s",
                provider,
                operation,
                exc,
            )

    async def _record_failure(
        self,
        *,
        provider: str,
        operation: str,
        endpoint: str,
        duration_ms: int,
        reason: str,
    ) -> None:
        if self._usage_recorder is None:
            return
        vendor = _VENDOR_LABEL[provider]
        usage = self._usage_recorder.build_usage_info({})
        try:
            await self._usage_recorder.record(
                vendor=vendor,
                model_requested="unknown",
                model_served="unknown",
                usage=usage,
                duration_ms=duration_ms,
                success=False,
                failover=False,
                failover_from=None,
                evidence_records=None,
                client_category="api",
                operation=operation,
                endpoint=endpoint,
                extra_usage={"failure_reason": reason[:200]},
            )
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning(
                "native api record_failure failed provider=%s op=%s: %s",
                provider,
                operation,
                exc,
            )


# ── 头过滤工具 ────────────────────────────────────────────────


def _filter_request_headers(headers: dict[str, str]) -> dict[str, str]:
    """剥除 hop-by-hop / 代理相关头，保留认证与业务头."""
    out: dict[str, str] = {}
    for k, v in headers.items():
        if k.lower() in _HOP_BY_HOP_HEADERS:
            continue
        out[k] = v
    return out


def _filter_response_headers(headers: dict[str, str]) -> dict[str, str]:
    out: dict[str, str] = {}
    for k, v in headers.items():
        if k.lower() in _RESPONSE_HOP_BY_HOP_HEADERS:
            continue
        out[k] = v
    return out


def _build_nonstream_evidence(
    *, vendor: str, extraction: ExtractionResult
) -> list[dict]:
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


# 在 handler 模块加载时同步触发三家 provider 抽取器注册（side-effect import）
from . import extractors as _extractors  # noqa: E402, F401

__all__ = ["NativeProxyHandler"]
