"""FastAPI 应用工厂函数 — 后端实例化与凭证解析."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from ..auth.providers.google import (
    GoogleOAuthProvider,
    _DEFAULT_CLIENT_ID as _GOOGLE_DEFAULT_CLIENT_ID,
    _DEFAULT_CLIENT_SECRET as _GOOGLE_DEFAULT_CLIENT_SECRET,
    _REQUIRED_SCOPE_SET as _GOOGLE_REQUIRED_SCOPE_SET,
)
from ..auth.runtime import RuntimeReauthCoordinator
from ..auth.store import TokenStoreManager
from ..backends.antigravity import AntigravityBackend
from ..backends.anthropic import AnthropicBackend
from ..backends.copilot import CopilotBackend
from ..backends.zhipu import ZhipuBackend
from ..config.schema import (
    AntigravityConfig,
    AnthropicConfig,
    CircuitBreakerConfig,
    CopilotConfig,
    FailoverConfig,
    QuotaGuardConfig,
    TierConfig,
    ZhipuConfig,
)
from ..routing.circuit_breaker import CircuitBreaker
from ..routing.model_mapper import ModelMapper
from ..routing.quota_guard import QuotaGuard
from ..routing.tier import BackendTier

logger = logging.getLogger(__name__)


def _find_anthropic_backend(router: Any) -> AnthropicBackend | None:
    """从路由链中查找 Anthropic 后端实例（用于旁路透传）."""
    for tier in router.tiers:
        if isinstance(tier.backend, AnthropicBackend):
            return tier.backend
    return None


def _find_copilot_backend(router: Any) -> CopilotBackend | None:
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
    failover_cfg: FailoverConfig,
    mapper: ModelMapper,
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


def _resolve_copilot_credentials(cfg: CopilotConfig, token_store: TokenStoreManager) -> CopilotConfig:
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


def _resolve_antigravity_credentials(cfg: AntigravityConfig, token_store: TokenStoreManager) -> AntigravityConfig:
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
            logger.warning("Antigravity: Token Store 中的 Google scope 不完整，缺少: %s", ", ".join(missing))

    return cfg
