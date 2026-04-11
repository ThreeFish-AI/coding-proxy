"""文件日志格式化器（字符串输出）.

为文件日志提供人类可读的字符串格式输出，
与控制台输出风格一致，便于人工阅读和 grep 检索。
"""

from __future__ import annotations

import logging


class FileFormatter(logging.Formatter):
    """将 LogRecord 格式化为单行可读字符串.

    输出格式：``2026-04-11 16:51:13 INFO  ModelCall: vendor=zhipu ...``

    设计要点：
    - 时间戳使用 ``yyyy-MM-dd HH:mm:ss`` 格式（与控制台一致）
    - 日志级别左对齐 5 字符宽度，保证多行对齐美观
    - 无 ANSI 颜色码（文件输出不需要终端转义）
    """

    def __init__(self) -> None:
        super().__init__(
            fmt="%(asctime)s %(levelname)-5s %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
