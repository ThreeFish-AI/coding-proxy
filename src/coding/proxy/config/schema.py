"""Pydantic 配置模型 — 聚合层（re-export 所有子模块符号）.

本文件为向后兼容的聚合入口点，所有配置模型已正交拆分至以下子模块：

- :mod:`.server`        – ServerConfig / DatabaseConfig / LoggingConfig
- :mod:`.backends`       – AnthropicConfig / CopilotConfig / AntigravityConfig / ZhipuConfig
- :mod:`.resiliency`     – CircuitBreakerConfig / RetryConfig / FailoverConfig / QuotaGuardConfig
- :mod:`.routing`        – BackendType / TierConfig / ModelMappingRule / ModelPricingEntry
- :mod:`.auth_schema`    – AuthConfig

:py:class:`ProxyConfig` 及其旧格式迁移逻辑保留在此文件中。
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field, model_validator

# ── 子模块 re-export ────────────────────────────────────────────

from .server import ServerConfig, DatabaseConfig, LoggingConfig  # noqa: F401
from .backends import (                                 # noqa: F401
    AnthropicConfig,
    CopilotConfig,
    AntigravityConfig,
    ZhipuConfig,
)
from .resiliency import (                              # noqa: F401
    CircuitBreakerConfig,
    RetryConfig,
    FailoverConfig,
    QuotaGuardConfig,
)
from .routing import (                                  # noqa: F401
    BackendType,
    TierConfig,
    ModelMappingRule,
    ModelPricingEntry,
    _COPILOT_FIELDS,
    _ANTIGRAVITY_FIELDS,
    _ZHIPU_FIELDS,
    _BACKEND_EXCLUSIVE_FIELDS,
)
from .auth_schema import AuthConfig                     # noqa: F401

logger = logging.getLogger(__name__)


class ProxyConfig(BaseModel):
    """顶层配置模型.

    .. note::
        以下字段为 **旧 flat 格式**（已废弃，保留仅用于向后兼容迁移）：
        ``primary``, ``copilot``, ``antigravity``, ``fallback``,
        ``circuit_breaker``, ``copilot_circuit_breaker``, ``antigravity_circuit_breaker``,
        ``quota_guard``, ``copilot_quota_guard``, ``antigravity_quota_guard``

        新配置应使用 ``tiers`` 列表格式（参见 config.example.yaml）。
        旧格式会在 ``_migrate_legacy_fields`` 中自动转换为 tiers。
    """

    server: ServerConfig = ServerConfig()

    # ── Legacy 字段（旧 flat 格式，由 _migrate_legacy_fields 自动迁移至 tiers） ──
    primary: AnthropicConfig = Field(
        default=AnthropicConfig(),
        description="[legacy] Anthropic 主后端配置；新格式请使用 tiers[].backend=anthropic",
    )
    copilot: CopilotConfig = Field(
        default=CopilotConfig(),
        description="[legacy] Copilot 后端配置；新格式请使用 tiers[].backend=copilot",
    )
    antigravity: AntigravityConfig = Field(
        default=AntigravityConfig(),
        description="[legacy] Antigravity 后端配置；新格式请使用 tiers[].backend=antigravity",
    )
    fallback: ZhipuConfig = Field(
        default=ZhipuConfig(),
        description="[legacy] 智谱兜底后端配置；新格式请使用 tiers[].backend=zhipu",
    )
    circuit_breaker: CircuitBreakerConfig = Field(
        default=CircuitBreakerConfig(),
        description="[legacy] 全局熔断器配置；新格式请使用 tiers[].circuit_breaker",
    )
    copilot_circuit_breaker: CircuitBreakerConfig = Field(
        default=CircuitBreakerConfig(),
        description="[legacy] Copilot 熔断器配置；新格式请使用 tiers[backend=copilot].circuit_breaker",
    )
    antigravity_circuit_breaker: CircuitBreakerConfig = Field(
        default=CircuitBreakerConfig(),
        description="[legacy] Antigravity 熔断器配置；新格式请使用 tiers[backend=antigravity].circuit_breaker",
    )
    failover: FailoverConfig = FailoverConfig()
    model_mapping: list[ModelMappingRule] = Field(
        default=[
            ModelMappingRule(pattern="claude-sonnet-.*", target="glm-5.1", is_regex=True),
            ModelMappingRule(pattern="claude-opus-.*", target="glm-5.1", is_regex=True),
            ModelMappingRule(pattern="claude-haiku-.*", target="glm-4.5-air", is_regex=True),
            ModelMappingRule(pattern="claude-.*", target="glm-5.1", is_regex=True),
        ],
    )
    quota_guard: QuotaGuardConfig = Field(
        default=QuotaGuardConfig(),
        description="[legacy] 全局配额守卫；新格式请使用 tiers[].quota_guard",
    )
    copilot_quota_guard: QuotaGuardConfig = Field(
        default=QuotaGuardConfig(),
        description="[legacy] Copilot 配额守卫；新格式请使用 tiers[backend=copilot].quota_guard",
    )
    antigravity_quota_guard: QuotaGuardConfig = Field(
        default=QuotaGuardConfig(),
        description="[legacy] Antigravity 配额守卫；新格式请使用 tiers[backend=antigravity].quota_guard",
    )
    auth: AuthConfig = AuthConfig()
    database: DatabaseConfig = DatabaseConfig()
    logging: LoggingConfig = LoggingConfig()
    # 模型定价（USD / 1M tokens），按 (backend, model) 匹配
    pricing: list[ModelPricingEntry] = Field(default_factory=list)
    # 新格式：tiers 列表，列表顺序即优先级
    tiers: list[TierConfig] = Field(default_factory=list)

    @model_validator(mode="before")
    @classmethod
    def _migrate_legacy_fields(cls, data: Any) -> Any:
        """向后兼容迁移（legacy flat 格式 → tiers 列表格式）.

        迁移规则：
        1. ``anthropic`` / ``zhipu`` 字段名自动映射为 ``primary`` / ``fallback``
        2. 若配置中未显式指定 ``tiers``，则从旧 flat 格式字段自动生成
        """
        if not isinstance(data, dict):
            return data

        # 1. 字段别名迁移
        if "anthropic" in data and "primary" not in data:
            data["primary"] = data.pop("anthropic")
        if "zhipu" in data and "fallback" not in data:
            data["fallback"] = data.pop("zhipu")

        # 2. 若已有 tiers 配置则直接使用，跳过自动迁移
        if data.get("tiers"):
            return data

        # 3. 从旧 flat 格式自动构建 tiers（触发时记录废弃日志）
        tiers: list[dict[str, Any]] = []
        _legacy_keys = {"primary", "copilot", "antigravity", "fallback",
                        "circuit_breaker", "copilot_circuit_breaker", "antigravity_circuit_breaker",
                        "quota_guard", "copilot_quota_guard", "antigravity_quota_guard"}
        if any(k in data for k in _legacy_keys):
            logger.info(
                "检测到旧 flat 格式配置字段，已自动迁移至 tiers 列表格式。"
                "建议迁移至 config.example.yaml 中的 tiers 新格式。",
            )

        primary = data.get("primary") or {}
        if primary.get("enabled", True):
            tier: dict[str, Any] = {"backend": "anthropic", **primary}
            cb = data.get("circuit_breaker")
            if cb:
                tier["circuit_breaker"] = cb
            qg = data.get("quota_guard")
            if qg:
                tier["quota_guard"] = qg
            tiers.append(tier)

        copilot = data.get("copilot") or {}
        if copilot.get("enabled", False):
            tier = {"backend": "copilot", **copilot}
            cb = data.get("copilot_circuit_breaker")
            if cb:
                tier["circuit_breaker"] = cb
            qg = data.get("copilot_quota_guard")
            if qg:
                tier["quota_guard"] = qg
            tiers.append(tier)

        antigravity = data.get("antigravity") or {}
        if antigravity.get("enabled", False):
            tier = {"backend": "antigravity", **antigravity}
            cb = data.get("antigravity_circuit_breaker")
            if cb:
                tier["circuit_breaker"] = cb
            qg = data.get("antigravity_quota_guard")
            if qg:
                tier["quota_guard"] = qg
            tiers.append(tier)

        fallback = data.get("fallback") or {}
        if fallback.get("enabled", True):
            # 终端层：不设置 circuit_breaker
            tiers.append({"backend": "zhipu", **fallback})

        data["tiers"] = tiers
        return data

    @property
    def db_path(self) -> Path:
        return Path(self.database.path).expanduser()

    @property
    def compat_state_path(self) -> Path:
        return Path(self.database.compat_state_path).expanduser()


__all__ = [
    "ProxyConfig",
    # server
    "ServerConfig", "DatabaseConfig", "LoggingConfig",
    # backends
    "AnthropicConfig", "CopilotConfig", "AntigravityConfig", "ZhipuConfig",
    # resiliency
    "CircuitBreakerConfig", "RetryConfig", "FailoverConfig", "QuotaGuardConfig",
    # routing
    "BackendType", "TierConfig", "ModelMappingRule", "ModelPricingEntry",
    "_COPILOT_FIELDS", "_ANTIGRAVITY_FIELDS", "_ZHIPU_FIELDS",
    "_BACKEND_EXCLUSIVE_FIELDS",
    # auth
    "AuthConfig",
]
