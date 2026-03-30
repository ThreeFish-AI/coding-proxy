"""Pydantic 配置模型."""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from pydantic import BaseModel, Field


class ServerConfig(BaseModel):
    host: str = "127.0.0.1"
    port: int = 8046


class AnthropicConfig(BaseModel):
    enabled: bool = True
    base_url: str = "https://api.anthropic.com"
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


class DatabaseConfig(BaseModel):
    path: str = "~/.coding-proxy/usage.db"


class LoggingConfig(BaseModel):
    level: str = "INFO"
    file: Optional[str] = None


class ProxyConfig(BaseModel):
    server: ServerConfig = ServerConfig()
    primary: AnthropicConfig = AnthropicConfig()
    fallback: ZhipuConfig = ZhipuConfig()
    circuit_breaker: CircuitBreakerConfig = CircuitBreakerConfig()
    failover: FailoverConfig = FailoverConfig()
    model_mapping: list[ModelMappingRule] = Field(
        default=[
            ModelMappingRule(pattern="claude-sonnet-.*", target="glm-5.1", is_regex=True),
            ModelMappingRule(pattern="claude-opus-.*", target="glm-5.1", is_regex=True),
            ModelMappingRule(pattern="claude-haiku-.*", target="glm-4.5-air", is_regex=True),
            ModelMappingRule(pattern="claude-.*", target="glm-5.1", is_regex=True),
        ],
    )
    database: DatabaseConfig = DatabaseConfig()
    logging: LoggingConfig = LoggingConfig()

    @property
    def db_path(self) -> Path:
        return Path(self.database.path).expanduser()
