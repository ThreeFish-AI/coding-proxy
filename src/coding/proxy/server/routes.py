"""路由注册 — 将 FastAPI 路由端点按职责分组注册到 app 实例."""

from __future__ import annotations

import json
import logging
from typing import Any

import httpx
from fastapi import Request, Response
from fastapi.responses import StreamingResponse

from ..vendors.base import NoCompatibleVendorError

# 向后兼容别名
NoCompatibleBackendError = NoCompatibleVendorError  # noqa: F401  (deprecated)
from ..vendors.token_manager import TokenAcquireError
from .responses import (
    extract_stream_http_error,
    json_error_response,
    stream_error_event,
)

logger = logging.getLogger(__name__)


async def _stream_proxy(router: Any, body: dict, headers: dict) -> Any:
    """流式代理生成器."""
    try:
        async for chunk, vendor_name in router.route_stream(body, headers):
            yield chunk
    except NoCompatibleVendorError as exc:
        yield (
            "event: error\n"
            f"data: {json.dumps({'type': 'error', 'error': {'type': 'invalid_request_error', 'message': str(exc), 'details': exc.reasons}}, ensure_ascii=False)}\n\n"
        ).encode()
    except TokenAcquireError as exc:
        yield (
            "event: error\n"
            f"data: {json.dumps({'type': 'error', 'error': {'type': 'authentication_error', 'message': str(exc)}}, ensure_ascii=False)}\n\n"
        ).encode()
    except (httpx.TimeoutException, httpx.ConnectError, httpx.ReadError) as exc:
        yield stream_error_event("api_error", f"上游不可达: {exc}")
    except httpx.HTTPStatusError as exc:
        error_type, message = extract_stream_http_error(exc)
        yield stream_error_event(error_type, message)
    except Exception as exc:
        logger.error(
            "_stream_proxy 未预期异常: %s: %s",
            type(exc).__name__,
            exc,
            exc_info=True,
        )
        yield stream_error_event(
            "api_error",
            f"内部错误: {type(exc).__name__}: {exc}",
        )


