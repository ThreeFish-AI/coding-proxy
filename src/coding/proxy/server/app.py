"""FastAPI 应用."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, Request, Response
from fastapi.responses import StreamingResponse

from ..backends.anthropic import AnthropicBackend
from ..backends.zhipu import ZhipuBackend
from ..config.loader import load_config
from ..config.schema import ProxyConfig
from ..logging.db import TokenLogger
from ..routing.circuit_breaker import CircuitBreaker
from ..routing.model_mapper import ModelMapper
from ..routing.router import RequestRouter

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """应用生命周期管理（启动 / 关闭）."""
    router = app.state.router
    token_logger = app.state.token_logger
    config = app.state.config
    # Startup
    await token_logger.init()
    logger.info("coding-proxy started: host=%s port=%d", config.server.host, config.server.port)
    yield
    # Shutdown
    await router.close()
    await token_logger.close()
    logger.info("coding-proxy stopped")


def create_app(config: ProxyConfig | None = None) -> FastAPI:
    """创建 FastAPI 应用实例."""
    if config is None:
        config = load_config()

    # 初始化 Token 日志
    token_logger = TokenLogger(config.db_path)

    # 初始化模型映射器
    mapper = ModelMapper(config.model_mapping)

    # 初始化后端
    primary = AnthropicBackend(config.primary, config.failover)
    fallback = ZhipuBackend(config.fallback, config.failover, mapper)

    # 初始化熔断器
    cb = CircuitBreaker(
        failure_threshold=config.circuit_breaker.failure_threshold,
        recovery_timeout_seconds=config.circuit_breaker.recovery_timeout_seconds,
        success_threshold=config.circuit_breaker.success_threshold,
        max_recovery_seconds=config.circuit_breaker.max_recovery_seconds,
    )

    # 初始化路由器
    router = RequestRouter(primary, fallback, cb, token_logger)

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
        info = router.circuit.get_info()
        return {
            "circuit_breaker": info,
            "primary": primary.get_name(),
            "fallback": fallback.get_name(),
        }

    @app.post("/api/reset")
    async def reset_circuit() -> dict:
        cb.reset()
        return {"status": "ok", "circuit_breaker": cb.get_info()}

    return app


async def _stream_proxy(router: RequestRouter, body: dict, headers: dict) -> Any:
    """流式代理生成器."""
    async for chunk, backend_name in router.route_stream(body, headers):
        yield chunk
