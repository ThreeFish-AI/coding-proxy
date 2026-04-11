"""结构化日志格式化器（JSON 输出）.

为文件日志提供机器可读的 JSON 格式输出，
与控制台的人类可读格式形成正交双写。
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone


class JsonFormatter(logging.Formatter):
    """将 LogRecord 格式化为单行 JSON 字符串.

    输出字段：
    - ``timestamp``: ISO 8601 UTC 时间戳
    - ``level``: 日志级别名称（DEBUG/INFO/WARNING/ERROR）
    - ``logger``: logger 名称（如 ``coding.proxy.routing.executor``）
    - ``message``: 格式化后的日志消息
    - ``exception``: 异常堆栈（仅当存在时）

    设计要点：
    - 使用 ``ensure_ascii=False`` 支持中文日志内容
    - 异常信息（exc_info）序列化为 ``exception`` 字段
    - 时间戳统一使用 UTC ISO 格式，便于跨时区聚合分析
    - ``sort_keys=True`` 保证输出确定性，便于日志聚合工具处理
    """

    def format(self, record: logging.LogRecord) -> str:
        """将 LogRecord 序列化为 JSON 行."""
        message = record.getMessage()

        exception: str | None = None
        if record.exc_info and record.exc_info[0] is not None:
            exception = self.formatException(record.exc_info)

        log_entry: dict[str, object] = {
            "timestamp": datetime.fromtimestamp(
                record.created, tz=timezone.utc
            ).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": message,
        }

        if exception:
            log_entry["exception"] = exception

        return json.dumps(log_entry, ensure_ascii=False, sort_keys=True)