def register_core_routes(app: Any, router: Any) -> None:
    """注册核心 API 路由：消息代理与 Token 计数."""

    @app.post("/v1/messages")
    async def messages(request: Request) -> Response:
        """Anthropic Messages API 代理端点."""
        body = await request.json()
        headers = dict(request.headers)
        is_streaming = body.get("stream", False)

        if is_streaming:
            return StreamingResponse(
                _stream_proxy(router, body, headers),
                media_type="text/event-stream",
                headers={"Cache-Control": "no-cache", "Connection": "keep-alive"},
            )

        try:
            resp = await router.route_message(body, headers)
        except NoCompatibleVendorError as exc:
            return json_error_response(
                400,
                error_type="invalid_request_error",
                message=str(exc),
                details=exc.reasons,
            )
        except TokenAcquireError as exc:
            return json_error_response(
                503, error_type="authentication_error", message=str(exc)
            )
        except (httpx.TimeoutException, httpx.ConnectError, httpx.ReadError) as exc:
            return json_error_response(
                502, error_type="api_error", message=f"上游不可达: {exc}"
            )
        except Exception as exc:
            logger.error(
                "messages() 非流式路径未预期异常: %s: %s",
                type(exc).__name__,
                exc,
                exc_info=True,
            )
            return json_error_response(
                500, error_type="api_error", message=f"内部错误: {type(exc).__name__}"
            )

        # 对上游返回的非标准错误格式输出诊断日志（如 Zhipu 使用 code 而非 type）
        if resp.status_code >= 500 and resp.raw_body:
            try:
                payload = json.loads(resp.raw_body)
                if isinstance(payload, dict) and "error" in payload:
                    err = payload["error"]
                    if isinstance(err, dict) and "type" not in err and "code" in err:
                        logger.debug(
                            "检测到非标准上游错误格式（含 code 非 type）: vendor_error=%s",
                            json.dumps(err, ensure_ascii=False)[:200],
                        )
            except (json.JSONDecodeError, UnicodeDecodeError):
                pass

        return Response(
            content=resp.raw_body or b"{}",
            status_code=resp.status_code,
            media_type="application/json",
        )

    @app.post("/v1/messages/count_tokens")
    async def count_tokens(request: Request) -> Response:
        """Token 计数 API 透传 — 使用主供应商转发.

        支持所有提供 Anthropic 兼容端点的供应商（anthropic, zhipu 等）。
        """
        from .factory import _find_count_tokens_vendor

        target_vendor = _find_count_tokens_vendor(router)
        if target_vendor is None:
            return Response(
                content=b'{"error":{"type":"not_implemented","message":"no available vendor for count_tokens"}}',
                status_code=501,
                media_type="application/json",
            )

        body = await request.json()
        headers = dict(request.headers)

        # count_tokens 绕过 executor 直接调用 vendor，此处通过内容感知推断源供应商
        # 后触发对应的 source→target 通道，使兼容性处理与 /v1/messages 保持一致。
        from ..convert.vendor_channels import (
            get_transition_channel,
            infer_source_vendor_from_body,
        )

        source = infer_source_vendor_from_body(body)
        if source:
            channel_fn = get_transition_channel(source, target_vendor.name)
            if channel_fn is not None:
                body, adaptations = channel_fn(body)
                if adaptations:
                    logger.debug(
                        "count_tokens channel %s → %s: %s",
                        source,
                        target_vendor.name,
                        ", ".join(adaptations),
                    )

        prepared_body, prepared_headers = await target_vendor._prepare_request(
            body, headers
        )

        client = target_vendor._get_client()
        url = "/v1/messages/count_tokens"
        if request.query_params:
            url = f"{url}?{request.query_params}"

        try:
            response = await client.post(
                url, json=prepared_body, headers=prepared_headers
            )
            return Response(
                content=response.content,
                status_code=response.status_code,
                media_type="application/json",
            )
        except (httpx.TimeoutException, httpx.ConnectError, httpx.ReadError) as exc:
            logger.warning("count_tokens proxy failed: %s", exc)
            return Response(
                content=b'{"error":{"type":"api_error","message":"count_tokens upstream unreachable"}}',
                status_code=502,
                media_type="application/json",
            )


def register_health_routes(app: Any) -> None:
    """注册健康检查与连通性探测路由."""

    @app.get("/health")
    async def health() -> dict:
        return {"status": "ok"}

    @app.head("/")
    @app.get("/")
    async def root() -> Response:
        """根路径连通性探测 — Claude Code 在建连前发送 HEAD / 作为 health probe."""
        return Response(status_code=200)


def register_status_route(app: Any, router: Any) -> None:
    """注册状态查询路由."""

    @app.get("/api/status")
    async def status() -> dict:
        result: dict[str, Any] = {"tiers": []}
        for tier in router.tiers:
            info: dict[str, Any] = {"name": tier.name}
            if tier.circuit_breaker:
                info["circuit_breaker"] = tier.circuit_breaker.get_info()
            if tier.quota_guard and tier.quota_guard.enabled:
                info["quota_guard"] = tier.quota_guard.get_info()
            if tier.weekly_quota_guard and tier.weekly_quota_guard.enabled:
                info["weekly_quota_guard"] = tier.weekly_quota_guard.get_info()
            info["rate_limit"] = tier.get_rate_limit_info()
            diagnostics = tier.vendor.get_diagnostics()
            if diagnostics:
                info["diagnostics"] = diagnostics
            result["tiers"].append(info)
        return result


