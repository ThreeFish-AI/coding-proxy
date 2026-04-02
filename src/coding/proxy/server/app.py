"""FastAPI 应用."""

from __future__ import annotations

import json
import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import httpx
from fastapi import FastAPI, Request, Response
from fastapi.responses import StreamingResponse

from ..auth.providers.github import GitHubDeviceFlowProvider
from ..auth.providers.google import (
    GoogleOAuthProvider,
    _REQUIRED_SCOPE_SET as _GOOGLE_REQUIRED_SCOPE_SET,
    _DEFAULT_CLIENT_ID as _GOOGLE_DEFAULT_CLIENT_ID,
    _DEFAULT_CLIENT_SECRET as _GOOGLE_DEFAULT_CLIENT_SECRET,
)
from ..auth.runtime import RuntimeReauthCoordinator
from ..auth.store import TokenStoreManager
from ..backends.base import NoCompatibleBackendError
from ..backends.antigravity import AntigravityBackend
from ..backends.anthropic import AnthropicBackend
from ..backends.copilot import CopilotBackend
from ..backends.token_manager import TokenAcquireError
from ..backends.zhipu import ZhipuBackend
from ..config.loader import load_config
from ..compat.session_store import CompatSessionStore
from ..config.schema import (
    AntigravityConfig,
    AnthropicConfig,
    CircuitBreakerConfig,
    CopilotConfig,
    ProxyConfig,
    QuotaGuardConfig,
    TierConfig,
    ZhipuConfig,
)
from ..logging.db import TokenLogger
from ..routing.circuit_breaker import CircuitBreaker
from ..routing.model_mapper import ModelMapper
from ..routing.quota_guard import QuotaGuard
from ..routing.router import RequestRouter
from ..routing.tier import BackendTier
from .request_normalizer import normalize_anthropic_request

logger = logging.getLogger(__name__)


def _find_anthropic_backend(router: RequestRouter) -> AnthropicBackend | None:
    """从路由链中查找 Anthropic 后端实例（用于旁路透传）."""
    for tier in router.tiers:
        if isinstance(tier.backend, AnthropicBackend):
            return tier.backend
    return None


def _find_copilot_backend(router: RequestRouter) -> CopilotBackend | None:
    """从路由链中查找 Copilot 后端实例（用于诊断与模型探测）."""
    for tier in router.tiers:
        if isinstance(tier.backend, CopilotBackend):
            return tier.backend
    return None


def _build_circuit_breaker(cfg: CircuitBreakerConfig) -> CircuitBreaker:
    """从配置构建熔断器实例."""
    return CircuitBreaker(
        failure_threshold=cfg.failure_threshold,
        recovery_timeout_seconds=cfg.recovery_timeout_seconds,
        success_threshold=cfg.success_threshold,
        max_recovery_seconds=cfg.max_recovery_seconds,
    )


def _build_quota_guard(cfg: QuotaGuardConfig) -> QuotaGuard:
    """从配置构建配额守卫实例."""
    return QuotaGuard(
        enabled=cfg.enabled,
        token_budget=cfg.token_budget,
        window_seconds=int(cfg.window_hours * 3600),
        threshold_percent=cfg.threshold_percent,
        probe_interval_seconds=cfg.probe_interval_seconds,
    )


def _create_backend_from_tier(
    tier_cfg: TierConfig,
    failover_cfg: Any,
    mapper: Any,
    token_store: TokenStoreManager,
) -> Any:
    """根据 tier_cfg.backend 创建对应后端实例（Strategy + Factory 模式）."""
    match tier_cfg.backend:
        case "anthropic":
            cfg = AnthropicConfig(
                enabled=tier_cfg.enabled,
                base_url=tier_cfg.base_url or "https://api.anthropic.com",
                timeout_ms=tier_cfg.timeout_ms,
            )
            return AnthropicBackend(cfg, failover_cfg)
        case "copilot":
            cfg = CopilotConfig(
                enabled=tier_cfg.enabled,
                github_token=tier_cfg.github_token,
                account_type=tier_cfg.account_type,
                token_url=tier_cfg.token_url,
                base_url=tier_cfg.base_url,
                models_cache_ttl_seconds=tier_cfg.models_cache_ttl_seconds,
                timeout_ms=tier_cfg.timeout_ms,
            )
            cfg = _resolve_copilot_credentials(cfg, token_store)
            return CopilotBackend(cfg, failover_cfg, mapper)
        case "antigravity":
            cfg = AntigravityConfig(
                enabled=tier_cfg.enabled,
                client_id=tier_cfg.client_id,
                client_secret=tier_cfg.client_secret,
                refresh_token=tier_cfg.refresh_token,
                base_url=tier_cfg.base_url or "https://generativelanguage.googleapis.com/v1beta",
                model_endpoint=tier_cfg.model_endpoint,
                timeout_ms=tier_cfg.timeout_ms,
            )
            cfg = _resolve_antigravity_credentials(cfg, token_store)
            return AntigravityBackend(cfg, failover_cfg, mapper)
        case "zhipu":
            cfg = ZhipuConfig(
                enabled=tier_cfg.enabled,
                base_url=tier_cfg.base_url or "https://open.bigmodel.cn/api/anthropic",
                api_key=tier_cfg.api_key,
                timeout_ms=tier_cfg.timeout_ms,
            )
            return ZhipuBackend(cfg, mapper)
        case _:
            raise ValueError(f"未知的 backend 类型: {tier_cfg.backend!r}")


