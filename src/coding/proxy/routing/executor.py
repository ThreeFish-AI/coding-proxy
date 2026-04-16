"""路由执行器 — 统一的 tier 迭代门控引擎.

封装 ``route_stream`` / ``route_message`` 共享的 tier 循环、
门控判断与错误处理逻辑，消除两个路由方法间的重复代码。
"""

from __future__ import annotations

import copy
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
from ..vendors.token_manager import TokenAcquireError, TokenErrorKind
from .error_classifier import (
    build_request_capabilities,
    extract_error_payload_from_http_status,
    is_semantic_rejection,
    is_structural_validation_error,
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
    tier_name: str,
    exc: Exception,
    *,
    is_stream: bool = False,
    tier: VendorTier | None = None,
) -> None:
    """记录 HTTP 错误的详细信息（状态码 / 响应体摘要 / 异常类型 / 熔断器快照）.

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
    # 熔断器状态快照
    if tier and tier.circuit_breaker:
        cb = tier.circuit_breaker
        cb_info = cb.get_info()
        detail_parts.append(
            f"  circuit_breaker={cb_info['state']} "
            f"(failures={cb_info['failure_count']}/{cb._failure_threshold})"
        )
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


def _is_likely_request_format_error(
    status_code: int,
    error_body_text: str | None,
    body: dict[str, Any],
) -> bool:
    """判断 HTTP 错误是否可能由请求格式不兼容导致（而非供应商故障）.

    当请求包含 tool_result 且供应商返回 400 时，极大概率是消息格式转换
    问题（如 tool_result 错位、字段缺失等），此类错误不应计入熔断器，
    因为重试同一格式的请求必然再次失败。

    此函数是 :func:`is_semantic_rejection` 的补充——后者依赖结构化 error body
    （JSON），而部分供应商（如 Copilot）的 400 响应可能是纯文本 ``Bad Request``。
    """
    if status_code != 400:
        return False
    if not _has_tool_results(body):
        return False
    # 400 + 有 tool_result + 无法解析为结构化错误 → 高概率格式问题
    if error_body_text is not None:
        trimmed = error_body_text.strip().lower()
        # 纯文本 400 响应（Copilot 等）或无意义的错误体
        if trimmed in ("bad request", "bad request\n", ""):
            return True
        # 非结构化响应体（非 JSON）
        if not trimmed.startswith("{") and len(trimmed) < 200:
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
    "minimax": "Anthropic",
    "kimi": "Anthropic",
    "doubao": "Anthropic",
    "xiaomi": "Anthropic",
    "alibaba": "Anthropic",
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

    def _prepare_body_for_tier(
        self,
        body: dict[str, Any],
        tier: VendorTier,
        normalization: Any = None,
        session_record: Any = None,
    ) -> dict[str, Any]:
        """为指定 tier 准备请求体，必要时应用 Anthropic 专属修复（Phase 2）.

        仅当 tier 为 Anthropic 时才执行以下处理：
        1. tool_result 重定位 + 孤儿修复（需 normalization.has_anthropic_fixes）
        2. 条件化 thinking block 剥离（仅跨供应商场景）

        确保 Zhipu 等其他 vendor 不受影响。
        """
        if tier.name != "anthropic":
            return body

        needs_tool_fixes = (
            normalization is not None and normalization.has_anthropic_fixes
        )
        needs_thinking_strip = self._needs_thinking_strip(normalization, session_record)

        if not needs_tool_fixes and not needs_thinking_strip:
            return body

        from ..server.request_normalizer import (
            apply_anthropic_specific_fixes,
            strip_thinking_blocks,
        )

        body_for_vendor = copy.deepcopy(body)

        if needs_tool_fixes:
            fixes = apply_anthropic_specific_fixes(
                body_for_vendor.get("messages", []),
                normalization.misplaced_tool_results,
                normalization.misplaced_log_info,
            )
            if fixes:
                logger.debug(
                    "Applied Anthropic-specific fixes for tier %s: %s",
                    tier.name,
                    ", ".join(fixes),
                )

        if needs_thinking_strip:
            stripped = strip_thinking_blocks(body_for_vendor)
            if stripped:
                logger.debug(
                    "Stripped %d thinking block(s) for cross-vendor compatibility",
                    stripped,
                )

        return body_for_vendor

    @staticmethod
    def _needs_thinking_strip(normalization: Any, session_record: Any) -> bool:
        """判断是否需要剥离 thinking blocks（仅跨供应商场景）.

        信号优先级：
        1. 请求规范化信号 — 当前请求体中检测到跨供应商产物
        2. 会话历史信号 — provider_state 中存在非 Anthropic 供应商记录

        安全默认：当无法确定会话来源时（session_record 为 None），
        回退到始终剥离，确保与 compat_session_store 未配置时的向后兼容。
        """
        # Signal 1: normalization 检测到跨供应商产物
        if normalization is not None and normalization.has_cross_vendor_signals:
            return True
        # Signal 2: 无会话追踪能力 → 无法判断是否跨供应商 → 安全回退到剥离
        if session_record is None:
            return True
        # Signal 3: 会话历史中有非 Anthropic 供应商
        if session_record.provider_state:
            non_anthropic = {
                v for v in session_record.provider_state if v != "anthropic"
            }
            if non_anthropic:
                return True
        # 纯 Anthropic 会话，无跨供应商信号 → 保留 thinking blocks
        return False

    async def execute_stream(
        self,
        body: dict[str, Any],
        headers: dict[str, str],
        normalization: Any = None,
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
                body_for_tier = self._prepare_body_for_tier(
                    body, tier, normalization, session_record=session_record
                )
                async for chunk in tier.vendor.send_message_stream(
                    body_for_tier, headers
                ):
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
                tier.record_success(
                    info.input_tokens
                    + info.output_tokens
                    + info.cache_creation_tokens
                    + info.cache_read_tokens
                )
                duration = int((time.monotonic() - start) * 1000)
                model = body.get("model", "unknown")
                model_served = usage.get("model_served") or tier.vendor.map_model(model)
                if failed_tier_name is not None:
                    logger.info(
                        "Tier %s stream succeeded (took over from failed tier: %s)",
                        tier.name,
                        failed_tier_name,
                    )
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
                _log_http_error_detail(tier.name, exc, is_stream=True, tier=tier)
                (
                    should_continue,
                    failed_tier_name,
                    last_exc,
                ) = await self._handle_http_error(
                    tier,
                    exc,
                    is_last,
                    failed_tier_name,
                    last_exc,
                    is_stream=True,
                    request_body=body,
                )
                if should_continue:
                    self._log_failover_transition(tier, exc, self._tiers, i)
                    continue
                if is_last:
                    raise
                # 结构性验证错误（如 tool_result 角色错位）不应级联到下一层：
                # 同样的畸形请求转发到其他供应商只会重复失败。
                if isinstance(exc, httpx.HTTPStatusError) and exc.response is not None:
                    if is_structural_validation_error(
                        status_code=exc.response.status_code,
                        error_message=self._extract_error_message_from_http_status(exc),
                    ):
                        logger.info(
                            "Tier %s structural validation error, stopping failover",
                            tier.name,
                        )
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
        normalization: Any = None,
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
                body_for_tier = self._prepare_body_for_tier(
                    body, tier, normalization, session_record=session_record
                )
                resp = await tier.vendor.send_message(body_for_tier, headers)

                if resp.status_code < 400:
                    duration = int((time.monotonic() - start) * 1000)
                    model = body.get("model", "unknown")
                    model_served = resp.model_served or tier.vendor.map_model(model)
                    if failed_tier_name is not None:
                        logger.info(
                            "Tier %s message succeeded (took over from failed tier: %s)",
                            tier.name,
                            failed_tier_name,
                        )
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
                is_semantic = is_semantic_rejection(
                    status_code=resp.status_code,
                    error_type=resp.error_type,
                    error_message=resp.error_message,
                )
                # 补充检测：400 + 有 tool_result + 无结构化错误体 → 格式不兼容
                # （覆盖 Copilot 等返回纯文本 "Bad Request" 的场景）
                if not is_semantic and _is_likely_request_format_error(
                    status_code=resp.status_code,
                    error_body_text=(resp.error_message or "")[:500],
                    body=body,
                ):
                    is_semantic = True
                    logger.warning(
                        "Tier %s likely format incompatibility (400 + tool_results), "
                        "trying next tier without recording failure",
                        tier.name,
                    )

                if not is_last and is_semantic:
                    logger.warning(
                        "Tier %s semantic rejection (%s), trying next tier without recording failure",
                        tier.name,
                        resp.error_type or resp.status_code,
                    )
                    failed_tier_name = tier.name
                    continue

                if tier.vendor.should_trigger_failover(
                    resp.status_code,
                    {"error": {"type": resp.error_type, "message": resp.error_message}},
                ):
                    rl_info = parse_rate_limit_headers(
                        resp.response_headers, resp.status_code, resp.error_message
                    )
                    tier.record_failure(
                        is_cap_error=self._is_cap_error(resp) or rl_info.is_cap_error,
                        retry_after_seconds=compute_effective_retry_seconds(rl_info),
                        rate_limit_deadline=compute_rate_limit_deadline(rl_info),
                    )
                    if not is_last:
                        next_tier = (
                            self._tiers[i + 1] if i + 1 < len(self._tiers) else None
                        )
                        next_info = f" → next: {next_tier.name}" if next_tier else ""
                        logger.warning(
                            "Tier %s error %d, failing over%s",
                            tier.name,
                            resp.status_code,
                            next_info,
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
                _log_http_error_detail(tier.name, exc, is_stream=False, tier=tier)
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
        """处理 TokenAcquireError 的共享逻辑.

        特殊处理：
        - ``INSUFFICIENT_SCOPE`` / ``INVALID_CREDENTIALS`` 属于永久性凭证问题，
          重试无意义，因此**不记录熔断器失败**，避免级联 OPEN 阻塞恢复。
        - 其他临时性错误（网络超时等）正常计入熔断器。
        """
        logger.warning("Tier %s credential error: %s", tier.name, exc)
        is_permanent = exc.kind in (
            TokenErrorKind.INSUFFICIENT_SCOPE,
            TokenErrorKind.INVALID_CREDENTIALS,
        )
        if not is_permanent:
            tier.record_failure()
        else:
            logger.info(
                "Tier %s permanent credential issue (%s), "
                "skipping circuit breaker failure recording",
                tier.name,
                exc.kind.value,
            )
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
        request_body: dict[str, Any] | None = None,
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

            # 补充检测：400 + 有 tool_result + 非结构化错误体 → 格式不兼容
            # （如 Copilot 返回纯文本 "Bad Request\n"）
            # 此类错误不应计入熔断器，因为重试同一请求必然再次失败。
            if (
                not semantic_rejection
                and request_body is not None
                and _is_likely_request_format_error(
                    status_code=exc.response.status_code,
                    error_body_text=exc.response.text[:500]
                    if exc.response.text
                    else None,
                    body=request_body,
                )
            ):
                semantic_rejection = True
                logger.warning(
                    "Tier %s likely format incompatibility (400 + tool_results), "
                    "trying next tier without recording failure",
                    tier.name,
                )

            if semantic_rejection and not is_last:
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
    def _log_failover_transition(
        current_tier: VendorTier,
        exc: Exception,
        tiers: list[VendorTier],
        current_index: int,
    ) -> None:
        """记录 vendor 轮转摘要日志（谁 → 谁，原因）."""
        next_tier = tiers[current_index + 1] if current_index + 1 < len(tiers) else None
        if next_tier is None:
            return

        # 提取错误摘要
        reason = type(exc).__name__
        if isinstance(exc, httpx.HTTPStatusError) and exc.response is not None:
            reason = f"HTTP {exc.response.status_code}"

        logger.info(
            "Failover: %s → %s (reason: %s)",
            current_tier.name,
            next_tier.name,
            reason,
        )

    @staticmethod
    def _is_cap_error(resp: VendorResponse) -> bool:
        """判断是否为订阅用量上限错误."""
        if resp.status_code not in (429, 403):
            return False
        msg = (resp.error_message or "").lower()
        return any(p in msg for p in ("usage cap", "quota", "limit exceeded"))

    @staticmethod
    def _extract_error_message_from_http_status(
        exc: httpx.HTTPStatusError,
    ) -> str | None:
        """从 HTTPStatusError 中提取错误消息文本."""
        if exc.response is None or not exc.response.content:
            return None
        try:
            payload = exc.response.json()
        except Exception:
            return None
        if not isinstance(payload, dict):
            return None
        error = payload.get("error", {})
        if isinstance(error, dict):
            return error.get("message")
        return None
