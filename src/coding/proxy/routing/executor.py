"""路由执行器 — 统一的 tier 迭代门控引擎.

封装 ``route_stream`` / ``route_message`` 共享的 tier 循环、
门控判断与错误处理逻辑，消除两个路由方法间的重复代码。
"""

from __future__ import annotations

import logging
import time
from collections.abc import AsyncIterator
from typing import Any

import httpx

from ..vendors.base import (
    NoCompatibleVendorError,
    RequestCapabilities,
    VendorResponse,
)
from ..vendors.token_manager import TokenAcquireError
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
from .tier import VendorTier
from .usage_parser import (
    build_usage_evidence_records,
    has_missing_input_usage_signals,
    parse_usage_from_chunk,
)
from .usage_recorder import UsageRecorder

# 向后兼容别名
BackendResponse = VendorResponse
NoCompatibleBackendError = NoCompatibleVendorError
from ..compat.canonical import CompatibilityStatus, build_canonical_request

logger = logging.getLogger(__name__)


def _log_http_error_detail(
    tier_name: str, exc: Exception, *, is_stream: bool = False
) -> None:
    """记录 HTTP 错误的详细信息（状态码 / 响应体摘要 / 异常类型）.

    替代原先单行 ``logger.warning("Tier %s stream failed: %s", ...)``，
    在非 200 响应时输出更丰富的诊断上下文，便于跟踪上游故障根因。
    """
    detail_parts = [f"Tier {tier_name} {'stream' if is_stream else 'message'} failed:"]
    detail_parts.append(f"  exc_type={type(exc).__name__}")
    if isinstance(exc, httpx.HTTPStatusError) and exc.response is not None:
        resp = exc.response
        detail_parts.append(f"  status={resp.status_code}")
        body_preview = (
            (resp.text[:300] if resp.text else "(empty)")
            if resp.content
            else "(no content)"
        )
        detail_parts.append(f"  response_body={body_preview}")
        # 尝试提取 error type / message
        try:
            payload = resp.json() if resp.content else None
        except Exception:
            payload = None
        if isinstance(payload, dict):
            err = payload.get("error", {})
            if isinstance(err, dict):
                detail_parts.append(f"  error_type={err.get('type', 'N/A')}")
                detail_parts.append(f"  error_msg={err.get('message', 'N/A')[:200]}")
    else:
        detail_parts.append(f"  message={str(exc)[:300]}")
    logger.warning("\n".join(detail_parts))


def _has_tool_results(body: dict[str, Any]) -> bool:
    """检测请求体是否包含 tool_result 内容块.

    用于诊断日志中标记「当前请求是否处于工具执行循环」，
    帮助快速定位 vendor 对 tool_result 处理不兼容的问题（如 Zhipu 500）.
    """
    for msg in body.get("messages", []):
        content = msg.get("content")
        if not isinstance(content, list):
            continue
        if any(isinstance(b, dict) and b.get("type") == "tool_result" for b in content):
            return True
    return False


