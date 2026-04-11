"""FastAPI 应用工厂函数 — 供应商实例化与凭证解析."""

from __future__ import annotations

import logging
from typing import Any

from ..auth.providers.google import (
    _DEFAULT_CLIENT_ID as _GOOGLE_DEFAULT_CLIENT_ID,
)
from ..auth.providers.google import (
    _DEFAULT_CLIENT_SECRET as _GOOGLE_DEFAULT_CLIENT_SECRET,
)
from ..auth.providers.google import (
    _REQUIRED_SCOPE_SET as _GOOGLE_REQUIRED_SCOPE_SET,
)
from ..auth.providers.google import (
    GoogleOAuthProvider,
)
from ..auth.store import TokenStoreManager
from ..config.schema import (
    AlibabaConfig,
    AnthropicConfig,
    AntigravityConfig,
    CircuitBreakerConfig,
    CopilotConfig,
    DoubaoConfig,
    FailoverConfig,
    KimiConfig,
    MinimaxConfig,
    QuotaGuardConfig,
    TierConfig,
    XiaomiConfig,
    ZhipuConfig,
)
from ..routing.circuit_breaker import CircuitBreaker
from ..routing.model_mapper import ModelMapper
from ..routing.quota_guard import QuotaGuard
from ..routing.tier import VendorTier
from ..vendors.alibaba import AlibabaVendor
from ..vendors.anthropic import AnthropicVendor
from ..vendors.antigravity import AntigravityVendor
from ..vendors.base import BaseVendor
from ..vendors.copilot import CopilotVendor
from ..vendors.doubao import DoubaoVendor
from ..vendors.kimi import KimiVendor
from ..vendors.minimax import MinimaxVendor
from ..vendors.xiaomi import XiaomiVendor
from ..vendors.zhipu import ZhipuVendor

# 向后兼容别名
BackendTier = VendorTier  # noqa: F401  (deprecated)

logger = logging.getLogger(__name__)


def _find_anthropic_vendor(router: Any) -> AnthropicVendor | None:
    """从路由链中查找 Anthropic 供应商实例（用于旁路透传）."""
    for tier in router.tiers:
        if isinstance(tier.vendor, AnthropicVendor):
            return tier.vendor
    return None


def _find_count_tokens_vendor(router: Any) -> BaseVendor | None:
    """查找当前实际在用的供应商（通过全局活跃状态）.

    读取 Executor 在成功响应时写入的活跃供应商名称，
    按名称匹配返回对应的 vendor 对象。
    无活跃记录时回退到 tiers[0]（冷启动场景）。
    """
    if not router.tiers:
        return None

    # 优先使用全局活跃状态
    active_name = router.active_vendor_name
    if active_name:
        for tier in router.tiers:
            if tier.name == active_name:
                return tier.vendor

    # 冷启动（无任何成功请求）：回退到首个供应商
    return router.tiers[0].vendor


def _find_copilot_vendor(router: Any) -> CopilotVendor | None:
    """从路由链中查找 Copilot 供应商实例（用于诊断与模型探测）."""
    for tier in router.tiers:
        if isinstance(tier.vendor, CopilotVendor):
            return tier.vendor
    return None


