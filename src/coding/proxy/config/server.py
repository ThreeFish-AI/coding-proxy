"""基础设施配置模型."""

from __future__ import annotations

from pydantic import BaseModel, Field

from .home import default_compat_db_str, default_usage_db_str


class ServerConfig(BaseModel):
    host: str = "127.0.0.1"
    port: int = 3392
    # 管理面（Dashboard / 非透传 /api/*）额外允许的 Host 主机名；loopback 恒允许
    allowed_hosts: list[str] = Field(default_factory=list)


class DatabaseConfig(BaseModel):
    # 默认由 $CODING_PROXY_HOME 推导（未设置时等价于旧字面量 ~/.coding-proxy/*.db）
    path: str = Field(default_factory=default_usage_db_str)
    compat_state_path: str = Field(default_factory=default_compat_db_str)
    compat_state_ttl_seconds: int = 86400


class LoggingConfig(BaseModel):
    """日志配置.

    Attributes:
        level: 控制台日志级别（INFO / WARNING / DEBUG 等）。
        file: 文件日志路径。为 ``None`` 时使用默认值 ``.logs/coding-proxy.log``；
             设为空字符串可禁用文件日志。
        max_bytes: 单个日志文件最大字节数（触发轮转）。默认 5 MB。
        backup_count: 保留的已压缩备份文件数。默认 5。
    """

    level: str = "INFO"
    file: str | None = None
    max_bytes: int = 5 * 1024 * 1024
    backup_count: int = 5


__all__ = ["ServerConfig", "DatabaseConfig", "LoggingConfig"]
