"""FastAPI 应用工厂与生命周期管理.

路由端点注册已正交分解至 :mod:`.routes`.
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI

from .. import __version__
from ..auth.providers.github import GitHubDeviceFlowProvider
from ..auth.providers.google import GoogleOAuthProvider
from ..auth.runtime import RuntimeReauthCoordinator
from ..auth.store import TokenStoreManager
from ..compat.session_store import CompatSessionStore
from ..config.loader import load_config
from ..config.schema import ProxyConfig
from ..logging.db import TokenLogger
from ..native_api import NativeProxyHandler
from ..routing.router import RequestRouter
from ..routing.session_policy import SessionPolicyResolver
from ..routing.tier import VendorTier
from ..routing.usage_recorder import UsageRecorder
from ..vendors.antigravity import AntigravityVendor
from ..vendors.copilot import CopilotVendor
from .factory import (  # noqa: F401
    _build_circuit_breaker,
    _build_quota_guard,
    _create_vendor_from_config,
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

    # 原生 API 透传 handler：运行时注入 pricing_table（启动期创建时尚未就绪）
    native_handler: NativeProxyHandler | None = getattr(
        app.state, "native_handler", None
    )
    if native_handler is not None:
        native_handler._pricing_table = pricing_table  # noqa: SLF001
        if native_handler._usage_recorder is not None:  # noqa: SLF001
            native_handler._usage_recorder.set_pricing_table(pricing_table)  # noqa: SLF001

    # 为每个有 QuotaGuard 的 tier 加载基线
    for tier in router.tiers:
        if tier.quota_guard and tier.quota_guard.enabled:
            total = await token_logger.query_window_total(
                tier.quota_guard.window_hours,
                vendor=tier.name,
            )
            tier.quota_guard.load_baseline(total, vendor=tier.name)
        if tier.weekly_quota_guard and tier.weekly_quota_guard.enabled:
            total = await token_logger.query_window_total(
                tier.weekly_quota_guard.window_hours,
                vendor=tier.name,
            )
            tier.weekly_quota_guard.load_baseline(total, vendor=tier.name)

    yield
    await router.close()
    if native_handler is not None:
        await native_handler.aclose()
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
        store_path=Path(config.auth.token_store_path)
        if config.auth.token_store_path
        else None
    )
    token_store.load()

    # 阶段一：构建 vendor_name → VendorTier 映射表（与顺序无关）
    _vendor_map: dict[str, Any] = {}
    for vendor_cfg in config.vendors:
        if not vendor_cfg.enabled:
            continue
        vendor = _create_vendor_from_config(
            vendor_cfg, config.failover, mapper, token_store
        )
        cb = (
            _build_circuit_breaker(
                vendor_cfg.circuit_breaker, vendor_name=vendor_cfg.vendor
            )
            if vendor_cfg.circuit_breaker
            else None
        )
        qg = _build_quota_guard(vendor_cfg.quota_guard)
        wqg = _build_quota_guard(vendor_cfg.weekly_quota_guard)
        _vendor_map[vendor_cfg.vendor] = VendorTier(
            vendor=vendor, circuit_breaker=cb, quota_guard=qg, weekly_quota_guard=wqg
        )

    # 阶段二：按 tiers 指定的顺序组装最终链路（或回退到 vendors 原始顺序）
    if config.tiers is not None:
        tiers = [_vendor_map[name] for name in config.tiers if name in _vendor_map]
    else:
        tiers = [_vendor_map[v.vendor] for v in config.vendors if v.enabled]

    # 构建运行时重认证协调器
    reauth_providers: dict[str, Any] = {}
    token_updaters: dict[str, Any] = {}
    for tier in tiers:
        if isinstance(tier.vendor, CopilotVendor):
            reauth_providers["github"] = GitHubDeviceFlowProvider()
            token_updaters["github"] = tier.vendor._token_manager.update_github_token
        elif isinstance(tier.vendor, AntigravityVendor):
            reauth_providers["google"] = GoogleOAuthProvider()
            token_updaters["google"] = tier.vendor._token_manager.update_refresh_token

    reauth_coordinator: RuntimeReauthCoordinator | None = None
    if reauth_providers:
        reauth_coordinator = RuntimeReauthCoordinator(
            token_store, reauth_providers, token_updaters
        )

    router = RequestRouter(
        tiers,
        token_logger,
        reauth_coordinator,
        compat_session_store,
        session_policy_resolver=SessionPolicyResolver(config.session_policies.policies),
    )

    app = FastAPI(title="coding-proxy", version=__version__, lifespan=lifespan)
    app.state.router = router
    app.state.token_logger = token_logger
    app.state.compat_session_store = compat_session_store
    app.state.config = config
    app.state.reauth_coordinator = reauth_coordinator

    # 原生 API 透传 handler — 仅在配置中至少启用一个 provider 时实例化
    if any(config.native_api.is_enabled(p) for p in ("openai", "gemini", "anthropic")):
        native_usage_recorder = UsageRecorder(
            token_logger=token_logger, pricing_table=None
        )
        native_handler = NativeProxyHandler(
            config.native_api,
            token_logger=token_logger,
            pricing_table=None,
            usage_recorder=native_usage_recorder,
        )
        app.state.native_handler = native_handler

    # 注册所有路由端点
    register_all_routes(app, router, reauth_coordinator)

    return app
