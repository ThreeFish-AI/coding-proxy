"""日志模块.

提供 uvicorn 兼容的 dictConfig 构建、JSON 结构化格式化器、
以及 gzip 压缩轮转支持。
"""

from __future__ import annotations

import gzip
import logging
import logging.handlers
import os
from pathlib import Path

from .formatters import JsonFormatter

# ── 常量 ────────────────────────────────────────────────────────

_DEFAULT_MAX_BYTES = 5 * 1024 * 1024  # 5 MB per file
_DEFAULT_BACKUP_COUNT = 5  # Keep 5 rotated backups
_FILE_LOG_LEVEL = "DEBUG"  # File logs capture everything


def _gzip_namer(default_name: str) -> str:
    """RotatingFileHandler namer: 为轮转文件添加 .gz 后缀."""
    return default_name + ".gz"


def _gzip_rotator(source: str, dest: str) -> None:
    """RotatingFileHandler rotator: 将源文件 gzip 压缩后写入目标.

    流程：
    1. 读取 source 文件全部内容
    2. gzip 压缩写入 dest 文件
    3. 删除 source 原文件
    """
    with open(source, "rb") as f_in:
        data = f_in.read()
    with open(dest, "wb") as f_out:
        f_out.write(gzip.compress(data, compresslevel=6))
    os.remove(source)


def _create_rotating_file_handler(
    *,
    filename: str,
    maxBytes: int = _DEFAULT_MAX_BYTES,
    backupCount: int = _DEFAULT_BACKUP_COUNT,
    encoding: str = "utf-8",
) -> logging.handlers.RotatingFileHandler:
    """创建带 gzip 压缩轮转的 RotatingFileHandler（dictConfig 兼容工厂函数）.

    ``logging.config.dictConfig`` 仅支持通过构造函数 kwargs 配置 handler，
    而 ``namer`` / ``rotator`` 是实例属性而非构造参数，因此需要通过工厂函数注入。
    """
    handler = logging.handlers.RotatingFileHandler(
        filename=filename,
        maxBytes=maxBytes,
        backupCount=backupCount,
        encoding=encoding,
    )
    handler.namer = _gzip_namer
    handler.rotator = _gzip_rotator
    return handler


def build_log_config(
    level: str = "INFO",
    file_path: str | None = None,
    max_bytes: int = _DEFAULT_MAX_BYTES,
    backup_count: int = _DEFAULT_BACKUP_COUNT,
) -> dict:
    """构建 uvicorn log_config，支持双写（控制台 + 文件）.

    Args:
        level: 控制台日志级别（默认 INFO）。
        file_path: 文件日志路径。为 ``None`` 时仅输出到控制台（向后兼容）。
        max_bytes: 单个日志文件最大字节数（默认 5 MB）。
        backup_count: 保留的轮转备份文件数（默认 5）。

    Returns:
        符合 ``logging.config.dictConfig`` 规范的字典。

    双写行为：
        - 控制台：人类可读格式，级别由 ``level`` 参数控制（handler 级别过滤）
        - 文件：JSON 结构化格式，固定 DEBUG 级别（捕获所有日志）
        - 当 ``file_path`` 为 ``None`` 或空字符串时，退化为纯控制台模式（向后兼容）
    """
    config: dict = {
        "version": 1,
        "disable_existing_loggers": False,
        "formatters": {
            "default": {
                "()": "uvicorn.logging.DefaultFormatter",
                "fmt": "%(asctime)s %(levelprefix)s %(message)s",
                "datefmt": "%Y-%m-%d %H:%M:%S",
                "use_colors": None,
            },
            "access": {
                "()": "uvicorn.logging.AccessFormatter",
                "fmt": '%(asctime)s %(levelprefix)s %(client_addr)s - "%(request_line)s" %(status_code)s',
                "datefmt": "%Y-%m-%d %H:%M:%S",
            },
        },
        "handlers": {
            "default": {
                "formatter": "default",
                "class": "logging.StreamHandler",
                "stream": "ext://sys.stderr",
                "level": level,
            },
            "access": {
                "formatter": "access",
                "class": "logging.StreamHandler",
                "stream": "ext://sys.stdout",
                "level": "INFO",
            },
        },
        "loggers": {
            "uvicorn": {"handlers": ["default"], "level": level, "propagate": False},
            "uvicorn.error": {"level": level},
            "uvicorn.access": {
                "handlers": ["access"],
                "level": "INFO",
                "propagate": False,
            },
            "coding.proxy": {
                "handlers": ["default"],
                "level": level,
                "propagate": False,
            },
        },
    }

    # ── 条件注入：文件日志基础设施 ────────────────────────────
    if file_path:
        # 确保日志目录存在
        log_file = Path(file_path)
        log_file.parent.mkdir(parents=True, exist_ok=True)

        # 注入 JSON formatter
        config["formatters"]["json"] = {
            "()": "coding.proxy.logging.formatters.JsonFormatter",
        }

        # 注入 RotatingFileHandler（gzip 压缩轮转）
        # 使用工厂函数（而非 class + namer/rotator kwargs），
        # 因为 dictConfig 不支持将 namer/rotator 作为构造参数传递
        config["handlers"]["file"] = {
            "formatter": "json",
            "()": "coding.proxy.logging._create_rotating_file_handler",
            "filename": str(log_file.resolve()),
            "maxBytes": max_bytes,
            "backupCount": backup_count,
            "encoding": "utf-8",
        }

        # 为每个 logger 添加 file handler
        # 注意：uvicorn.error 无 handlers 键（通过 propagate 继承 uvicorn 的 handler）
        for logger_name in (
            "uvicorn",
            "uvicorn.error",
            "uvicorn.access",
            "coding.proxy",
        ):
            logger_cfg = config["loggers"][logger_name]
            handlers = logger_cfg.get("handlers", [])
            if isinstance(handlers, list):
                handlers.append("file")
                logger_cfg["handlers"] = handlers
            else:
                logger_cfg["handlers"] = [handlers, "file"]

        # Logger 级别设为 DEBUG（让所有消息通过到 file handler）
        # Console handler 已设 level 过滤，确保控制台仅输出 INFO+
        config["loggers"]["coding.proxy"]["level"] = _FILE_LOG_LEVEL
        config["loggers"]["uvicorn"]["level"] = _FILE_LOG_LEVEL
        config["loggers"]["uvicorn.error"]["level"] = _FILE_LOG_LEVEL

    return config


__all__ = [
    "build_log_config",
    "JsonFormatter",
    "_gzip_namer",
    "_gzip_rotator",
]
