"""Pydantic 配置模型."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Literal, Optional

from pydantic import BaseModel, Field, model_validator


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


class ZhipuConfig(BaseModel):
    enabled: bool = True
    base_url: str = "https://open.bigmodel.cn/api/anthropic"
    api_key: str = ""
    timeout_ms: int = 3000000


class CircuitBreakerConfig(BaseModel):
    failure_threshold: int = 3
    recovery_timeout_seconds: int = 300
    success_threshold: int = 2
    max_recovery_seconds: int = 3600


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
    """

    backend: BackendType

    # 通用字段
    enabled: bool = True
    base_url: str = ""
    timeout_ms: int = 300000

    # Copilot 专属
    github_token: str = ""
    account_type: str = "individual"
    token_url: str = "https://api.github.com/copilot_internal/v2/token"
    models_cache_ttl_seconds: int = 300

    # Antigravity 专属
    client_id: str = ""
    client_secret: str = ""
    refresh_token: str = ""
    model_endpoint: str = "models/claude-sonnet-4-20250514"

    # Zhipu 专属
    api_key: str = ""

    # 弹性配置（None = 终端层，无熔断器）
    circuit_breaker: CircuitBreakerConfig | None = None
    quota_guard: QuotaGuardConfig = Field(default_factory=QuotaGuardConfig)
    weekly_quota_guard: QuotaGuardConfig = Field(default_factory=QuotaGuardConfig)


class DatabaseConfig(BaseModel):
    path: str = "~/.coding-proxy/usage.db"


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
    server: ServerConfig = ServerConfig()
    primary: AnthropicConfig = AnthropicConfig()
    copilot: CopilotConfig = CopilotConfig()
    antigravity: AntigravityConfig = AntigravityConfig()
    fallback: ZhipuConfig = ZhipuConfig()
    circuit_breaker: CircuitBreakerConfig = CircuitBreakerConfig()
    copilot_circuit_breaker: CircuitBreakerConfig = CircuitBreakerConfig()
    antigravity_circuit_breaker: CircuitBreakerConfig = CircuitBreakerConfig()
    failover: FailoverConfig = FailoverConfig()
    model_mapping: list[ModelMappingRule] = Field(
        default=[
            ModelMappingRule(pattern="claude-sonnet-.*", target="glm-5.1", is_regex=True),
            ModelMappingRule(pattern="claude-opus-.*", target="glm-5.1", is_regex=True),
            ModelMappingRule(pattern="claude-haiku-.*", target="glm-4.5-air", is_regex=True),
            ModelMappingRule(pattern="claude-.*", target="glm-5.1", is_regex=True),
        ],
    )
    quota_guard: QuotaGuardConfig = QuotaGuardConfig()
    copilot_quota_guard: QuotaGuardConfig = QuotaGuardConfig()
    antigravity_quota_guard: QuotaGuardConfig = QuotaGuardConfig()
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
        """向后兼容：
        1. 支持 anthropic/zhipu 作为 primary/fallback 的别名
        2. 若未指定 tiers，则从旧 flat 格式自动迁移生成 tiers 列表
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

        # 3. 从旧 flat 格式自动构建 tiers
        tiers: list[dict[str, Any]] = []

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