def _resolve_copilot_credentials(
    cfg: CopilotConfig, token_store: TokenStoreManager
) -> CopilotConfig:
    """合并 Copilot 凭证: Token Store > Config YAML.

    返回更新后的 CopilotConfig（github_token 已填充）。
    """
    if cfg.github_token:
        return cfg  # config.yaml 已有凭证，直接使用

    tokens = token_store.get("github")
    if tokens.access_token:
        cfg = cfg.model_copy(update={"github_token": tokens.access_token})
        logger.info("Copilot: 使用 Token Store 中的 GitHub 凭证")

    return cfg


def _resolve_antigravity_credentials(
    cfg: AntigravityConfig, token_store: TokenStoreManager
) -> AntigravityConfig:
    """合并 Antigravity 凭证: Token Store > Config YAML.

    优先使用 Token Store 中的 refresh_token；
    若 config.yaml 已有完整凭证（client_id + client_secret + refresh_token），则直接使用。
    """
    if cfg.refresh_token:
        return cfg  # config.yaml 已有凭证，直接使用

    tokens = token_store.get("google")
    if tokens.refresh_token:
        updates: dict[str, str] = {"refresh_token": tokens.refresh_token}
        # 若 config.yaml 缺少 OAuth 凭据，使用默认公开凭据
        if not cfg.client_id:
            updates["client_id"] = _GOOGLE_DEFAULT_CLIENT_ID
        if not cfg.client_secret:
            updates["client_secret"] = _GOOGLE_DEFAULT_CLIENT_SECRET
        cfg = cfg.model_copy(update=updates)
        logger.info("Antigravity: 使用 Token Store 中的 Google 凭证")
        if tokens.scope and not GoogleOAuthProvider.has_required_scopes(tokens.scope):
            missing = sorted(_GOOGLE_REQUIRED_SCOPE_SET.difference(tokens.scope.split()))
            logger.warning(
                "Antigravity: Token Store 中的 Google scope 不完整，缺少: %s",
                ", ".join(missing),
            )

    return cfg


def _json_error_response(
    status_code: int,
    *,
    error_type: str,
    message: str,
    details: list[str] | None = None,
) -> Response:
    payload: dict[str, Any] = {
        "error": {
            "type": error_type,
            "message": message,
        }
    }
    if details:
        payload["error"]["details"] = details
    return Response(
        content=json.dumps(payload, ensure_ascii=False).encode(),
        status_code=status_code,
        media_type="application/json",
    )


def _stream_error_event(error_type: str, message: str, details: list[str] | None = None) -> bytes:
    payload: dict[str, Any] = {
        "type": "error",
        "error": {
            "type": error_type,
            "message": message,
        },
    }
    if details:
        payload["error"]["details"] = details
    return f"event: error\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n".encode()


def _extract_stream_http_error(exc: httpx.HTTPStatusError) -> tuple[str, str]:
    response = exc.response
    if response is None:
        return "api_error", str(exc)

    try:
        payload = response.json() if response.content else None
    except (json.JSONDecodeError, UnicodeDecodeError, TypeError, ValueError):
        payload = None

    if isinstance(payload, dict):
        error = payload.get("error")
        if isinstance(error, dict):
            error_type = error.get("type")
            message = error.get("message")
            if isinstance(error_type, str) and isinstance(message, str) and message:
                return error_type, message
        message = payload.get("message")
        if isinstance(message, str) and message:
            return "api_error", message

    text = response.text.strip() if response.content else ""
    if text:
        return "api_error", text[:500]
    return "api_error", str(exc)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """应用生命周期管理（启动 / 关闭）."""
    router: RequestRouter = app.state.router
    token_logger: TokenLogger = app.state.token_logger
    compat_session_store: CompatSessionStore = app.state.compat_session_store
    config: ProxyConfig = app.state.config

    await token_logger.init()
    await compat_session_store.init()

    # 从配置加载模型定价表
    from ..pricing import PricingTable
    pricing_table = PricingTable(config.pricing)
    app.state.pricing_table = pricing_table
    router.set_pricing_table(pricing_table)

    # 为每个有 QuotaGuard 的 tier 加载基线
    for tier in router.tiers:
        if tier.quota_guard and tier.quota_guard.enabled:
            total = await token_logger.query_window_total(
                tier.quota_guard.window_hours,
                backend=tier.name,
            )
            tier.quota_guard.load_baseline(total)
        if tier.weekly_quota_guard and tier.weekly_quota_guard.enabled:
            total = await token_logger.query_window_total(
                tier.weekly_quota_guard.window_hours,
                backend=tier.name,
            )
            tier.weekly_quota_guard.load_baseline(total)

    logger.info("coding-proxy started: host=%s port=%d", config.server.host, config.server.port)
    yield
    await router.close()
    await compat_session_store.close()
    await token_logger.close()
    logger.info("coding-proxy stopped")


