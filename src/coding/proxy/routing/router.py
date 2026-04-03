"""请求路由器 — N-tier 链式路由与自动故障转移."""

from __future__ import annotations

import json
import logging
import time
from typing import TYPE_CHECKING, Any, AsyncIterator

import httpx

if TYPE_CHECKING:
    from ..pricing import PricingTable

from ..backends.base import BackendResponse, UsageInfo
from ..backends.base import NoCompatibleBackendError, RequestCapabilities
from ..backends.token_manager import TokenAcquireError
from ..compat.canonical import (
    CompatibilityStatus,
    CompatibilityTrace,
    build_canonical_request,
)
from ..compat.session_store import CompatSessionRecord, CompatSessionStore
from ..logging.db import TokenLogger
from .rate_limit import (
    compute_effective_retry_seconds,
    compute_rate_limit_deadline,
    parse_rate_limit_headers,
)
from .tier import BackendTier

logger = logging.getLogger(__name__)


def _extract_error_payload_from_http_status(exc: httpx.HTTPStatusError) -> dict[str, Any] | None:
    response = exc.response
    if response is None or not response.content:
        return None
    try:
        payload = response.json()
    except (json.JSONDecodeError, UnicodeDecodeError, TypeError, ValueError):
        return None
    return payload if isinstance(payload, dict) else None


def _is_semantic_rejection(
    *,
    status_code: int,
    error_type: str | None = None,
    error_message: str | None = None,
) -> bool:
    if status_code != 400:
        return False
    normalized_type = (error_type or "").strip().lower()
    if normalized_type == "invalid_request_error":
        return True
    normalized_message = (error_message or "").lower()
    return any(
        marker in normalized_message
        for marker in (
            "invalid_request_error",
            "should match pattern",
            "validation",
            "tool_use_id",
            "server_tool_use",
        )
    )


def _build_request_capabilities(body: dict[str, Any]) -> RequestCapabilities:
    """从请求体提取能力画像."""
    has_images = False
    for msg in body.get("messages", []):
        content = msg.get("content")
        if not isinstance(content, list):
            continue
        if any(
            isinstance(block, dict) and block.get("type") == "image"
            for block in content
        ):
            has_images = True
            break

    return RequestCapabilities(
        has_tools=bool(body.get("tools") or body.get("tool_choice")),
        has_thinking=bool(body.get("thinking") or body.get("extended_thinking")),
        has_images=has_images,
        has_metadata=bool(body.get("metadata")),
    )


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
    entries.append({
        "evidence_kind": evidence_kind,
        "raw_usage": raw_usage,
        "request_id": request_id or "",
        "model_served": model_served or "",
        "source_field_map": {
            "input_tokens": next(
                (key for key in ("input_tokens", "prompt_tokens") if key in raw_usage),
                "",
            ),
            "output_tokens": next(
                (key for key in ("output_tokens", "completion_tokens") if key in raw_usage),
                "",
            ),
            "cache_creation_tokens": next(
                (key for key in ("cache_creation_input_tokens",) if key in raw_usage),
                "",
            ),
            "cache_read_tokens": next(
                (
                    key for key in (
                        "cache_read_input_tokens",
                        "cached_tokens",
                    ) if key in raw_usage
                ),
                "",
            ),
        },
        "cache_signal_present": any(
            key in raw_usage
            for key in ("cache_creation_input_tokens", "cache_read_input_tokens", "cached_tokens")
        ),
    })