def register_copilot_routes(app: Any, router: Any) -> None:
    """注册 Copilot 诊断与模型探测路由."""
    from .factory import _find_copilot_vendor

    @app.get("/api/copilot/diagnostics")
    async def copilot_diagnostics() -> Response:
        """返回 Copilot 认证与交换链路的脱敏诊断信息."""
        vendor = _find_copilot_vendor(router)
        if vendor is None:
            return json_error_response(
                404, error_type="not_found", message="copilot vendor not enabled"
            )
        return Response(
            content=json.dumps(vendor.get_diagnostics(), ensure_ascii=False).encode(),
            status_code=200,
            media_type="application/json",
        )

    @app.get("/api/copilot/models")
    async def copilot_models() -> Response:
        """按需探测当前 Copilot 会话可见模型列表."""
        vendor = _find_copilot_vendor(router)
        if vendor is None:
            return json_error_response(
                404, error_type="not_found", message="copilot vendor not enabled"
            )
        try:
            probe = await vendor.probe_models()
        except TokenAcquireError as exc:
            return json_error_response(
                503, error_type="authentication_error", message=str(exc)
            )
        except (httpx.TimeoutException, httpx.ConnectError) as exc:
            return json_error_response(
                502,
                error_type="api_error",
                message=f"copilot models probe failed: {exc}",
            )
        return Response(
            content=json.dumps(probe, ensure_ascii=False).encode(),
            status_code=200 if probe.get("probe_status") == "ok" else 502,
            media_type="application/json",
        )


def register_admin_routes(app: Any, router: Any) -> None:
    """注册管理操作路由（重置等）."""

    @app.post("/api/reset")
    async def reset_circuit(request: Request) -> Response:
        """重置所有层级的熔断器/配额守卫/rate limit.

        可选 JSON body ``{"vendors": ["v1", "v2", ...]}`` 支持运行时重排序：
        - 单个 vendor → 提升至最高优先级，其余保持相对顺序
        - 多个 vendor → 替换整个 N-tier 链路顺序（需覆盖所有 vendor）
        """
        # 解析可选 body
        vendor_names: list[str] | None = None
        try:
            body = await request.json()
            if isinstance(body, dict):
                raw = body.get("vendors")
                if isinstance(raw, list) and raw:
                    vendor_names = [str(v) for v in raw]
        except Exception:
            # 无 body 或非 JSON → 仅 reset（向后兼容）
            pass

        # 重排序（如果指定）
        if vendor_names is not None:
            try:
                if len(vendor_names) == 1:
                    router.promote_vendor(vendor_names[0])
                else:
                    router.reorder_tiers(vendor_names)
            except ValueError as exc:
                return json_error_response(
                    400,
                    error_type="invalid_request_error",
                    message=str(exc),
                )

        # 全量 reset
        for tier in router.tiers:
            if tier.circuit_breaker:
                tier.circuit_breaker.reset()
            if tier.quota_guard:
                tier.quota_guard.reset()
            if tier.weekly_quota_guard:
                tier.weekly_quota_guard.reset()
            tier.reset_rate_limit()

        result: dict[str, Any] = {"status": "ok"}
        if vendor_names is not None:
            result["tier_order"] = router.get_vendor_names()

        return Response(
            content=json.dumps(result, ensure_ascii=False).encode(),
            status_code=200,
            media_type="application/json",
        )


def register_reauth_routes(app: Any, reauth_coordinator: Any) -> None:
    """注册重认证路由."""

    @app.get("/api/reauth/status")
    async def reauth_status() -> dict:
        """查询运行时重认证状态."""
        if not reauth_coordinator:
            return {"providers": {}}
        return {"providers": reauth_coordinator.get_status()}

    @app.post("/api/reauth/{provider}")
    async def trigger_reauth(provider: str) -> Response:
        """手动触发指定 provider 的运行时重认证."""
        if not reauth_coordinator:
            return Response(
                content=b'{"error":"reauth not available"}',
                status_code=404,
                media_type="application/json",
            )
        await reauth_coordinator.request_reauth(provider)
        return Response(
            content=b'{"status":"reauth requested"}',
            status_code=202,
            media_type="application/json",
        )


def register_all_routes(
    app: Any, router: Any, reauth_coordinator: Any | None = None
) -> None:
    """一次性注册所有路由分组."""
    register_core_routes(app, router)
    register_health_routes(app)
    register_status_route(app, router)
    register_copilot_routes(app, router)
    register_admin_routes(app, router)
    if reauth_coordinator:
        register_reauth_routes(app, reauth_coordinator)

    from .dashboard import register_dashboard_routes

    register_dashboard_routes(app)
