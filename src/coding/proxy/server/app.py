"""FastAPI 应用工厂与生命周期管理.

路由端点注册已正交分解至 :mod:`.routes`.
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI

from ..auth.providers.github import GitHubDeviceFlowProvider
from ..auth.providers.google import GoogleOAuthProvider
from ..auth.runtime import RuntimeReauthCoordinator
from ..auth.store import TokenStoreManager
from ..backends.antigravity import AntigravityBackend
from ..backends.copilot import CopilotBackend
from ..config.loader import load_config
from ..compat.session_store import CompatSessionStore
from ..config.schema import ProxyConfig
from ..logging.db import TokenLogger
from ..routing.router import RequestRouter
from ..routing.tier import BackendTier
from .factory import (  # noqa: F401
    _build_circuit_breaker,
    _build_quota_guard,
    _create_backend_from_tier,
)
from .routes import register_all_routes

logger = logging.getLogger(__name__)


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
    from ..pricing import PricingTable  # noqa: F401

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
    from ..routing.model_mapper import ModelMapper  # noqa: E402

    mapper = ModelMapper(config.model_mapping)

    # 加载 Token Store 用于凭证合并
    token_store = TokenStoreManager(
        store_path=Path(config.auth.token_store_path) if config.auth.token_store_path else None
    )
    token_store.load()

    # 按 config.tiers 列表顺序构建后端层级链（列表顺序即优先级）
    tiers: list[Any] = []
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
        reauth_coordinator = RuntimeReauthCoordinator(token_store, reauth_providers, token_updaters)

    router = RequestRouter(tiers, token_logger, reauth_coordinator, compat_session_store)

    app = FastAPI(title="coding-proxy", version="0.1.0", lifespan=lifespan)
    app.state.router = router
    app.state.token_logger = token_logger
    app.state.compat_session_store = compat_session_store
    app.state.config = config
    app.state.reauth_coordinator = reauth_coordinator

    # 注册所有路由端点
    register_all_routes(app, router, reauth_coordinator)

    return app