def _build_circuit_breaker(
    cfg: CircuitBreakerConfig, *, vendor_name: str = ""
) -> CircuitBreaker:
    """从配置构建熔断器实例."""
    return CircuitBreaker(
        failure_threshold=cfg.failure_threshold,
        recovery_timeout_seconds=cfg.recovery_timeout_seconds,
        success_threshold=cfg.success_threshold,
        max_recovery_seconds=cfg.max_recovery_seconds,
        vendor_name=vendor_name,
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


def _create_vendor_from_config(
    vendor_cfg: TierConfig,
    failover_cfg: FailoverConfig,
    mapper: ModelMapper,
    token_store: TokenStoreManager,
) -> Any:
    """根据 vendor_cfg.vendor 创建对应供应商实例（Strategy + Factory 模式）."""
    match vendor_cfg.vendor:
        case "anthropic":
            cfg = AnthropicConfig(
                enabled=vendor_cfg.enabled,
                base_url=vendor_cfg.base_url or "https://api.anthropic.com",
                timeout_ms=vendor_cfg.timeout_ms,
            )
            return AnthropicVendor(cfg, failover_cfg)
        case "copilot":
            cfg = CopilotConfig(
                enabled=vendor_cfg.enabled,
                github_token=vendor_cfg.github_token,
                account_type=vendor_cfg.account_type,
                token_url=vendor_cfg.token_url,
                base_url=vendor_cfg.base_url,
                models_cache_ttl_seconds=vendor_cfg.models_cache_ttl_seconds,
                timeout_ms=vendor_cfg.timeout_ms,
            )
            cfg = _resolve_copilot_credentials(cfg, token_store)
            return CopilotVendor(cfg, failover_cfg, mapper)
        case "antigravity":
            cfg = AntigravityConfig(
                enabled=vendor_cfg.enabled,
                client_id=vendor_cfg.client_id,
                client_secret=vendor_cfg.client_secret,
                refresh_token=vendor_cfg.refresh_token,
                base_url=vendor_cfg.base_url
                or "https://generativelanguage.googleapis.com/v1beta",
                model_endpoint=vendor_cfg.model_endpoint,
                timeout_ms=vendor_cfg.timeout_ms,
            )
            cfg = _resolve_antigravity_credentials(cfg, token_store)
            return AntigravityVendor(cfg, failover_cfg, mapper)
        case "zhipu":
            cfg = ZhipuConfig(
                enabled=vendor_cfg.enabled,
                base_url=vendor_cfg.base_url
                or "https://open.bigmodel.cn/api/anthropic",
                api_key=vendor_cfg.api_key,
                timeout_ms=vendor_cfg.timeout_ms,
            )
            return ZhipuVendor(cfg, mapper, failover_cfg)
        case "minimax":
            cfg = MinimaxConfig(
                enabled=vendor_cfg.enabled,
                base_url=vendor_cfg.base_url or "https://api.minimaxi.com/anthropic",
                api_key=vendor_cfg.api_key,
                timeout_ms=vendor_cfg.timeout_ms,
            )
            return MinimaxVendor(cfg, mapper, failover_cfg)
        case "kimi":
            cfg = KimiConfig(
                enabled=vendor_cfg.enabled,
                base_url=vendor_cfg.base_url or "https://api.kimi.com/coding/",
                api_key=vendor_cfg.api_key,
                timeout_ms=vendor_cfg.timeout_ms,
            )
            return KimiVendor(cfg, mapper, failover_cfg)
        case "doubao":
            cfg = DoubaoConfig(
                enabled=vendor_cfg.enabled,
                base_url=vendor_cfg.base_url
                or "https://ark.cn-beijing.volces.com/api/coding",
                api_key=vendor_cfg.api_key,
                timeout_ms=vendor_cfg.timeout_ms,
            )
            return DoubaoVendor(cfg, mapper, failover_cfg)
        case "xiaomi":
            cfg = XiaomiConfig(
                enabled=vendor_cfg.enabled,
                base_url=vendor_cfg.base_url
                or "https://token-plan-cn.xiaomimimo.com/anthropic",
                api_key=vendor_cfg.api_key,
                timeout_ms=vendor_cfg.timeout_ms,
            )
            return XiaomiVendor(cfg, mapper, failover_cfg)
        case "alibaba":
            cfg = AlibabaConfig(
                enabled=vendor_cfg.enabled,
                base_url=vendor_cfg.base_url
                or "https://coding-intl.dashscope.aliyuncs.com/apps/anthropic",
                api_key=vendor_cfg.api_key,
                timeout_ms=vendor_cfg.timeout_ms,
            )
            return AlibabaVendor(cfg, mapper, failover_cfg)
        case _:
            raise ValueError(f"未知的 vendor 类型: {vendor_cfg.vendor!r}")


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
            missing = sorted(
                _GOOGLE_REQUIRED_SCOPE_SET.difference(tokens.scope.split())
            )
            logger.warning(
                "Antigravity: Token Store 中的 Google scope 不完整，缺少: %s",
                ", ".join(missing),
            )

    return cfg


# ── 向后兼容别名 (deprecated) ──────────────────────────────

_find_anthropic_backend = _find_anthropic_vendor  # noqa: F401
_find_copilot_backend = _find_copilot_vendor  # noqa: F401
_create_backend_from_tier = _create_vendor_from_config  # noqa: F401