def create_app(config: ProxyConfig | None = None) -> FastAPI:
    """创建 FastAPI 应用实例."""
    if config is None:
        config = load_config()

    token_logger = TokenLogger(config.db_path)
    compat_session_store = CompatSessionStore(
        config.compat_state_path,
        ttl_seconds=config.database.compat_state_ttl_seconds,
    )
    mapper = ModelMapper(config.model_mapping)

    # 加载 Token Store 用于凭证合并
    token_store = TokenStoreManager(
        store_path=Path(config.auth.token_store_path) if config.auth.token_store_path else None
    )
    token_store.load()

    # 按 config.tiers 列表顺序构建后端层级链（列表顺序即优先级）
    tiers: list[BackendTier] = []
    for tier_cfg in config.tiers:
        if not tier_cfg.enabled:
            continue
        backend = _create_backend_from_tier(tier_cfg, config.failover, mapper, token_store)
        cb = _build_circuit_breaker(tier_cfg.circuit_breaker) if tier_cfg.circuit_breaker else None
        qg = _build_quota_guard(tier_cfg.quota_guard)
        wqg = _build_quota_guard(tier_cfg.weekly_quota_guard)
        tiers.append(BackendTier(backend=backend, circuit_breaker=cb, quota_guard=qg, weekly_quota_guard=wqg))

    # 构建运行时重认证协调器
    reauth_providers: dict[str, Any] = {}
    token_updaters: dict[str, Any] = {}
    for tier in tiers:
        if isinstance(tier.backend, CopilotBackend):
            reauth_providers["github"] = GitHubDeviceFlowProvider()
            token_updaters["github"] = tier.backend._token_manager.update_github_token
        elif isinstance(tier.backend, AntigravityBackend):
            reauth_providers["google"] = GoogleOAuthProvider()
            token_updaters["google"] = tier.backend._token_manager.update_refresh_token

    reauth_coordinator: RuntimeReauthCoordinator | None = None
    if reauth_providers:
        reauth_coordinator = RuntimeReauthCoordinator(
            token_store, reauth_providers, token_updaters,
        )

    router = RequestRouter(tiers, token_logger, reauth_coordinator, compat_session_store)

    app = FastAPI(title="coding-proxy", version="0.1.0", lifespan=lifespan)
    app.state.router = router
    app.state.token_logger = token_logger
    app.state.compat_session_store = compat_session_store
    app.state.config = config
    app.state.reauth_coordinator = reauth_coordinator

    @app.post("/v1/messages")
    async def messages(request: Request) -> Response:
        """Anthropic Messages API 代理端点."""
        body = await request.json()
        headers = dict(request.headers)
        normalization = normalize_anthropic_request(body)
        body = normalization.body
        is_streaming = body.get("stream", False)

        if normalization.adaptations:
            logger.info("Request normalized before routing: %s", ", ".join(normalization.adaptations))

        if is_streaming:
            return StreamingResponse(
                _stream_proxy(router, body, headers),
                media_type="text/event-stream",
                headers={
                    "Cache-Control": "no-cache",
                    "Connection": "keep-alive",
                },
            )

        try:
            resp = await router.route_message(body, headers)
        except NoCompatibleBackendError as exc:
            return _json_error_response(
                400,
                error_type="invalid_request_error",
                message=str(exc),
                details=exc.reasons,
            )
        except TokenAcquireError as exc:
            return _json_error_response(
                503,
                error_type="authentication_error",
                message=str(exc),
            )
        except (httpx.TimeoutException, httpx.ConnectError) as exc:
            return _json_error_response(
                502,
                error_type="api_error",
                message=f"上游不可达: {exc}",
            )
        return Response(
            content=resp.raw_body or b"{}",
            status_code=resp.status_code,
            media_type="application/json",
        )

    @app.get("/health")
    async def health() -> dict:
        return {"status": "ok"}

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
            diagnostics = tier.backend.get_diagnostics()
            if diagnostics:
                info["diagnostics"] = diagnostics
            result["tiers"].append(info)
        return result

    @app.get("/api/copilot/diagnostics")
    async def copilot_diagnostics() -> Response:
        """返回 Copilot 认证与交换链路的脱敏诊断信息."""
        backend = _find_copilot_backend(router)
        if backend is None:
            return _json_error_response(
                404,
                error_type="not_found",
                message="copilot backend not enabled",
            )
        return Response(
            content=json.dumps(backend.get_diagnostics(), ensure_ascii=False).encode(),
            status_code=200,
            media_type="application/json",
        )

    @app.get("/api/copilot/models")
    async def copilot_models() -> Response:
        """按需探测当前 Copilot 会话可见模型列表."""
        backend = _find_copilot_backend(router)
        if backend is None:
            return _json_error_response(
                404,
                error_type="not_found",
                message="copilot backend not enabled",
            )
        try:
            probe = await backend.probe_models()
        except TokenAcquireError as exc:
            return _json_error_response(
                503,
                error_type="authentication_error",
                message=str(exc),
            )
        except (httpx.TimeoutException, httpx.ConnectError) as exc:
            return _json_error_response(
                502,
                error_type="api_error",
                message=f"copilot models probe failed: {exc}",
            )
        return Response(
            content=json.dumps(probe, ensure_ascii=False).encode(),
            status_code=200 if probe.get("probe_status") == "ok" else 502,
            media_type="application/json",
        )

    @app.post("/api/reset")
    async def reset_circuit() -> dict:
        for tier in router.tiers:
            if tier.circuit_breaker:
                tier.circuit_breaker.reset()
            if tier.quota_guard:
                tier.quota_guard.reset()
            if tier.weekly_quota_guard:
                tier.weekly_quota_guard.reset()
            tier.reset_rate_limit()
        return {"status": "ok"}

    # ── 连通性探测 ──────────────────────────────────────────────
    @app.head("/")
    @app.get("/")
    async def root() -> Response:
        """根路径连通性探测 — Claude Code 在建连前发送 HEAD / 作为 health probe."""
        return Response(status_code=200)

    # ── Token 计数透传 ─────────────────────────────────────────
    @app.post("/v1/messages/count_tokens")
    async def count_tokens(request: Request) -> Response:
        """Token 计数 API 透传 — 旁路直通 Anthropic，不经过路由链.

        仅当 Anthropic 主后端启用时可用；其他后端不支持此协议。
        """
        anthropic_backend = _find_anthropic_backend(router)
        if anthropic_backend is None:
            return Response(
                content=b'{"error":{"type":"not_found","message":"count_tokens requires anthropic backend"}}',
                status_code=404,
                media_type="application/json",
            )

        body = await request.json()
        headers = dict(request.headers)
        prepared_body, prepared_headers = await anthropic_backend._prepare_request(body, headers)

        client = anthropic_backend._get_client()
        url = "/v1/messages/count_tokens"
        if request.query_params:
            url = f"{url}?{request.query_params}"

        try:
            response = await client.post(url, json=prepared_body, headers=prepared_headers)
            return Response(
                content=response.content,
                status_code=response.status_code,
                media_type="application/json",
            )
        except (httpx.TimeoutException, httpx.ConnectError) as exc:
            logger.warning("count_tokens proxy failed: %s", exc)
            return Response(
                content=b'{"error":{"type":"api_error","message":"count_tokens upstream unreachable"}}',
                status_code=502,
                media_type="application/json",
            )

    # ── 重认证 API ─────────────────────────────────────────────
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

    return app


async def _stream_proxy(router: RequestRouter, body: dict, headers: dict) -> Any:
    """流式代理生成器."""
    try:
        async for chunk, backend_name in router.route_stream(body, headers):
            yield chunk
    except NoCompatibleBackendError as exc:
        yield (
            "event: error\n"
            f"data: {json.dumps({'type': 'error', 'error': {'type': 'invalid_request_error', 'message': str(exc), 'details': exc.reasons}}, ensure_ascii=False)}\n\n"
        ).encode()
    except TokenAcquireError as exc:
        yield (
            "event: error\n"
            f"data: {json.dumps({'type': 'error', 'error': {'type': 'authentication_error', 'message': str(exc)}}, ensure_ascii=False)}\n\n"
        ).encode()
    except (httpx.TimeoutException, httpx.ConnectError) as exc:
        yield _stream_error_event("api_error", f"上游不可达: {exc}")
    except httpx.HTTPStatusError as exc:
        error_type, message = _extract_stream_http_error(exc)
        yield _stream_error_event(error_type, message)
