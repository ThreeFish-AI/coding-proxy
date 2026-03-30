"""FastAPI 应用."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, Request, Response
from fastapi.responses import StreamingResponse

from ..backends.anthropic import AnthropicBackend
from ..backends.copilot import CopilotBackend
from ..backends.zhipu import ZhipuBackend
from ..config.loader import load_config
from ..config.schema import (
    CircuitBreakerConfig,
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
        tiers.append(BackendTier(
            backend=CopilotBackend(config.copilot, config.failover),
            circuit_breaker=_build_circuit_breaker(config.copilot_circuit_breaker),
            quota_guard=_build_quota_guard(config.copilot_quota_guard),
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

    return app


async def _stream_proxy(router: RequestRouter, body: dict, headers: dict) -> Any:
    """流式代理生成器."""
    async for chunk, backend_name in router.route_stream(body, headers):
        yield chunk
