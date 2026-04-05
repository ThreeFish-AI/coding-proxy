"""基础设施配置模型."""

from __future__ import annotations

from pydantic import BaseModel


class ServerConfig(BaseModel):
    host: str = "127.0.0.1"
    port: int = 8046


class DatabaseConfig(BaseModel):
    path: str = "~/.coding-proxy/usage.db"
    compat_state_path: str = "~/.coding-proxy/compat.db"
    compat_state_ttl_seconds: int = 86400


class LoggingConfig(BaseModel):
    level: str = "INFO"
    file: str | None = None


__all__ = ["ServerConfig", "DatabaseConfig", "LoggingConfig"]
