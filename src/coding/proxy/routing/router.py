"""请求路由器 — N-tier 链式路由与自动故障转移."""

from __future__ import annotations

import json
import logging
import time
from typing import TYPE_CHECKING, Any, AsyncIterator

import httpx

if TYPE_CHECKING:
    from ..pricing import PricingTable

from .error_classifier import (
    build_request_capabilities,
    extract_error_payload_from_http_status,
    is_semantic_rejection,
)
from .rate_limit import (
    compute_effective_retry_seconds,
    compute_rate_limit_deadline,
    parse_rate_limit_headers,
)
from .tier import BackendTier
from .usage_parser import (
    build_usage_evidence_records,
    has_missing_input_usage_signals,
    parse_usage_from_chunk,
)

from ..backends.base import BackendResponse, NoCompatibleBackendError, RequestCapabilities, UsageInfo
from ..backends.token_manager import TokenAcquireError
from ..compat.canonical import (
    CompatibilityStatus,
    CompatibilityTrace,
    build_canonical_request,
)
from ..compat.session_store import CompatSessionRecord, CompatSessionStore
from ..logging.db import TokenLogger

logger = logging.getLogger(__name__)

# tier.name → 上游 Vendor 协议标签映射（用于 token 用量日志标注）
_VENDOR_LABEL_MAP: dict[str, str] = {
    "anthropic": "Anthropic",
    "zhipu": "Anthropic",
    "copilot": "OpenAI",
    "antigravity": "Gemini",
}


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

    # ── 公开路由接口 ──

    async def route_stream(
        self,
        body: dict[str, Any],
        headers: dict[str, str],
    ) -> AsyncIterator[tuple[bytes, str]]:
        """路由流式请求，按优先级尝试各层级."""
        last_idx = len(self._tiers) - 1
        last_exc: Exception | None = None
        failed_tier_name: str | None = None
        request_caps = build_request_capabilities(body)
        canonical_request = build_canonical_request(body, headers)
        session_record = await self._get_or_create_session_record(canonical_request.session_key, canonical_request.trace_id)
        incompatible_reasons: list[str] = []

        for i, tier in enumerate(self._tiers):
            is_last = i == last_idx

            gate_result = await self._try_gate_tier(tier, is_last, request_caps, canonical_request, session_record, incompatible_reasons)
            if gate_result == "skip":
                continue
            if gate_result == "eligible":
                pass  # 继续执行

            start = time.monotonic()
            usage: dict[str, Any] = {}

            try:
                async for chunk in tier.backend.send_message_stream(body, headers):
                    parse_usage_from_chunk(
                        chunk, usage,
                        vendor_label=_VENDOR_LABEL_MAP.get(tier.name),
                    )
                    yield chunk, tier.name

                info = self._build_usage_info(usage)
                if has_missing_input_usage_signals(info):
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
                self._log_model_call(backend=tier.name, model_requested=model, model_served=model_served, duration_ms=duration, usage=info)
                await self._persist_compat_session(tier.backend.get_compat_trace(), session_record)
                await self._record_usage(
                    tier.name, model, model_served, info, duration, True,
                    failed_tier_name is not None, failed_tier_name,
                    evidence_records=build_usage_evidence_records(usage, backend=tier.name, model_served=model_served, request_id=info.request_id),
                )
                return

            except TokenAcquireError as exc:
                failed_tier_name, last_exc = await self._handle_token_error(tier, exc, is_last, failed_tier_name)
                if is_last and last_exc is exc:
                    raise

            except (httpx.HTTPStatusError, httpx.TimeoutException, httpx.ConnectError, httpx.ReadError) as exc:
                logger.warning("Tier %s stream failed: %s", tier.name, exc)
                should_continue, failed_tier_name, last_exc = await self._handle_http_error(tier, exc, is_last, failed_tier_name, last_exc, is_stream=True)
                if should_continue:
                    continue
                if is_last:
                    raise

        if last_exc:
            raise last_exc
        raise NoCompatibleBackendError("当前请求包含仅客户端/MCP 可安全承接的能力，未找到兼容后端", reasons=incompatible_reasons)

    async def route_message(
        self,
        body: dict[str, Any],
        headers: dict[str, str],
    ) -> BackendResponse:
        """路由非流式请求，按优先级尝试各层级."""
        last_idx = len(self._tiers) - 1
        start = time.monotonic()
        failed_tier_name: str | None = None
        request_caps = build_request_capabilities(body)
        canonical_request = build_canonical_request(body, headers)
        session_record = await self._get_or_create_session_record(canonical_request.session_key, canonical_request.trace_id)
        incompatible_reasons: list[str] = []

        for i, tier in enumerate(self._tiers):
            is_last = i == last_idx

            gate_result = await self._try_gate_tier(tier, is_last, request_caps, canonical_request, session_record, incompatible_reasons)
            if gate_result == "skip":
                continue

            try:
                resp = await tier.backend.send_message(body, headers)

                if resp.status_code < 400:
                    duration = int((time.monotonic() - start) * 1000)
                    model = body.get("model", "unknown")
                    model_served = resp.model_served or tier.backend.map_model(model)
                    self._log_model_call(backend=tier.name, model_requested=model, model_served=model_served, duration_ms=duration, usage=resp.usage)
                    await self._persist_compat_session(tier.backend.get_compat_trace(), session_record)
                    await self._record_usage(
                        tier.name, model, model_served, resp.usage, duration, True,
                        failed_tier_name is not None, failed_tier_name,
                        evidence_records=self._build_nonstream_evidence_records(backend=tier.name, model_served=model_served, usage=resp.usage),
                    )
                    return resp

                # 非流式的 semantic rejection 和 failover 判断（从响应对象而非异常中提取）
                if not is_last and is_semantic_rejection(status_code=resp.status_code, error_type=resp.error_type, error_message=resp.error_message):
                    logger.warning("Tier %s semantic rejection (%s), trying next tier without recording failure", tier.name, resp.error_type or resp.status_code)
                    failed_tier_name = tier.name
                    continue

                if not is_last and tier.backend.should_trigger_failover(resp.status_code, {"error": {"type": resp.error_type, "message": resp.error_message}}):
                    logger.warning("Tier %s error %d, failing over", tier.name, resp.status_code)
                    rl_info = parse_rate_limit_headers(resp.response_headers, resp.status_code, resp.error_message)
                    tier.record_failure(
                        is_cap_error=self._is_cap_error(resp) or rl_info.is_cap_error,
                        retry_after_seconds=compute_effective_retry_seconds(rl_info),
                        rate_limit_deadline=compute_rate_limit_deadline(rl_info),
                    )
                    failed_tier_name = tier.name
                    continue

                # 最后一层或不可 failover 的错误：记录并返回原始响应
                duration = int((time.monotonic() - start) * 1000)
                model = body.get("model", "unknown")
                model_served = resp.model_served or tier.backend.map_model(model)
                self._log_model_call(backend=tier.name, model_requested=model, model_served=model_served, duration_ms=duration, usage=resp.usage)
                await self._record_usage(
                    tier.name, model, model_served, resp.usage, duration, resp.status_code < 400,
                    failed_tier_name is not None, failed_tier_name,
                    evidence_records=self._build_nonstream_evidence_records(backend=tier.name, model_served=model_served, usage=resp.usage),
                )
                return resp

            except TokenAcquireError as exc:
                failed_tier_name, last_exc = await self._handle_token_error(tier, exc, is_last, failed_tier_name)
                if is_last:
                    raise
                continue

            except (httpx.TimeoutException, httpx.ConnectError, httpx.ReadError) as exc:
                logger.warning("Tier %s connection error: %s", tier.name, exc)
                tier.record_failure()
                failed_tier_name = tier.name
                if is_last:
                    raise
                continue

        if incompatible_reasons:
            raise NoCompatibleBackendError("当前请求包含仅客户端/MCP 可安全承接的能力，未找到兼容后端", reasons=incompatible_reasons)
        raise RuntimeError("无可用后端层级")

    # ── 共享的门控与错误处理辅助方法 ──

    async def _try_gate_tier(
        self,
        tier: BackendTier,
        is_last: bool,
        request_caps: RequestCapabilities,
        canonical_request: Any,
        session_record: CompatSessionRecord | None,
        incompatible_reasons: list[str],
    ) -> str:
        """对单个 tier 执行能力门控和兼容性检查.

        Returns:
            "eligible" — 通过所有门控，可执行请求
            "skip" — 未通过门控，跳过此 tier
        """
        supported, reasons = tier.backend.supports_request(request_caps)
        if not supported:
            reason_text = ",".join(sorted({r.value for r in reasons}))
            incompatible_reasons.append(f"{tier.name}:{reason_text}")
            logger.info("Tier %s skipped due to incompatible capabilities: %s", tier.name, reason_text)
            return "skip"

        decision = tier.backend.make_compatibility_decision(canonical_request)
        if decision.status is CompatibilityStatus.UNSAFE:
            reason_text = ",".join(sorted(decision.unsupported_semantics))
            incompatible_reasons.append(f"{tier.name}:{reason_text}")
            logger.info("Tier %s skipped due to compatibility decision: %s", tier.name, reason_text)
            return "skip"

        self._apply_compat_context(tier=tier, canonical_request=canonical_request, decision=decision, session_record=session_record)

        # 非终端层使用健康检查门控；终端层仅检查 can_execute
        if not is_last:
            if not await tier.can_execute_with_health_check():
                return "skip"
        elif not tier.can_execute():
            return "skip"

        return "eligible"

    async def _handle_token_error(
        self,
        tier: BackendTier,
        exc: TokenAcquireError,
        is_last: bool,
        failed_tier_name: str | None,
    ) -> tuple[str | None, Exception]:
        """处理 TokenAcquireError 的共享逻辑."""
        logger.warning("Tier %s credential expired: %s", tier.name, exc)
        tier.record_failure()
        if exc.needs_reauth and self._reauth_coordinator:
            provider = self._TIER_PROVIDER_MAP.get(tier.name)
            if provider:
                await self._reauth_coordinator.request_reauth(provider)
        return tier.name, exc

    async def _handle_http_error(
        self,
        tier: BackendTier,
        exc: Exception,
        is_last: bool,
        failed_tier_name: str | None,
        last_exc: Exception | None,
        *,
        is_stream: bool = False,
    ) -> tuple[bool, str | None, Exception | None]:
        """处理 HTTP 错误的共享逻辑（流式路径）.

        Returns:
            (should_continue, failed_tier_name, last_exc)
        """
        semantic_rejection = False
        if isinstance(exc, httpx.HTTPStatusError) and exc.response is not None:
            payload = extract_error_payload_from_http_status(exc)
            error = payload.get("error", {}) if isinstance(payload, dict) else {}
            semantic_rejection = is_semantic_rejection(
                status_code=exc.response.status_code,
                error_type=error.get("type") if isinstance(error, dict) else None,
                error_message=error.get("message") if isinstance(error, dict) else None,
            )
            if semantic_rejection and not is_last:
                logger.warning("Tier %s semantic rejection, trying next tier without recording failure", tier.name)
                return True, tier.name, exc

            rl_info = parse_rate_limit_headers(exc.response.headers, exc.response.status_code, exc.response.text[:500] if exc.response.text else None)
            tier.record_failure(
                is_cap_error=rl_info.is_cap_error,
                retry_after_seconds=compute_effective_retry_seconds(rl_info),
                rate_limit_deadline=compute_rate_limit_deadline(rl_info),
            )
        else:
            tier.record_failure()

        return False, tier.name, exc

    # ── 用量记录与会话持久化 ──

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
            backend=backend, model_requested=model_requested, model_served=model_served,
            input_tokens=usage.input_tokens, output_tokens=usage.output_tokens,
            cache_creation_tokens=usage.cache_creation_tokens, cache_read_tokens=usage.cache_read_tokens,
            duration_ms=duration_ms, success=success, failover=failover, failover_from=failover_from,
            request_id=usage.request_id,
        )
        if not evidence_records or backend != "copilot":
            return
        if not hasattr(self._token_logger, "log_evidence"):
            return
        for record in evidence_records:
            await self._token_logger.log_evidence(**record)

    @staticmethod
    def _build_nonstream_evidence_records(*, backend: str, model_served: str, usage: UsageInfo) -> list[dict[str, Any]]:
        if backend != "copilot":
            return []
        raw_usage: dict[str, Any] = {"input_tokens": usage.input_tokens, "output_tokens": usage.output_tokens}
        if usage.cache_creation_tokens > 0:
            raw_usage["cache_creation_input_tokens"] = usage.cache_creation_tokens
        if usage.cache_read_tokens > 0:
            raw_usage["cache_read_input_tokens"] = usage.cache_read_tokens
        return [{
            "backend": backend, "request_id": usage.request_id, "model_served": model_served,
            "evidence_kind": "nonstream_usage_summary",
            "raw_usage_json": json.dumps(raw_usage, ensure_ascii=False, sort_keys=True),
            "parsed_input_tokens": usage.input_tokens, "parsed_output_tokens": usage.output_tokens,
            "parsed_cache_creation_tokens": usage.cache_creation_tokens, "parsed_cache_read_tokens": usage.cache_read_tokens,
            "cache_signal_present": usage.cache_creation_tokens > 0 or usage.cache_read_tokens > 0,
            "source_field_map_json": json.dumps({
                "input_tokens": "input_tokens", "output_tokens": "output_tokens",
                "cache_creation_tokens": "cache_creation_input_tokens" if usage.cache_creation_tokens > 0 else "",
                "cache_read_tokens": "cache_read_input_tokens" if usage.cache_read_tokens > 0 else "",
            }, ensure_ascii=False, sort_keys=True),
        }]

    # ── 生命周期与会话管理 ──

    async def close(self) -> None:
        for tier in self._tiers:
            await tier.backend.close()

    async def _get_or_create_session_record(self, session_key: str, trace_id: str) -> CompatSessionRecord | None:
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
            trace_id=canonical_request.trace_id, backend=tier.name,
            session_key=canonical_request.session_key, provider_protocol=provider_protocol,
            compat_mode=decision.status.value, simulation_actions=list(decision.simulation_actions),
            unsupported_semantics=list(decision.unsupported_semantics),
            session_state_hits=1 if session_record else 0, request_adaptations=[],
        )
        tier.backend.set_compat_context(trace=compat_trace, session_record=session_record)

    async def _persist_compat_session(self, trace: CompatibilityTrace | None, session_record: CompatSessionRecord | None) -> None:
        if self._compat_session_store is None or trace is None or session_record is None:
            return
        provider_states = dict(session_record.provider_state)
        provider_states[trace.backend] = {
            "compat_mode": trace.compat_mode, "simulation_actions": trace.simulation_actions,
            "unsupported_semantics": trace.unsupported_semantics, "trace_id": trace.trace_id,
        }
        session_record.trace_id = trace.trace_id
        session_record.provider_state = provider_states
        await self._compat_session_store.upsert(session_record)