def _log_vendor_response_error(
    tier_name: str,
    resp: VendorResponse,
    body: dict[str, Any],
    *,
    is_stream: bool = False,
) -> None:
    """记录供应商返回的非 200 VendorResponse 详细信息.

    补充 :func:`_log_http_error_detail` 的覆盖盲区：
    当 ``send_message()`` 返回 ``VendorResponse(status_code>=400)``
    而非抛出 httpx 异常时，该函数提供等价的诊断日志能力。

    典型场景：Zhipu 等薄透传供应商将上游 500 原样包装为
    VendorResponse 返回，executor 的异常捕获路径不会触发。
    """
    mode = "stream" if is_stream else "message"
    detail_parts = [f"Tier {tier_name} {mode} vendor error response:"]
    detail_parts.append(f"  status={resp.status_code}")
    detail_parts.append(f"  error_type={resp.error_type or 'N/A'}")
    detail_parts.append(f"  error_msg={(resp.error_message or 'N/A')[:300]}")
    # 请求上下文（模型 / 工具 / 工具结果）
    model = body.get("model", "unknown")
    has_tools = bool(body.get("tools"))
    has_tool_results = _has_tool_results(body)
    detail_parts.append(f"  model={model}")
    detail_parts.append(f"  has_tools={has_tools}")
    detail_parts.append(f"  has_tool_results={has_tool_results}")
    # 响应体摘要
    if resp.raw_body:
        try:
            raw_text = resp.raw_body.decode("utf-8", errors="replace")[:500]
        except (AttributeError, UnicodeDecodeError):
            raw_text = f"(binary, {len(resp.raw_body)} bytes)"
        detail_parts.append(f"  response_body_preview={raw_text}")
    logger.warning("\n".join(detail_parts))


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
        router: Any,  # RequestRouter 引用，用于写入活跃供应商状态
        tiers: list[VendorTier],
        usage_recorder: UsageRecorder,
        session_manager: RouteSessionManager,
        reauth_coordinator: Any | None = None,
    ) -> None:
        self._router = router
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
            canonical_request.session_key,
            canonical_request.trace_id,
        )
        incompatible_reasons: list[str] = []

        for i, tier in enumerate(self._tiers):
            is_last = i == last_idx

            gate = await self._try_gate_tier(
                tier,
                is_last,
                request_caps,
                canonical_request,
                session_record,
                incompatible_reasons,
            )
            if gate == "skip":
                continue

            start = time.monotonic()
            usage: dict[str, Any] = {}

            try:
                async for chunk in tier.vendor.send_message_stream(body, headers):
                    parse_usage_from_chunk(
                        chunk,
                        usage,
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
                model_served = usage.get("model_served") or tier.vendor.map_model(model)
                self._recorder.log_model_call(
                    vendor=tier.name,
                    model_requested=model,
                    model_served=model_served,
                    duration_ms=duration,
                    usage=info,
                )
                await self._session_mgr.persist_session(
                    tier.vendor.get_compat_trace(), session_record
                )
                await self._recorder.record(
                    tier.name,
                    model,
                    model_served,
                    info,
                    duration,
                    True,
                    failed_tier_name is not None,
                    failed_tier_name,
                    evidence_records=build_usage_evidence_records(
                        usage,
                        vendor=tier.name,
                        model_served=model_served,
                        request_id=info.request_id,
                    ),
                )
                self._router._active_vendor_name = tier.name  # 更新活跃供应商
                return

            except TokenAcquireError as exc:
                failed_tier_name, last_exc = await self._handle_token_error(
                    tier, exc, is_last, failed_tier_name
                )
                if is_last and last_exc is exc:
                    raise

            except (
                httpx.HTTPStatusError,
                httpx.TimeoutException,
                httpx.ConnectError,
                httpx.ReadError,
            ) as exc:
                _log_http_error_detail(tier.name, exc, is_stream=True)
                (
                    should_continue,
                    failed_tier_name,
                    last_exc,
                ) = await self._handle_http_error(
                    tier, exc, is_last, failed_tier_name, last_exc, is_stream=True
                )
                if should_continue:
                    continue
                if is_last:
                    raise
            except Exception as exc:
                logger.error(
                    "Tier %s stream unexpected error: %s: %s",
                    tier.name,
                    type(exc).__name__,
                    exc,
                    exc_info=True,
                )
                tier.record_failure()
                failed_tier_name = tier.name
                if not is_last:
                    continue
                raise

        if last_exc:
            raise last_exc
        raise NoCompatibleVendorError(
            "当前请求包含仅客户端/MCP 可安全承接的能力，未找到兼容供应商",
            reasons=incompatible_reasons,
        )

    async def execute_message(
        self,
        body: dict[str, Any],
        headers: dict[str, str],
    ) -> VendorResponse:
        """路由非流式请求，按优先级尝试各层级."""
        last_idx = len(self._tiers) - 1
        start = time.monotonic()
        failed_tier_name: str | None = None
        request_caps = build_request_capabilities(body)
        canonical_request = build_canonical_request(body, headers)
        session_record = await self._session_mgr.get_or_create_record(
            canonical_request.session_key,
            canonical_request.trace_id,
        )
        incompatible_reasons: list[str] = []

        for i, tier in enumerate(self._tiers):
            is_last = i == last_idx

            gate = await self._try_gate_tier(
                tier,
                is_last,
                request_caps,
                canonical_request,
                session_record,
                incompatible_reasons,
            )
            if gate == "skip":
                continue

            try:
                resp = await tier.vendor.send_message(body, headers)

                if resp.status_code < 400:
                    duration = int((time.monotonic() - start) * 1000)
                    model = body.get("model", "unknown")
                    model_served = resp.model_served or tier.vendor.map_model(model)
                    self._recorder.log_model_call(
                        vendor=tier.name,
                        model_requested=model,
                        model_served=model_served,
                        duration_ms=duration,
                        usage=resp.usage,
                    )
                    await self._session_mgr.persist_session(
                        tier.vendor.get_compat_trace(), session_record
                    )
                    await self._recorder.record(
                        tier.name,
                        model,
                        model_served,
                        resp.usage,
                        duration,
                        True,
                        failed_tier_name is not None,
                        failed_tier_name,
                        evidence_records=self._recorder.build_nonstream_evidence_records(
                            vendor=tier.name,
                            model_served=model_served,
                            usage=resp.usage,
                        ),
                    )
                    self._router._active_vendor_name = tier.name  # 更新活跃供应商
                    return resp

                # 非流式的 semantic rejection 和 failover 判断（从响应对象而非异常中提取）
                if not is_last and is_semantic_rejection(
                    status_code=resp.status_code,
                    error_type=resp.error_type,
                    error_message=resp.error_message,
                ):
                    logger.warning(
                        "Tier %s semantic rejection (%s), trying next tier without recording failure",
                        tier.name,
                        resp.error_type or resp.status_code,
                    )
                    failed_tier_name = tier.name
                    continue

                if not is_last and tier.vendor.should_trigger_failover(
                    resp.status_code,
                    {"error": {"type": resp.error_type, "message": resp.error_message}},
                ):
                    logger.warning(
                        "Tier %s error %d, failing over", tier.name, resp.status_code
                    )
                    rl_info = parse_rate_limit_headers(
                        resp.response_headers, resp.status_code, resp.error_message
                    )
                    tier.record_failure(
                        is_cap_error=self._is_cap_error(resp) or rl_info.is_cap_error,
                        retry_after_seconds=compute_effective_retry_seconds(rl_info),
                        rate_limit_deadline=compute_rate_limit_deadline(rl_info),
                    )
                    failed_tier_name = tier.name
                    continue

                # 最后一层或不可 failover 的错误：记录并返回原始响应
                _log_vendor_response_error(tier.name, resp, body, is_stream=False)
                duration = int((time.monotonic() - start) * 1000)
                model = body.get("model", "unknown")
                model_served = resp.model_served or tier.vendor.map_model(model)
                self._recorder.log_model_call(
                    vendor=tier.name,
                    model_requested=model,
                    model_served=model_served,
                    duration_ms=duration,
                    usage=resp.usage,
                )
                await self._recorder.record(
                    tier.name,
                    model,
                    model_served,
                    resp.usage,
                    duration,
                    resp.status_code < 400,
                    failed_tier_name is not None,
                    failed_tier_name,
                    evidence_records=self._recorder.build_nonstream_evidence_records(
                        vendor=tier.name, model_served=model_served, usage=resp.usage
                    ),
                )
                return resp

            except TokenAcquireError as exc:
                failed_tier_name, last_exc = await self._handle_token_error(
                    tier, exc, is_last, failed_tier_name
                )
                if is_last:
                    raise
                continue

            except (httpx.TimeoutException, httpx.ConnectError, httpx.ReadError) as exc:
                _log_http_error_detail(tier.name, exc, is_stream=False)
                tier.record_failure()
                failed_tier_name = tier.name
                if is_last:
                    raise
                continue
            except Exception as exc:
                logger.error(
                    "Tier %s message unexpected error: %s: %s",
                    tier.name,
                    type(exc).__name__,
                    exc,
                    exc_info=True,
                )
                tier.record_failure()
                failed_tier_name = tier.name
                if not is_last:
                    continue
                raise

        if incompatible_reasons:
            raise NoCompatibleVendorError(
                "当前请求包含仅客户端/MCP 可安全承接的能力，未找到兼容供应商",
                reasons=incompatible_reasons,
            )
        raise RuntimeError("无可用供应商层级")

    # ── 门控与错误处理 ──────────────────────────────────────

    async def _try_gate_tier(
        self,
        tier: VendorTier,
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
        supported, reasons = tier.vendor.supports_request(request_caps)
        if not supported:
            reason_text = ",".join(sorted({r.value for r in reasons}))
            incompatible_reasons.append(f"{tier.name}:{reason_text}")
            logger.info(
                "Tier %s skipped due to incompatible capabilities: %s",
                tier.name,
                reason_text,
            )
            return "skip"

        decision = tier.vendor.make_compatibility_decision(canonical_request)
        if decision.status is CompatibilityStatus.UNSAFE:
            reason_text = ",".join(sorted(decision.unsupported_semantics))
            incompatible_reasons.append(f"{tier.name}:{reason_text}")
            logger.info(
                "Tier %s skipped due to compatibility decision: %s",
                tier.name,
                reason_text,
            )
            return "skip"

        self._session_mgr.apply_compat_context(
            tier=tier,
            canonical_request=canonical_request,
            decision=decision,
            session_record=session_record,
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
        tier: VendorTier,
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
        tier: VendorTier,
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
                logger.warning(
                    "Tier %s semantic rejection, trying next tier without recording failure",
                    tier.name,
                )
                return True, tier.name, exc

            rl_info = parse_rate_limit_headers(
                exc.response.headers,
                exc.response.status_code,
                exc.response.text[:500] if exc.response.text else None,
            )
            tier.record_failure(
                is_cap_error=rl_info.is_cap_error,
                retry_after_seconds=compute_effective_retry_seconds(rl_info),
                rate_limit_deadline=compute_rate_limit_deadline(rl_info),
            )
        else:
            tier.record_failure()

        return False, tier.name, exc

    @staticmethod
    def _is_cap_error(resp: VendorResponse) -> bool:
        """判断是否为订阅用量上限错误."""
        if resp.status_code not in (429, 403):
            return False
        msg = (resp.error_message or "").lower()
        return any(p in msg for p in ("usage cap", "quota", "limit exceeded"))
