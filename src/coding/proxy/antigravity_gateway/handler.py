"""Antigravity Gateway 透传核心 — ``AntigravityGatewayHandler``.

设计原则（与 :mod:`..native_api.handler` 同源，机制经
:mod:`..passthrough_kernel` 共享）：

- **字节级透传** — 请求 / 响应体、query string、认证头原样转发，
  绝不改写（v1internal 为未文档化私有契约，任何改写都可能破坏客户端）；
- **旁路记账** — 请求侧解析 v1internal 信封取 ``model`` / ``requestId``
  （session 维度），响应侧 tee 解析 ``usageMetadata`` 落账；
  解析失败静默降级，不影响主链路；
- **身份维度** — 落账 ``client_category='antigravity'``、
  ``vendor='antigravity-gateway'``（与反方向 vendor ``antigravity``
  的 cc 流量区分，避免定价/看板口径串台）；
- **不保管凭据** — IDE 登录态的 ``Authorization: Bearer`` 由客户端自带。
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import AsyncIterator
from typing import TYPE_CHECKING
from urllib.parse import unquote

import httpx

from ..passthrough_kernel import (
    UPSTREAM_NETWORK_ERRORS,
    ExtractionResult,
    build_502_response_body,
    build_nonstream_evidence_records,
    build_not_enabled_response_body,
    build_upstream_url,
    filter_request_headers,
    filter_response_headers,
    iter_sse_tee,
)
from .envelope import (
    RequestEnvelope,
    V1InternalStreamAccumulator,
    extract_v1internal_usage,
    parse_request_envelope,
)
from .operation import classify_v1internal_path

if TYPE_CHECKING:
    from fastapi import Request
    from starlette.responses import Response as StarletteResponse

    from ..logging.db import TokenLogger
    from ..pricing import PricingTable
    from ..routing.usage_recorder import UsageRecorder
    from .config import AntigravityGatewayConfig

logger = logging.getLogger(__name__)

# usage_log.vendor 标签 — 'antigravity' 已被反方向 vendor（cc 链路）占用
VENDOR_LABEL = "antigravity-gateway"

# usage_log.client_category — 与 'cc' / 'api' 并列的第三类客户端身份
CLIENT_CATEGORY = "antigravity"


class AntigravityGatewayHandler:
    """Antigravity 客户端透传处理器.

    生命周期与 FastAPI app 同寿命（构造于 ``create_app``，关闭于 lifespan 结束）。
    持有独立 ``httpx.AsyncClient``，与 native_api / vendor client 均不共享。
    """

    def __init__(
        self,
        config: AntigravityGatewayConfig,
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
        self._client: httpx.AsyncClient | None = None

    # ── 生命周期 ───────────────────────────────────────────────

    def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            timeout = httpx.Timeout(
                self._config.timeout_ms / 1000.0,
                connect=self._config.connect_timeout_ms / 1000.0,
            )
            self._client = httpx.AsyncClient(
                base_url=self._config.base_url.rstrip("/"),
                timeout=timeout,
                transport=self._transport,
                follow_redirects=False,
            )
        return self._client

    async def aclose(self) -> None:
        if self._client is not None:
            try:
                await self._client.aclose()
            except Exception as exc:  # pragma: no cover - defensive
                logger.warning("antigravity gateway client close error: %s", exc)
            self._client = None

    # ── 请求处理入口 ───────────────────────────────────────────

    async def dispatch(
        self,
        rest_path: str,
        request: Request,
    ) -> StarletteResponse:
        """处理单次 Antigravity 透传请求（``/api/antigravity/{rest_path}``）."""
        from fastapi.responses import Response as FastAPIResponse
        from fastapi.responses import StreamingResponse

        if not self._config.enabled:
            return FastAPIResponse(
                content=build_not_enabled_response_body("antigravity gateway"),
                status_code=404,
                media_type="application/json",
            )

        method = request.method.upper()
        # %3A → :（v1internal 冒号方法路径，与 native_api 同源防御）
        decoded_rest_path = unquote(rest_path)
        operation = classify_v1internal_path(method, decoded_rest_path)
        endpoint = (
            decoded_rest_path
            if decoded_rest_path.startswith("/")
            else f"/{decoded_rest_path}"
        )

        upstream_headers = filter_request_headers(dict(request.headers))
        upstream_headers["accept-encoding"] = "identity"
        body_bytes = await request.body()
        query_string = str(request.url.query)

        # 请求侧信封旁路解析（model / session_key；失败静默）
        envelope = parse_request_envelope(body_bytes)

        start_ts = time.perf_counter()
        client = self._get_client()
        upstream_url = build_upstream_url(endpoint, query_string)
        req = client.build_request(
            method=method,
            url=upstream_url,
            content=body_bytes if body_bytes else None,
            headers=upstream_headers,
        )

        try:
            upstream_resp = await client.send(req, stream=True)
        except UPSTREAM_NETWORK_ERRORS as exc:
            duration_ms = int((time.perf_counter() - start_ts) * 1000)
            await self._record_failure(
                operation=operation,
                endpoint=endpoint,
                duration_ms=duration_ms,
                reason=str(exc),
                session_key=envelope.session_key,
            )
            return FastAPIResponse(
                content=build_502_response_body(str(exc)),
                status_code=502,
                media_type="application/json",
            )

        content_type = upstream_resp.headers.get("content-type", "").lower()
        resp_headers = filter_response_headers(dict(upstream_resp.headers))
        status = upstream_resp.status_code

        # ── 流式分支：SSE ──────────────────────────────────────
        if "text/event-stream" in content_type:
            return StreamingResponse(
                self._stream_and_accumulate(
                    upstream_resp,
                    operation=operation,
                    endpoint=endpoint,
                    start_ts=start_ts,
                    request_envelope=envelope,
                ),
                status_code=status,
                headers=resp_headers,
                media_type=content_type,
            )

        # ── 非流式：读取全量 body，旁路抽取 usage ──────────────
        try:
            raw_body = await upstream_resp.aread()
        finally:
            await upstream_resp.aclose()

        duration_ms = int((time.perf_counter() - start_ts) * 1000)

        extraction = ExtractionResult(
            model_served=envelope.model,
            request_id=envelope.request_id,
        )
        if "application/json" in content_type and raw_body:
            try:
                parsed = json.loads(raw_body.decode("utf-8", errors="replace"))
                if isinstance(parsed, dict):
                    body_extraction = extract_v1internal_usage(parsed)
                    # 响应侧抽取优先（含真实 usage）；信封兜底 model/requestId
                    extraction = ExtractionResult(
                        input_tokens=body_extraction.input_tokens,
                        output_tokens=body_extraction.output_tokens,
                        cache_read_tokens=body_extraction.cache_read_tokens,
                        request_id=body_extraction.request_id or envelope.request_id,
                        model_served=body_extraction.model_served or envelope.model,
                        extra_usage=body_extraction.extra_usage,
                        raw_usage=body_extraction.raw_usage,
                        source_field_map=body_extraction.source_field_map,
                        evidence_kind=body_extraction.evidence_kind,
                    )
            except (ValueError, UnicodeDecodeError):
                logger.debug("antigravity gateway non-json response ignored for usage")

        await self._record_usage(
            operation=operation,
            endpoint=endpoint,
            duration_ms=duration_ms,
            status=status,
            extraction=extraction,
            evidence_records=build_nonstream_evidence_records(
                vendor=VENDOR_LABEL, extraction=extraction
            ),
            session_key=envelope.session_key,
        )

        return FastAPIResponse(
            content=raw_body,
            status_code=status,
            headers=resp_headers,
            media_type=content_type or None,
        )

    # ── SSE 流式转发（同时累加 usage） ─────────────────────────

    async def _stream_and_accumulate(
        self,
        upstream_resp: httpx.Response,
        *,
        operation: str,
        endpoint: str,
        start_ts: float,
        request_envelope: RequestEnvelope,
    ) -> AsyncIterator[bytes]:
        acc = V1InternalStreamAccumulator()
        # try/finally：客户端中途断开（GeneratorExit 在 yield 处抛出）时，
        # 已累计的 usage 也要结算落账（Antigravity 取消生成是高频操作）
        try:
            async for chunk in iter_sse_tee(
                upstream_resp, acc.feed, log_name="antigravity gateway"
            ):
                yield chunk
        finally:
            duration_ms = int((time.perf_counter() - start_ts) * 1000)
            extraction, evidence = acc.finalize(
                vendor=VENDOR_LABEL,
                model_served=request_envelope.model,
                request_id=request_envelope.request_id,
            )
            await self._record_usage(
                operation=operation,
                endpoint=endpoint,
                duration_ms=duration_ms,
                status=upstream_resp.status_code,
                extraction=extraction,
                evidence_records=evidence,
                session_key=request_envelope.session_key,
            )

    # ── 用量记录（走 UsageRecorder 统一入口） ──────────────────

    async def _record_usage(
        self,
        *,
        operation: str,
        endpoint: str,
        duration_ms: int,
        status: int,
        extraction: ExtractionResult,
        evidence_records: list[dict] | None,
        session_key: str = "",
    ) -> None:
        if self._usage_recorder is None:
            return
        usage = self._usage_recorder.build_usage_info(
            {
                "input_tokens": extraction.input_tokens,
                "output_tokens": extraction.output_tokens,
                "cache_read_tokens": extraction.cache_read_tokens,
                "request_id": extraction.request_id,
            }
        )
        model_served = extraction.model_served or "unknown"
        try:
            await self._usage_recorder.record(
                vendor=VENDOR_LABEL,
                model_requested=model_served,
                model_served=model_served,
                usage=usage,
                duration_ms=duration_ms,
                success=200 <= status < 400,
                failover=False,
                failover_from=None,
                evidence_records=evidence_records,
                client_category=CLIENT_CATEGORY,
                operation=operation,
                endpoint=endpoint,
                extra_usage=extraction.extra_usage or None,
                session_key=session_key,
            )
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning(
                "antigravity gateway record_usage failed op=%s: %s", operation, exc
            )

    async def _record_failure(
        self,
        *,
        operation: str,
        endpoint: str,
        duration_ms: int,
        reason: str,
        session_key: str = "",
    ) -> None:
        if self._usage_recorder is None:
            return
        usage = self._usage_recorder.build_usage_info({})
        try:
            await self._usage_recorder.record(
                vendor=VENDOR_LABEL,
                model_requested="unknown",
                model_served="unknown",
                usage=usage,
                duration_ms=duration_ms,
                success=False,
                failover=False,
                failover_from=None,
                evidence_records=None,
                client_category=CLIENT_CATEGORY,
                operation=operation,
                endpoint=endpoint,
                extra_usage={"failure_reason": reason[:200]},
                session_key=session_key,
            )
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning(
                "antigravity gateway record_failure failed op=%s: %s", operation, exc
            )


__all__ = ["AntigravityGatewayHandler", "CLIENT_CATEGORY", "VENDOR_LABEL"]
