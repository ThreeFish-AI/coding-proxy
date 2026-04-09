"""Pydantic 配置模型 — 聚合层（re-export 所有子模块符号）.

本文件为向后兼容的聚合入口点，所有配置模型已正交拆分至以下子模块：

- :mod:`.server`        – ServerConfig / DatabaseConfig / LoggingConfig
- :mod:`.vendors`       – AnthropicConfig / CopilotConfig / AntigravityConfig / ZhipuConfig
- :mod:`.resiliency`     – CircuitBreakerConfig / RetryConfig / FailoverConfig / QuotaGuardConfig
- :mod:`.routing`        – VendorType / VendorConfig / ModelMappingRule / ModelPricingEntry
- :mod:`.auth_schema`    – AuthConfig

:py:class:`ProxyConfig` 及其旧格式迁移逻辑保留在此文件中。
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field, model_validator

from .auth_schema import AuthConfig  # noqa: F401
from .resiliency import (  # noqa: F401
    CircuitBreakerConfig,
    FailoverConfig,
    QuotaGuardConfig,
    RetryConfig,
)
from .routing import (  # noqa: F401
    _ANTIGRAVITY_FIELDS,
    _BACKEND_EXCLUSIVE_FIELDS,  # 向后兼容别名
    _COPILOT_FIELDS,
    _NATIVE_ANTHROPIC_FIELDS,
    _VENDOR_EXCLUSIVE_FIELDS,
    _ZHIPU_FIELDS,
    BackendType,  # 向后兼容别名
    ModelMappingRule,
    ModelPricingEntry,
    TierConfig,  # 向后兼容别名
    VendorConfig,
    VendorType,
)

# ── 子模块 re-export ────────────────────────────────────────────
from .server import DatabaseConfig, LoggingConfig, ServerConfig  # noqa: F401
from .vendors import (  # noqa: F401
    AlibabaConfig,
    AnthropicConfig,
    AntigravityConfig,
    CopilotConfig,
    DoubaoConfig,
    KimiConfig,
    MinimaxConfig,
    XiaomiConfig,
    ZhipuConfig,
)

logger = logging.getLogger(__name__)


class ProxyConfig(BaseModel):
    """顶层配置模型.

    .. note::
        以下字段为 **旧 flat 格式**（已废弃，保留仅用于向后兼容迁移）：
        ``primary``, ``copilot``, ``antigravity``, ``fallback``,
        ``circuit_breaker``, ``copilot_circuit_breaker``, ``antigravity_circuit_breaker``,
        ``quota_guard``, ``copilot_quota_guard``, ``antigravity_quota_guard``

        新配置应使用 ``vendors`` 列表格式（参见 config.default.yaml）。
        旧格式会在 ``_migrate_legacy_fields`` 中自动转换为 vendors。
    """

    server: ServerConfig = ServerConfig()

    # ── Legacy 字段（旧 flat 格式，由 _migrate_legacy_fields 自动迁移至 vendors） ──
    primary: AnthropicConfig = Field(
        default=AnthropicConfig(),
        description="[legacy] Anthropic 主供应商配置；新格式请使用 vendors[].vendor=anthropic",
    )
    copilot: CopilotConfig = Field(
        default=CopilotConfig(),
        description="[legacy] Copilot 供应商配置；新格式请使用 vendors[].vendor=copilot",
    )
    antigravity: AntigravityConfig = Field(
        default=AntigravityConfig(),
        description="[legacy] Antigravity 供应商配置；新格式请使用 vendors[].vendor=antigravity",
    )
    fallback: ZhipuConfig = Field(
        default=ZhipuConfig(),
        description="[legacy] 智谱兜底供应商配置；新格式请使用 vendors[].vendor=zhipu",
    )
    circuit_breaker: CircuitBreakerConfig = Field(
        default=CircuitBreakerConfig(),
        description="[legacy] 全局熔断器配置；新格式请使用 vendors[].circuit_breaker",
    )
    copilot_circuit_breaker: CircuitBreakerConfig = Field(
        default=CircuitBreakerConfig(),
        description="[legacy] Copilot 熔断器配置；新格式请使用 vendors[vendor=copilot].circuit_breaker",
    )
    antigravity_circuit_breaker: CircuitBreakerConfig = Field(
        default=CircuitBreakerConfig(),
        description="[legacy] Antigravity 熔断器配置；新格式请使用 vendors[vendor=antigravity].circuit_breaker",
    )
    failover: FailoverConfig = FailoverConfig()
    model_mapping: list[ModelMappingRule] = Field(
        default=[
            ModelMappingRule(
                pattern="claude-sonnet-.*", target="glm-5.1", is_regex=True
            ),
            ModelMappingRule(pattern="claude-opus-.*", target="glm-5.1", is_regex=True),
            ModelMappingRule(
                pattern="claude-haiku-.*", target="glm-4.5-air", is_regex=True
            ),
            ModelMappingRule(pattern="claude-.*", target="glm-5.1", is_regex=True),
        ],
    )
    quota_guard: QuotaGuardConfig = Field(
        default=QuotaGuardConfig(),
        description="[legacy] 全局配额守卫；新格式请使用 vendors[].quota_guard",
    )
    copilot_quota_guard: QuotaGuardConfig = Field(
        default=QuotaGuardConfig(),
        description="[legacy] Copilot 配额守卫；新格式请使用 vendors[vendor=copilot].quota_guard",
    )
    antigravity_quota_guard: QuotaGuardConfig = Field(
        default=QuotaGuardConfig(),
        description="[legacy] Antigravity 配额守卫；新格式请使用 vendors[vendor=antigravity].quota_guard",
    )
    auth: AuthConfig = AuthConfig()
    database: DatabaseConfig = DatabaseConfig()
    logging: LoggingConfig = LoggingConfig()
    # 模型定价（USD / 1M tokens），按 (vendor, model) 匹配
    pricing: list[ModelPricingEntry] = Field(default_factory=list)
    # 新格式：vendors 列表（供应商定义）
    vendors: list[VendorConfig] = Field(default_factory=list)
    # 降级链路优先级（可选）；None 时回退到 vendors 列表顺序
    tiers: list[VendorType] | None = Field(
        default=None,
        description=(
            "显式指定降级链路的优先级顺序（索引越小优先级越高）。"
            "引用的 vendor 必须在 vendors 中存在且 enabled=True。"
            "未配置时回退到 vendors 列表原始顺序。"
        ),
    )

    @model_validator(mode="before")
    @classmethod
    def _migrate_legacy_fields(cls, data: Any) -> Any:
        """向后兼容迁移（legacy flat 格式 → vendors 列表格式）.

        迁移规则：
        1. ``anthropic`` / ``zhipu`` 字段名自动映射为 ``primary`` / ``fallback``
        2. 若配置中未显式指定 ``vendors``，则从旧 flat 格式字段自动生成
        """
        if not isinstance(data, dict):
            return data

        # 1. 字段别名迁移
        if "anthropic" in data and "primary" not in data:
            data["primary"] = data.pop("anthropic")
        if "zhipu" in data and "fallback" not in data:
            data["fallback"] = data.pop("zhipu")

        # 2. 若已有 vendors 配置则直接使用，跳过自动迁移
        #    同时支持旧的 tiers 字段名（向后兼容 YAML）
        #    使用 key 存在性检测（而非真值检测），以正确处理 vendors: [] 等显式空配置
        if "vendors" in data or "tiers" in data:
            # 如果用户使用了旧的 tiers 字段名但实际是 vendor 定义列表，重映射
            if (
                "tiers" in data
                and "vendors" not in data
                and isinstance(data["tiers"], list)
            ):
                # 检测是否为新格式的 vendor 定义列表（每项有 vendor 字段，兼容旧 backend 字段）
                first_item = data["tiers"][0] if data["tiers"] else {}
                if isinstance(first_item, dict) and (
                    "vendor" in first_item or "backend" in first_item
                ):
                    data["vendors"] = data.pop("tiers")
            return data

        # 3. 从旧 flat 格式自动构建 vendors（触发时记录废弃日志）
        vendors: list[dict[str, Any]] = []
        _legacy_keys = {
            "primary",
            "copilot",
            "antigravity",
            "fallback",
            "circuit_breaker",
            "copilot_circuit_breaker",
            "antigravity_circuit_breaker",
            "quota_guard",
            "copilot_quota_guard",
            "antigravity_quota_guard",
        }
        if any(k in data for k in _legacy_keys):
            logger.info(
                "检测到旧 flat 格式配置字段，已自动迁移至 vendors 列表格式。"
                "建议迁移至 config.default.yaml 中的 vendors 新格式。",
            )

        primary = data.get("primary") or {}
        if primary.get("enabled", True):
            vendor_cfg: dict[str, Any] = {"vendor": "anthropic", **primary}
            cb = data.get("circuit_breaker")
            if cb:
                vendor_cfg["circuit_breaker"] = cb
            qg = data.get("quota_guard")
            if qg:
                vendor_cfg["quota_guard"] = qg
            vendors.append(vendor_cfg)

        copilot = data.get("copilot") or {}
        if copilot.get("enabled", False):
            vendor_cfg = {"vendor": "copilot", **copilot}
            cb = data.get("copilot_circuit_breaker")
            if cb:
                vendor_cfg["circuit_breaker"] = cb
            qg = data.get("copilot_quota_guard")
            if qg:
                vendor_cfg["quota_guard"] = qg
            vendors.append(vendor_cfg)

        antigravity = data.get("antigravity") or {}
        if antigravity.get("enabled", False):
            vendor_cfg = {"vendor": "antigravity", **antigravity}
            cb = data.get("antigravity_circuit_breaker")
            if cb:
                vendor_cfg["circuit_breaker"] = cb
            qg = data.get("antigravity_quota_guard")
            if qg:
                vendor_cfg["quota_guard"] = qg
            vendors.append(vendor_cfg)

        fallback = data.get("fallback") or {}
        if fallback.get("enabled", True):
            # 终端层：不设置 circuit_breaker
            vendors.append({"vendor": "zhipu", **fallback})

        data["vendors"] = vendors
        return data

    @model_validator(mode="after")
    def _validate_tiers(self) -> ProxyConfig:
        """校验 tiers 引用的 vendor 必须在 enabled vendors 中存在."""
        if self.tiers is None:
            return self  # 未配置，跳过校验

        # 构建 enabled vendor 的 name 集合
        enabled_vendors = {v.vendor for v in self.vendors if v.enabled}

        # 检查重复
        seen: set[str] = set()
        for name in self.tiers:
            if name in seen:
                raise ValueError(f"tiers 包含重复的 vendor 名称: {name!r}")
            seen.add(name)

            # 检查引用是否存在
            all_vendors = {v.vendor for v in self.vendors}
            if name not in all_vendors:
                raise ValueError(
                    f"tiers 引用了不存在的 vendor: {name!r}。"
                    f"可用的 vendor: {sorted(all_vendors)}"
                )

            # 存在但 disabled → warning
            if name not in enabled_vendors:
                logger.warning(
                    "tiers 引用了 disabled 的 vendor: %s，该层级将在运行时被跳过",
                    name,
                )

        return self

    @property
    def db_path(self) -> Path:
        return Path(self.database.path).expanduser()

    @property
    def compat_state_path(self) -> Path:
        return Path(self.database.compat_state_path).expanduser()


__all__ = [
    "ProxyConfig",
    # server
    "ServerConfig",
    "DatabaseConfig",
    "LoggingConfig",
    # vendors
    "AnthropicConfig",
    "CopilotConfig",
    "AntigravityConfig",
    "ZhipuConfig",
    # resiliency
    "CircuitBreakerConfig",
    "RetryConfig",
    "FailoverConfig",
    "QuotaGuardConfig",
    # routing
    "VendorType",
    "VendorConfig",
    "ModelMappingRule",
    "ModelPricingEntry",
    "TierConfig",  # 向后兼容别名
    "BackendType",  # 向后兼容别名
    "_COPILOT_FIELDS",
    "_ANTIGRAVITY_FIELDS",
    "_ZHIPU_FIELDS",
    "_NATIVE_ANTHROPIC_FIELDS",
    "_VENDOR_EXCLUSIVE_FIELDS",
    "_BACKEND_EXCLUSIVE_FIELDS",
    # auth
    "AuthConfig",
    # new native anthropic vendor configs
    "MinimaxConfig",
    "KimiConfig",
    "DoubaoConfig",
    "XiaomiConfig",
    "AlibabaConfig",
]
