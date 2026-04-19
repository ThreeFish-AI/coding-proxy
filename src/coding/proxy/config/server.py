"""基础设施配置模型."""

from __future__ import annotations

from pydantic import BaseModel


class ServerConfig(BaseModel):
    host: str = "127.0.0.1"
    port: int = 3392


class DatabaseConfig(BaseModel):
    path: str = "~/.coding-proxy/usage.db"
    compat_state_path: str = "~/.coding-proxy/compat.db"
    compat_state_ttl_seconds: int = 86400


class LoggingConfig(BaseModel):
    """日志配置.

    Attributes:
        level: 控制台日志级别（INFO / WARNING / DEBUG 等）。
        file: 文件日志路径。为 ``None`` 时使用默认值 ``coding-proxy.log``；
             设为空字符串可禁用文件日志。
        max_bytes: 单个日志文件最大字节数（触发轮转）。默认 5 MB。
        backup_count: 保留的已压缩备份文件数。默认 5。
    """

    level: str = "INFO"
    file: str | None = None
    max_bytes: int = 5 * 1024 * 1024
    backup_count: int = 5


__all__ = ["ServerConfig", "DatabaseConfig", "LoggingConfig"]
