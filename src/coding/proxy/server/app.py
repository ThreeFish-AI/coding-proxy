"""FastAPI 应用."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import httpx
from fastapi import FastAPI, Request, Response
from fastapi.responses import StreamingResponse

from ..auth.store import TokenStoreManager
from ..backends.antigravity import AntigravityBackend
from ..backends.anthropic import AnthropicBackend
from ..backends.copilot import CopilotBackend
from ..backends.zhipu import ZhipuBackend
from ..config.loader import load_config
from ..config.schema import (
    AntigravityConfig,
    CircuitBreakerConfig,
    CopilotConfig,
    ProxyConfig,
    QuotaGuardConfig,
)
from ..logging.db import TokenLogger
from ..routing.circuit_breaker import CircuitBreaker
from ..routing.model_mapper import ModelMapper
from ..routing.quota_guard import QuotaGuard
from ..routing.router import RequestRouter
from ..routing.tier import BackendTier

logger = logging.getLogger(__name__)


def _find_anthropic_backend(router: RequestRouter) -> AnthropicBackend | None:
    """从路由链中查找 Anthropic 后端实例（用于旁路透传）."""
    for tier in router.tiers:
        if isinstance(tier.backend, AnthropicBackend):
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
            updates["client_id"] = "764086051850-6qr4p6gpi6hn506pt8ejuq83di341hur.apps.googleusercontent.com"
        if not cfg.client_secret:
            updates["client_secret"] = "d-FL95Q19W7jAaasCmO6F9XZ"
        cfg = cfg.model_copy(update=updates)
        logger.info("Antigravity: 使用 Token Store 中的 Google 凭证")

    return cfg


@asynccontextmanager
async def lifespan(app: FastAPI):
    """应用生命周期管理（启动 / 关闭）."""
    router: RequestRouter = app.state.router
    token_logger: TokenLogger = app.state.token_logger
    config: ProxyConfig = app.state.config

    await token_logger.init()

    # 为每个有 QuotaGuard 的 tier 加载基线
    for tier in router.tiers:
        if tier.quota_guard and tier.quota_guard.enabled:
            total = await token_logger.query_window_total(
                tier.quota_guard.window_hours,
                backend=tier.name,
            )
            tier.quota_guard.load_baseline(total)

    logger.info("coding-proxy started: host=%s port=%d", config.server.host, config.server.port)
    yield
    await router.close()
    await token_logger.close()
    logger.info("coding-proxy stopped")


def create_app(config: ProxyConfig | None = None) -> FastAPI:
    """创建 FastAPI 应用实例."""
    if config is None:
        config = load_config()

    token_logger = TokenLogger(config.db_path)
    mapper = ModelMapper(config.model_mapping)

    # 加载 Token Store 用于凭证合并
    token_store = TokenStoreManager(
        store_path=Path(config.auth.token_store_path) if config.auth.token_store_path else None
    )
    token_store.load()

    # 构建后端层级链
    tiers: list[BackendTier] = []

    # Tier 0: Anthropic (主后端)
    if config.primary.enabled:
        tiers.append(BackendTier(
            backend=AnthropicBackend(config.primary, config.failover),
            circuit_breaker=_build_circuit_breaker(config.circuit_breaker),
            quota_guard=_build_quota_guard(config.quota_guard),
        ))

    # Tier 1: GitHub Copilot (中间层)
    if config.copilot.enabled:
        copilot_cfg = _resolve_copilot_credentials(config.copilot, token_store)
        tiers.append(BackendTier(
            backend=CopilotBackend(copilot_cfg, config.failover),
            circuit_breaker=_build_circuit_breaker(config.copilot_circuit_breaker),
            quota_guard=_build_quota_guard(config.copilot_quota_guard),
        ))

    # Tier 2: Google Antigravity Claude (中间层)
    if config.antigravity.enabled:
        antigravity_cfg = _resolve_antigravity_credentials(config.antigravity, token_store)
        tiers.append(BackendTier(
            backend=AntigravityBackend(antigravity_cfg, config.failover),
            circuit_breaker=_build_circuit_breaker(config.antigravity_circuit_breaker),
            quota_guard=_build_quota_guard(config.antigravity_quota_guard),
        ))

    # Tier N: Zhipu (终端 fallback，无熔断器/配额守卫)
    if config.fallback.enabled:
        tiers.append(BackendTier(
            backend=ZhipuBackend(config.fallback, mapper),
        ))

    router = RequestRouter(tiers, token_logger)

    app = FastAPI(title="coding-proxy", version="0.1.0", lifespan=lifespan)
    app.state.router = router
    app.state.token_logger = token_logger
    app.state.config = config

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
                headers={
                    "Cache-Control": "no-cache",
                    "Connection": "keep-alive",
                },
            )

        resp = await router.route_message(body, headers)
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
            result["tiers"].append(info)
        return result

    @app.post("/api/reset")
    async def reset_circuit() -> dict:
        for tier in router.tiers:
            if tier.circuit_breaker:
                tier.circuit_breaker.reset()
            if tier.quota_guard:
                tier.quota_guard.reset()
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

    return app


async def _stream_proxy(router: RequestRouter, body: dict, headers: dict) -> Any:
    """流式代理生成器."""
    async for chunk, backend_name in router.route_stream(body, headers):
        yield chunk