def _build_usage_evidence_records(
    usage: dict[str, Any],
    *,
    backend: str,
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
        records.append({
            "backend": backend,
            "request_id": str(entry.get("request_id") or request_id or ""),
            "model_served": str(entry.get("model_served") or model_served or ""),
            "evidence_kind": str(entry.get("evidence_kind") or "stream_usage"),
            "raw_usage_json": json.dumps(raw_usage, ensure_ascii=False, sort_keys=True),
            "parsed_input_tokens": usage.get("input_tokens", 0),
            "parsed_output_tokens": usage.get("output_tokens", 0),
            "parsed_cache_creation_tokens": usage.get("cache_creation_tokens", 0),
            "parsed_cache_read_tokens": usage.get("cache_read_tokens", 0),
            "cache_signal_present": bool(entry.get("cache_signal_present")),
            "source_field_map_json": json.dumps(source_field_map, ensure_ascii=False, sort_keys=True),
        })
    return records


def _parse_usage_from_chunk(chunk: bytes, usage: dict) -> None:
    """从 SSE chunk 提取 token 用量.

    同时支持 Anthropic 原生格式和 OpenAI/Zhipu 兼容格式：
    - Anthropic: data.message.usage.input_tokens / data.usage.output_tokens
    - OpenAI/Zhipu: 顶层 data.usage.prompt_tokens / data.usage.completion_tokens
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
        if isinstance(msg, dict) and "usage" in msg:
            u = msg["usage"]
            input_tokens = u.get("input_tokens", 0) or u.get("prompt_tokens", 0)
            if input_tokens > 0:
                logger.debug("Extracted input tokens from message.usage: %d", input_tokens)
            _set_if_nonzero(usage, "input_tokens", input_tokens)
            _set_if_nonzero(usage, "cache_creation_tokens", u.get("cache_creation_input_tokens", 0))
            _set_if_nonzero(usage, "cache_read_tokens", u.get("cache_read_input_tokens", 0))
            if "id" in msg:
                usage["request_id"] = msg["id"]
            if "model" in msg:
                usage["model_served"] = msg["model"]
            if isinstance(u, dict):
                _append_usage_evidence(
                    usage,
                    evidence_kind="message_usage",
                    raw_usage=dict(u),
                    request_id=msg.get("id"),
                    model_served=msg.get("model"),
                )

        # Anthropic message_delta / OpenAI 最后一个 chunk (data.usage)
        if "usage" in data:
            u = data["usage"]
            output_tokens = u.get("output_tokens", 0) or u.get("completion_tokens", 0)
            input_tokens = u.get("input_tokens", 0) or u.get("prompt_tokens", 0)
            cache_creation_tokens = u.get("cache_creation_input_tokens", 0)
            cache_read_tokens = u.get("cache_read_input_tokens", 0)

            if output_tokens > 0:
                logger.debug("Extracted output tokens from data.usage: %d", output_tokens)
            if input_tokens > 0:
                logger.debug("Extracted input tokens from data.usage: %d (Copilot/OpenAI format)", input_tokens)

            _set_if_nonzero(usage, "output_tokens", output_tokens)
            _set_if_nonzero(usage, "input_tokens", input_tokens)
            _set_if_nonzero(usage, "cache_creation_tokens", cache_creation_tokens)
            _set_if_nonzero(usage, "cache_read_tokens", cache_read_tokens)
            if isinstance(u, dict):
                _append_usage_evidence(
                    usage,
                    evidence_kind="data_usage",
                    raw_usage=dict(u),
                    request_id=data.get("id"),
                    model_served=data.get("model"),
                )

        # request_id fallback (OpenAI 格式下 id 在顶层)
        if "id" in data and not usage.get("request_id"):
            usage["request_id"] = data["id"]


def _has_missing_input_usage_signals(info: UsageInfo) -> bool:
    """判断流式请求是否缺失可解释的输入 usage 信号."""
    if info.output_tokens <= 0:
        return False
    if info.input_tokens > 0:
        return False
    return info.cache_creation_tokens <= 0 and info.cache_read_tokens <= 0


class RequestRouter:
    """路由请求到合适的后端层级，按优先级链式故障转移."""

    # Tier 名称 → OAuth provider 名称的映射
    _TIER_PROVIDER_MAP: dict[str, str] = {
        "copilot": "github",
        "antigravity": "google",
    }

    def __init__(
        self,
        tiers: list[BackendTier],
        token_logger: TokenLogger | None = None,
        reauth_coordinator: Any | None = None,
        compat_session_store: CompatSessionStore | None = None,
    ) -> None:
        if not tiers:
            raise ValueError("至少需要一个后端层级")
        self._tiers = tiers
        self._token_logger = token_logger
        self._reauth_coordinator = reauth_coordinator
        self._pricing_table: PricingTable | None = None
        self._compat_session_store = compat_session_store

    def set_pricing_table(self, table: PricingTable) -> None:
        """注入 PricingTable 实例（由 lifespan 在启动阶段调用）."""
        self._pricing_table = table

    @property
    def tiers(self) -> list[BackendTier]:
        return self._tiers

    @staticmethod
    def _build_usage_info(usage: dict[str, Any]) -> UsageInfo:
        return UsageInfo(
            input_tokens=usage.get("input_tokens", 0),
            output_tokens=usage.get("output_tokens", 0),
            cache_creation_tokens=usage.get("cache_creation_tokens", 0),
            cache_read_tokens=usage.get("cache_read_tokens", 0),
            request_id=usage.get("request_id", ""),
        )

    def _log_model_call(
        self,
        *,
        backend: str,
        model_requested: str,
        model_served: str,
        duration_ms: int,
        usage: UsageInfo,
    ) -> None:
        """打印模型调用级别的详细 Access Log."""
        cost_str = "-"
        if self._pricing_table is not None:
            cost = self._pricing_table.compute_cost(
                backend=backend,
                model_served=model_served,
                input_tokens=usage.input_tokens,
                output_tokens=usage.output_tokens,
                cache_creation_tokens=usage.cache_creation_tokens,
                cache_read_tokens=usage.cache_read_tokens,
            )
            if cost is not None:
                cost_str = f"${cost:.4f}"
        logger.info(
            "ModelCall: backend=%s model_requested=%s model_served=%s "
            "duration=%dms tokens=[in:%d out:%d cache_create:%d cache_read:%d] cost=%s",
            backend, model_requested, model_served, duration_ms,
            usage.input_tokens, usage.output_tokens,
            usage.cache_creation_tokens, usage.cache_read_tokens, cost_str,
        )

    @staticmethod
    def _is_cap_error(resp: BackendResponse) -> bool:
        """判断是否为订阅用量上限错误."""
        if resp.status_code not in (429, 403):
            return False
        msg = (resp.error_message or "").lower()
        return any(p in msg for p in ("usage cap", "quota", "limit exceeded"))

    async def route_stream(
        self,
        body: dict[str, Any],
        headers: dict[str, str],
    ) -> AsyncIterator[tuple[bytes, str]]:
        """路由流式请求，按优先级尝试各层级."""
        last_idx = len(self._tiers) - 1
        last_exc: Exception | None = None
        failed_tier_name: str | None = None
        request_caps = _build_request_capabilities(body)
        canonical_request = build_canonical_request(body, headers)
        session_record = await self._get_or_create_session_record(canonical_request.session_key, canonical_request.trace_id)
        incompatible_reasons: list[str] = []

        for i, tier in enumerate(self._tiers):
            is_last = i == last_idx
            supported, reasons = tier.backend.supports_request(request_caps)
            if not supported:
                reason_text = ",".join(sorted({r.value for r in reasons}))
                incompatible_reasons.append(f"{tier.name}:{reason_text}")
                logger.info(
                    "Tier %s skipped due to incompatible capabilities: %s",
                    tier.name, reason_text,
                )
                continue

            decision = tier.backend.make_compatibility_decision(canonical_request)
            if decision.status is CompatibilityStatus.UNSAFE:
                reason_text = ",".join(sorted(decision.unsupported_semantics))
                incompatible_reasons.append(f"{tier.name}:{reason_text}")
                logger.info("Tier %s skipped due to compatibility decision: %s", tier.name, reason_text)
                continue
            self._apply_compat_context(
                tier=tier,
                canonical_request=canonical_request,
                decision=decision,
                session_record=session_record,
            )

            # 非终端层使用健康检查门控
            if not is_last:
                if not await tier.can_execute_with_health_check():
                    continue
            elif not tier.can_execute() and not is_last:
                continue

            start = time.monotonic()
            usage: dict[str, Any] = {}

            try:
                async for chunk in tier.backend.send_message_stream(body, headers):
                    _parse_usage_from_chunk(chunk, usage)
                    yield chunk, tier.name

                info = self._build_usage_info(usage)
                if _has_missing_input_usage_signals(info):
                    logger.warning(
                        "Stream completed with missing input usage signals: output_tokens=%d, "
                        "cache_creation_tokens=%d, cache_read_tokens=%d, tier=%s, usage_data=%r",
                        info.output_tokens,
                        info.cache_creation_tokens,
                        info.cache_read_tokens,
                        tier.name,
                        usage,
                    )
                tier.record_success(info.input_tokens + info.output_tokens)
                duration = int((time.monotonic() - start) * 1000)
                model = body.get("model", "unknown")
                model_served = usage.get("model_served") or tier.backend.map_model(model)
                self._log_model_call(
                    backend=tier.name, model_requested=model,
                    model_served=model_served, duration_ms=duration, usage=info,
                )
                await self._persist_compat_session(tier.backend.get_compat_trace(), session_record)
                await self._record_usage(
                    tier.name, model, model_served,
                    info, duration, True,
                    failed_tier_name is not None, failed_tier_name,
                    evidence_records=_build_usage_evidence_records(
                        usage,
                        backend=tier.name,
                        model_served=model_served,
                        request_id=info.request_id,
                    ),
                )
                return
            except TokenAcquireError as exc:
                logger.warning("Tier %s credential expired: %s", tier.name, exc)
                tier.record_failure()
                if exc.needs_reauth and self._reauth_coordinator:
                    provider = self._TIER_PROVIDER_MAP.get(tier.name)
                    if provider:
                        await self._reauth_coordinator.request_reauth(provider)
                failed_tier_name = tier.name
                last_exc = exc
                if is_last:
                    raise
            except (httpx.HTTPStatusError, httpx.TimeoutException, httpx.ConnectError, httpx.ReadError) as exc:
                logger.warning("Tier %s stream failed: %s", tier.name, exc)

                semantic_rejection = False
                if isinstance(exc, httpx.HTTPStatusError) and exc.response is not None:
                    payload = _extract_error_payload_from_http_status(exc)
                    error = payload.get("error", {}) if isinstance(payload, dict) else {}
                    semantic_rejection = _is_semantic_rejection(
                        status_code=exc.response.status_code,
                        error_type=error.get("type") if isinstance(error, dict) else None,
                        error_message=error.get("message") if isinstance(error, dict) else None,
                    )
                if semantic_rejection and not is_last:
                    logger.warning(
                        "Tier %s semantic rejection, trying next tier without recording failure",
                        tier.name,
                    )
                    failed_tier_name = tier.name
                    last_exc = exc
                    continue

                # 从 HTTPStatusError 提取 rate limit 信息
                retry_seconds = None
                is_cap = False
                deadline = None
                if isinstance(exc, httpx.HTTPStatusError) and exc.response is not None:
                    rl_info = parse_rate_limit_headers(
                        exc.response.headers,
                        exc.response.status_code,
                        exc.response.text[:500] if exc.response.text else None,
                    )
                    retry_seconds = compute_effective_retry_seconds(rl_info)
                    is_cap = rl_info.is_cap_error
                    deadline = compute_rate_limit_deadline(rl_info)

                tier.record_failure(
                    is_cap_error=is_cap,
                    retry_after_seconds=retry_seconds,
                    rate_limit_deadline=deadline,
                )
                failed_tier_name = tier.name
                last_exc = exc
                if is_last:
                    raise

        if last_exc:
            raise last_exc
        raise NoCompatibleBackendError(
            "当前请求包含仅客户端/MCP 可安全承接的能力，未找到兼容后端",
            reasons=incompatible_reasons,
        )

    async def route_message(
        self,
        body: dict[str, Any],
        headers: dict[str, str],
    ) -> BackendResponse:
        """路由非流式请求，按优先级尝试各层级."""
        last_idx = len(self._tiers) - 1
        start = time.monotonic()
        failed_tier_name: str | None = None
        request_caps = _build_request_capabilities(body)
        canonical_request = build_canonical_request(body, headers)
        session_record = await self._get_or_create_session_record(canonical_request.session_key, canonical_request.trace_id)
        incompatible_reasons: list[str] = []

        for i, tier in enumerate(self._tiers):
            is_last = i == last_idx
            supported, reasons = tier.backend.supports_request(request_caps)
            if not supported:
                reason_text = ",".join(sorted({r.value for r in reasons}))
                incompatible_reasons.append(f"{tier.name}:{reason_text}")
                logger.info(
                    "Tier %s skipped due to incompatible capabilities: %s",
                    tier.name, reason_text,
                )
                continue

            decision = tier.backend.make_compatibility_decision(canonical_request)
            if decision.status is CompatibilityStatus.UNSAFE:
                reason_text = ",".join(sorted(decision.unsupported_semantics))
                incompatible_reasons.append(f"{tier.name}:{reason_text}")
                logger.info("Tier %s skipped due to compatibility decision: %s", tier.name, reason_text)
                continue
            self._apply_compat_context(
                tier=tier,
                canonical_request=canonical_request,
                decision=decision,
                session_record=session_record,
            )

            # 非终端层使用健康检查门控
            if not is_last:
                if not await tier.can_execute_with_health_check():
                    continue
            elif not tier.can_execute() and not is_last:
                continue

            try:
                resp = await tier.backend.send_message(body, headers)

                if resp.status_code < 400:
                    tier.record_success(resp.usage.input_tokens + resp.usage.output_tokens)
                    duration = int((time.monotonic() - start) * 1000)
                    model = body.get("model", "unknown")
                    model_served = resp.model_served or tier.backend.map_model(model)
                    self._log_model_call(
                        backend=tier.name, model_requested=model,
                        model_served=model_served, duration_ms=duration, usage=resp.usage,
                    )
                    await self._persist_compat_session(tier.backend.get_compat_trace(), session_record)
                    await self._record_usage(
                        tier.name, model, model_served,
                        resp.usage, duration, True,
                        failed_tier_name is not None, failed_tier_name,
                        evidence_records=self._build_nonstream_evidence_records(
                            backend=tier.name,
                            model_served=model_served,
                            usage=resp.usage,
                        ),
                    )
                    return resp

                if not is_last and _is_semantic_rejection(
                    status_code=resp.status_code,
                    error_type=resp.error_type,
                    error_message=resp.error_message,
                ):
                    logger.warning(
                        "Tier %s semantic rejection (%s), trying next tier without recording failure",
                        tier.name, resp.error_type or resp.status_code,
                    )
                    failed_tier_name = tier.name
                    continue

                if not is_last and tier.backend.should_trigger_failover(
                    resp.status_code,
                    {"error": {"type": resp.error_type, "message": resp.error_message}},
                ):
                    logger.warning("Tier %s error %d, failing over", tier.name, resp.status_code)

                    # 解析 rate limit 信息
                    rl_info = parse_rate_limit_headers(
                        resp.response_headers,
                        resp.status_code,
                        resp.error_message,
                    )
                    retry_seconds = compute_effective_retry_seconds(rl_info)
                    deadline = compute_rate_limit_deadline(rl_info)

                    tier.record_failure(
                        is_cap_error=self._is_cap_error(resp) or rl_info.is_cap_error,
                        retry_after_seconds=retry_seconds,
                        rate_limit_deadline=deadline,
                    )
                    failed_tier_name = tier.name
                    continue

                duration = int((time.monotonic() - start) * 1000)
                model = body.get("model", "unknown")
                model_served = resp.model_served or tier.backend.map_model(model)
                self._log_model_call(
                    backend=tier.name, model_requested=model,
                    model_served=model_served, duration_ms=duration, usage=resp.usage,
                )
                await self._record_usage(
                    tier.name, model, model_served,
                    resp.usage, duration, resp.status_code < 400,
                    failed_tier_name is not None, failed_tier_name,
                    evidence_records=self._build_nonstream_evidence_records(
                        backend=tier.name,
                        model_served=model_served,
                        usage=resp.usage,
                    ),
                )
                return resp

            except TokenAcquireError as exc:
                logger.warning("Tier %s credential expired: %s", tier.name, exc)
                tier.record_failure()
                if exc.needs_reauth and self._reauth_coordinator:
                    provider = self._TIER_PROVIDER_MAP.get(tier.name)
                    if provider:
                        await self._reauth_coordinator.request_reauth(provider)
                failed_tier_name = tier.name
                if is_last:
                    raise
                continue
            except (httpx.TimeoutException, httpx.ConnectError, httpx.ReadError) as exc:
                logger.warning("Tier %s connection error: %s", tier.name, exc)
                tier.record_failure()  # 连接错误无 rate limit 信息
                failed_tier_name = tier.name
                if is_last:
                    raise
                continue

        if incompatible_reasons:
            raise NoCompatibleBackendError(
                "当前请求包含仅客户端/MCP 可安全承接的能力，未找到兼容后端",
                reasons=incompatible_reasons,
            )
        raise RuntimeError("无可用后端层级")

    async def _record_usage(
        self,
        backend: str,
        model_requested: str,
        model_served: str,
        usage: UsageInfo,
        duration_ms: int,
        success: bool,
        failover: bool,
        failover_from: str | None = None,
        evidence_records: list[dict[str, Any]] | None = None,
    ) -> None:
        if not self._token_logger:
            return
        await self._token_logger.log(
            backend=backend,
            model_requested=model_requested,
            model_served=model_served,
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
            cache_creation_tokens=usage.cache_creation_tokens,
            cache_read_tokens=usage.cache_read_tokens,
            duration_ms=duration_ms,
            success=success,
            failover=failover,
            failover_from=failover_from,
            request_id=usage.request_id,
        )
        if not evidence_records or backend != "copilot":
            return
        if not hasattr(self._token_logger, "log_evidence"):
            return
        for record in evidence_records:
            await self._token_logger.log_evidence(**record)

    @staticmethod
    def _build_nonstream_evidence_records(
        *,
        backend: str,
        model_served: str,
        usage: UsageInfo,
    ) -> list[dict[str, Any]]:
        if backend != "copilot":
            return []
        raw_usage = {
            "input_tokens": usage.input_tokens,
            "output_tokens": usage.output_tokens,
        }
        if usage.cache_creation_tokens > 0:
            raw_usage["cache_creation_input_tokens"] = usage.cache_creation_tokens
        if usage.cache_read_tokens > 0:
            raw_usage["cache_read_input_tokens"] = usage.cache_read_tokens
        return [{
            "backend": backend,
            "request_id": usage.request_id,
            "model_served": model_served,
            "evidence_kind": "nonstream_usage_summary",
            "raw_usage_json": json.dumps(raw_usage, ensure_ascii=False, sort_keys=True),
            "parsed_input_tokens": usage.input_tokens,
            "parsed_output_tokens": usage.output_tokens,
            "parsed_cache_creation_tokens": usage.cache_creation_tokens,
            "parsed_cache_read_tokens": usage.cache_read_tokens,
            "cache_signal_present": usage.cache_creation_tokens > 0 or usage.cache_read_tokens > 0,
            "source_field_map_json": json.dumps({
                "input_tokens": "input_tokens",
                "output_tokens": "output_tokens",
                "cache_creation_tokens": "cache_creation_input_tokens" if usage.cache_creation_tokens > 0 else "",
                "cache_read_tokens": "cache_read_input_tokens" if usage.cache_read_tokens > 0 else "",
            }, ensure_ascii=False, sort_keys=True),
        }]

    async def close(self) -> None:
        for tier in self._tiers:
            await tier.backend.close()

    async def _get_or_create_session_record(
        self,
        session_key: str,
        trace_id: str,
    ) -> CompatSessionRecord | None:
        if self._compat_session_store is None:
            return None
        record = await self._compat_session_store.get(session_key)
        if record is not None:
            return record
        return CompatSessionRecord(session_key=session_key, trace_id=trace_id)

    def _apply_compat_context(
        self,
        *,
        tier: BackendTier,
        canonical_request: Any,
        decision: Any,
        session_record: CompatSessionRecord | None,
    ) -> None:
        provider_protocol = {
            "copilot": "openai_chat_completions",
            "antigravity": "gemini_generate_content",
            "zhipu": "anthropic_messages",
            "anthropic": "anthropic_messages",
        }.get(tier.name, "unknown")
        compat_trace = CompatibilityTrace(
            trace_id=canonical_request.trace_id,
            backend=tier.name,
            session_key=canonical_request.session_key,
            provider_protocol=provider_protocol,
            compat_mode=decision.status.value,
            simulation_actions=list(decision.simulation_actions),
            unsupported_semantics=list(decision.unsupported_semantics),
            session_state_hits=1 if session_record else 0,
            request_adaptations=[],
        )
        tier.backend.set_compat_context(trace=compat_trace, session_record=session_record)

    async def _persist_compat_session(
        self,
        trace: CompatibilityTrace | None,
        session_record: CompatSessionRecord | None,
    ) -> None:
        if self._compat_session_store is None or trace is None or session_record is None:
            return
        provider_states = dict(session_record.provider_state)
        provider_states[trace.backend] = {
            "compat_mode": trace.compat_mode,
            "simulation_actions": trace.simulation_actions,
            "unsupported_semantics": trace.unsupported_semantics,
            "trace_id": trace.trace_id,
        }
        session_record.trace_id = trace.trace_id
        session_record.provider_state = provider_states
        await self._compat_session_store.upsert(session_record)
