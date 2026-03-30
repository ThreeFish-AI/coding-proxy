"""Pydantic 配置模型."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

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
    token_url: str = "https://github.com/github-copilot/chat/token"
    base_url: str = "https://api.individual.githubcopilot.com"
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


class QuotaGuardConfig(BaseModel):
    enabled: bool = False
    token_budget: int = 0
    window_hours: float = 5.0
    threshold_percent: float = 99.0
    probe_interval_seconds: int = 300


class DatabaseConfig(BaseModel):
    path: str = "~/.coding-proxy/usage.db"


class LoggingConfig(BaseModel):
    level: str = "INFO"
    file: Optional[str] = None


class ProxyConfig(BaseModel):
    server: ServerConfig = ServerConfig()
    primary: AnthropicConfig = AnthropicConfig()
    copilot: CopilotConfig = CopilotConfig()
    fallback: ZhipuConfig = ZhipuConfig()
    circuit_breaker: CircuitBreakerConfig = CircuitBreakerConfig()
    copilot_circuit_breaker: CircuitBreakerConfig = CircuitBreakerConfig()
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
    database: DatabaseConfig = DatabaseConfig()
    logging: LoggingConfig = LoggingConfig()

    @model_validator(mode="before")
    @classmethod
    def _migrate_legacy_fields(cls, data: Any) -> Any:
        """向后兼容：支持 anthropic/zhipu 作为 primary/fallback 的别名."""
        if isinstance(data, dict):
            if "anthropic" in data and "primary" not in data:
                data["primary"] = data.pop("anthropic")
            if "zhipu" in data and "fallback" not in data:
                data["fallback"] = data.pop("zhipu")
        return data

    @property
    def db_path(self) -> Path:
        return Path(self.database.path).expanduser()
