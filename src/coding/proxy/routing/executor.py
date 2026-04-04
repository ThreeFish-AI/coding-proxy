"""路由执行器 — 统一的 tier 迭代门控引擎.

封装 ``route_stream`` / ``route_message`` 共享的 tier 循环、
门控判断与错误处理逻辑，消除两个路由方法间的重复代码。
"""

from __future__ import annotations

import logging
import time
from typing import Any, AsyncIterator

import httpx

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
from .session_manager import RouteSessionManager
from .tier import BackendTier
from .usage_parser import (
    build_usage_evidence_records,
    has_missing_input_usage_signals,
    parse_usage_from_chunk,
)
from .usage_recorder import UsageRecorder
from ..backends.base import BackendResponse, NoCompatibleBackendError, RequestCapabilities, UsageInfo
from ..backends.token_manager import TokenAcquireError
from ..compat.canonical import CompatibilityStatus, build_canonical_request

logger = logging.getLogger(__name__)

# tier.name → 上游 Vendor 协议标签映射（用于 token 用量日志标注）
_VENDOR_PROTOCOL_LABEL_MAP: dict[str, str] = {
    "anthropic": "Anthropic",
    "zhipu": "Anthropic",
    "copilot": "OpenAI",
    "antigravity": "Gemini",
}


class _RouteExecutor:
    """统一的 tier 迭代门控引擎.

    职责：
    - 按优先级遍历 tiers，执行能力门控与健康检查
    - 委托具体的流式/非流式执行给调用方回调
    - 统一处理 TokenAcquireError / HTTP 错误 / 语义拒绝
    - 成功后委托 UsageRecorder 记录用量
    """

    def __init__(
        self,
        tiers: list[BackendTier],
        usage_recorder: UsageRecorder,
        session_manager: RouteSessionManager,
        reauth_coordinator: Any | None = None,
    ) -> None:
        self._tiers = tiers
        self._recorder = usage_recorder
        self._session_mgr = session_manager
        self._reauth_coordinator = reauth_coordinator

        # Tier 名称 → OAuth provider 名称的映射
        self._tier_provider_map: dict[str, str] = {
            "copilot": "github",
            "antigravity": "google",
        }

    # ── 公开执行入口 ──────────────────────────────────────

    async def execute_stream(
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
        session_record = await self._session_mgr.get_or_create_record(
            canonical_request.session_key, canonical_request.trace_id,
        )
        incompatible_reasons: list[str] = []

        for i, tier in enumerate(self._tiers):
            is_last = i == last_idx

            gate = await self._try_gate_tier(tier, is_last, request_caps, canonical_request, session_record, incompatible_reasons)
            if gate == "skip":
                continue

            start = time.monotonic()
            usage: dict[str, Any] = {}

            try:
                async for chunk in tier.backend.send_message_stream(body, headers):
                    parse_usage_from_chunk(
                        chunk, usage,
                        vendor_label=_VENDOR_PROTOCOL_LABEL_MAP.get(tier.name),
                    )
                    yield chunk, tier.name

                info = self._recorder.build_usage_info(usage)
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
                self._recorder.log_model_call(backend=tier.name, model_requested=model, model_served=model_served, duration_ms=duration, usage=info)
                await self._session_mgr.persist_session(tier.backend.get_compat_trace(), session_record)
                await self._recorder.record(
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

    async def execute_message(
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
        session_record = await self._session_mgr.get_or_create_record(
            canonical_request.session_key, canonical_request.trace_id,
        )
        incompatible_reasons: list[str] = []

        for i, tier in enumerate(self._tiers):
            is_last = i == last_idx

            gate = await self._try_gate_tier(tier, is_last, request_caps, canonical_request, session_record, incompatible_reasons)
            if gate == "skip":
                continue

            try:
                resp = await tier.backend.send_message(body, headers)

                if resp.status_code < 400:
                    duration = int((time.monotonic() - start) * 1000)
                    model = body.get("model", "unknown")
                    model_served = resp.model_served or tier.backend.map_model(model)
                    self._recorder.log_model_call(backend=tier.name, model_requested=model, model_served=model_served, duration_ms=duration, usage=resp.usage)
                    await self._session_mgr.persist_session(tier.backend.get_compat_trace(), session_record)
                    await self._recorder.record(
                        tier.name, model, model_served, resp.usage, duration, True,
                        failed_tier_name is not None, failed_tier_name,
                        evidence_records=self._recorder.build_nonstream_evidence_records(backend=tier.name, model_served=model_served, usage=resp.usage),
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
                self._recorder.log_model_call(backend=tier.name, model_requested=model, model_served=model_served, duration_ms=duration, usage=resp.usage)
                await self._recorder.record(
                    tier.name, model, model_served, resp.usage, duration, resp.status_code < 400,
                    failed_tier_name is not None, failed_tier_name,
                    evidence_records=self._recorder.build_nonstream_evidence_records(backend=tier.name, model_served=model_served, usage=resp.usage),
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

    # ── 门控与错误处理 ──────────────────────────────────────

    async def _try_gate_tier(
        self,
        tier: BackendTier,
        is_last: bool,
        request_caps: RequestCapabilities,
        canonical_request: Any,
        session_record: Any,
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

        self._session_mgr.apply_compat_context(
            tier=tier, canonical_request=canonical_request, decision=decision, session_record=session_record,
        )

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
            provider = self._tier_provider_map.get(tier.name)
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

    @staticmethod
    def _is_cap_error(resp: BackendResponse) -> bool:
        """判断是否为订阅用量上限错误."""
        if resp.status_code not in (429, 403):
            return False
        msg = (resp.error_message or "").lower()
        return any(p in msg for p in ("usage cap", "quota", "limit exceeded"))
