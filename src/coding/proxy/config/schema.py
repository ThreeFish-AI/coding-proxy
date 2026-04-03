"""Pydantic 配置模型."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Literal, Optional

from pydantic import BaseModel, Field, model_validator

logger = logging.getLogger(__name__)

# ── 后端专属字段分组映射 ────────────────────────────────────────
# 每个 backend 类型对应其专属字段集合，用于 TierConfig 的语义标注与校验

_COPILOT_FIELDS: frozenset[str] = frozenset({
    "github_token", "account_type", "token_url", "models_cache_ttl_seconds",
})
_ANTIGRAVITY_FIELDS: frozenset[str] = frozenset({
    "client_id", "client_secret", "refresh_token", "model_endpoint",
})
_ZHIPU_FIELDS: frozenset[str] = frozenset({"api_key",})

_BACKEND_EXCLUSIVE_FIELDS: dict[str, frozenset[str]] = {
    "copilot": _COPILOT_FIELDS,
    "antigravity": _ANTIGRAVITY_FIELDS,
    "zhipu": _ZHIPU_FIELDS,
}


class ServerConfig(BaseModel):
    host: str = "127.0.0.1"
    port: int = 8046


class AnthropicConfig(BaseModel):
    enabled: bool = True
    base_url: str = "https://api.anthropic.com"
    timeout_ms: int = 300000


class CopilotConfig(BaseModel):
    """GitHub Copilot 后端配置."""

    enabled: bool = False
    github_token: str = ""
    account_type: str = "individual"
    token_url: str = "https://api.github.com/copilot_internal/v2/token"
    base_url: str = ""
    models_cache_ttl_seconds: int = 300
    timeout_ms: int = 300000


class AntigravityConfig(BaseModel):
    """Google Antigravity Claude 后端配置."""

    enabled: bool = False
    client_id: str = ""
    client_secret: str = ""
    refresh_token: str = ""
    base_url: str = "https://generativelanguage.googleapis.com/v1beta"
    model_endpoint: str = "models/claude-sonnet-4-20250514"
    timeout_ms: int = 300000
    safety_settings: dict[str, str] | None = None


class ZhipuConfig(BaseModel):
    """智谱 GLM 后端配置（原生 Anthropic 兼容端点）.

    官方端点已完整支持 Anthropic Messages API 协议，
    无需工具截断、thinking 剥离等适配逻辑。
    """

    enabled: bool = True
    base_url: str = "https://open.bigmodel.cn/api/anthropic"
    api_key: str = ""
    timeout_ms: int = 3000000


class CircuitBreakerConfig(BaseModel):
    failure_threshold: int = 3
    recovery_timeout_seconds: int = 300
    success_threshold: int = 2
    max_recovery_seconds: int = 3600


class RetryConfig(BaseModel):
    """传输层重试配置."""

    max_retries: int = 2
    initial_delay_ms: int = 500
    max_delay_ms: int = 5000
    backoff_multiplier: float = 2.0
    jitter: bool = True


class FailoverConfig(BaseModel):
    status_codes: list[int] = Field(
        default=[429, 403, 503, 500],
    )
    error_types: list[str] = Field(
        default=["rate_limit_error", "overloaded_error", "api_error"],
    )
    error_message_patterns: list[str] = Field(
        default=["quota", "limit exceeded", "usage cap", "capacity"],
    )


class ModelMappingRule(BaseModel):
    pattern: str
    target: str
    is_regex: bool = False
    backends: list[str] = Field(default_factory=list)


class ModelPricingEntry(BaseModel):
    """单个模型的定价配置（USD / 1M tokens）."""

    backend: str                            # 后端名称（对应 usage 表"后端"列）
    model: str                              # 实际模型名（对应 usage 表"实际模型"列）
    input_cost_per_mtok: float = 0.0        # 输入 Token 单价
    output_cost_per_mtok: float = 0.0       # 输出 Token 单价
    cache_write_cost_per_mtok: float = 0.0  # 缓存创建 Token 单价
    cache_read_cost_per_mtok: float = 0.0   # 缓存读取 Token 单价


class QuotaGuardConfig(BaseModel):
    enabled: bool = False
    token_budget: int = 0
    window_hours: float = 5.0
    threshold_percent: float = 99.0
    probe_interval_seconds: int = 300


BackendType = Literal["anthropic", "copilot", "antigravity", "zhipu"]


class TierConfig(BaseModel):
    """单个 Tier 的统一配置（支持所有后端类型）.

    列表顺序即优先级：index 越小优先级越高。
    无 circuit_breaker 的 Tier 为终端层（不触发故障转移）。

    各后端类型的专属字段已通过 ``Field(description=...)`` 标注适用范围，
    非当前 backend 类型的专属字段在验证阶段会发出 warning 日志。
    """

    backend: BackendType

    # ── 通用字段（所有后端共用） ──────────────────────────────
    enabled: bool = True
    base_url: str = Field(
        default="",
        description="后端 API 基础 URL；留空时使用各后端默认值",
    )
    timeout_ms: int = Field(
        default=300000,
        description="请求超时时间（毫秒），适用于所有后端",
    )

    # ── Copilot 专属字段 ─────────────────────────────────────
    github_token: str = Field(
        default="",
        description="[copilot] GitHub Personal Access Token 或 OAuth Token",
    )
    account_type: str = Field(
        default="individual",
        description="[copilot] Copilot 账户类型：individual / business / enterprise",
    )
    token_url: str = Field(
        default="https://api.github.com/copilot_internal/v2/token",
        description="[copilot] Copilot Token 交换端点 URL",
    )
    models_cache_ttl_seconds: int = Field(
        default=300,
        description="[copilot] 模型列表缓存 TTL（秒）",
    )

    # ── Antigravity 专属字段 ──────────────────────────────────
    client_id: str = Field(
        default="",
        description="[antigravity] Google OAuth2 Client ID",
    )
    client_secret: str = Field(
        default="",
        description="[antigravity] Google OAuth2 Client Secret",
    )
    refresh_token: str = Field(
        default="",
        description="[antigravity] Google OAuth2 Refresh Token",
    )
    model_endpoint: str = Field(
        default="models/claude-sonnet-4-20250514",
        description="[antigravity] Antigravity 模型端点路径",
    )

    # ── Zhipu 专属字段 ────────────────────────────────────────
    api_key: str = Field(
        default="",
        description="[zhipu] 智谱 GLM API Key",
    )

    # ── 弹性配置 ──────────────────────────────────────────────
    circuit_breaker: CircuitBreakerConfig | None = Field(
        default=None,
        description="熔断器配置；None 表示终端层（不触发故障转移）",
    )
    retry: RetryConfig = Field(default_factory=RetryConfig)
    quota_guard: QuotaGuardConfig = Field(default_factory=QuotaGuardConfig)
    weekly_quota_guard: QuotaGuardConfig = Field(default_factory=QuotaGuardConfig)

    @model_validator(mode="after")
    def _warn_irrelevant_fields(self) -> "TierConfig":
        """对非当前 backend 类型的非空专属字段发出 warning."""
        exclusive = _BACKEND_EXCLUSIVE_FIELDS.get(self.backend)
        if not exclusive:
            return self
        for backend_type, fields in _BACKEND_EXCLUSIVE_FIELDS.items():
            if backend_type == self.backend:
                continue
            for field_name in fields:
                value = getattr(self, field_name, None)
                if value and value != getattr(TierConfig.model_fields[field_name], "default", None):
                    logger.warning(
                        "TierConfig(backend=%s): 字段 %s 属于 %s 后端，当前值将被忽略",
                        self.backend, field_name, backend_type,
                    )
        return self


class DatabaseConfig(BaseModel):
    path: str = "~/.coding-proxy/usage.db"
    compat_state_path: str = "~/.coding-proxy/compat.db"
    compat_state_ttl_seconds: int = 86400


class LoggingConfig(BaseModel):
    level: str = "INFO"
    file: Optional[str] = None


class AuthConfig(BaseModel):
    """OAuth 登录配置."""

    github_client_id: str = "Iv1.b507a08c87ecfe98"
    google_client_id: str = (
        "1071006060591-tmhssin2h21lcre235vtolojh4g403ep"
        ".apps.googleusercontent.com"
    )
    google_client_secret: str = "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"
    token_store_path: str = "~/.coding-proxy/tokens.json"


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
