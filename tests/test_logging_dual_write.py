"""双写日志系统单元测试.

覆盖：
1. JsonFormatter 输出格式验证
2. build_log_config 无 file_path 时的向后兼容性
3. build_log_config 有 file_path 时的双写配置生成
4. handler 级别过滤（console=INFO, file=DEBUG）
5. GzipRotator 功能验证
6. LoggingConfig 模型校验
"""

from __future__ import annotations

import gzip
import json
import logging
import logging.config

from coding.proxy.config.server import LoggingConfig
from coding.proxy.logging import (
    JsonFormatter,
    _gzip_namer,
    _gzip_rotator,
    build_log_config,
)

# ── JsonFormatter 测试 ──────────────────────────────────────────


class TestJsonFormatter:
    def test_basic_output_is_valid_json(self):
        fmt = JsonFormatter()
        record = logging.LogRecord(
            name="test.logger",
            level=logging.INFO,
            pathname="test.py",
            lineno=1,
            msg="hello world",
            args=(),
            exc_info=None,
        )
        result = fmt.format(record)
        parsed = json.loads(result)
        assert parsed["level"] == "INFO"
        assert parsed["logger"] == "test.logger"
        assert parsed["message"] == "hello world"
        assert "timestamp" in parsed

    def test_timestamp_is_iso_utc(self):
        fmt = JsonFormatter()
        record = logging.LogRecord(
            name="test",
            level=logging.DEBUG,
            pathname="",
            lineno=1,
            msg="ts_test",
            args=(),
            exc_info=None,
        )
        result = fmt.format(record)
        parsed = json.loads(result)
        ts = parsed["timestamp"]
        # ISO 格式应包含 T 分隔符或 + 偏移量
        assert "T" in ts or "+" in ts

    def test_exception_serialization(self):
        fmt = JsonFormatter()
        try:
            raise ValueError("test error")
        except ValueError:
            exc_info = __import__("sys").exc_info()
        record = logging.LogRecord(
            name="test",
            level=logging.ERROR,
            pathname="",
            lineno=1,
            msg="error occurred",
            args=(),
            exc_info=exc_info,
        )
        result = fmt.format(record)
        parsed = json.loads(result)
        assert "exception" in parsed
        assert "ValueError" in parsed["exception"]

    def test_percent_style_formatting(self):
        fmt = JsonFormatter()
        record = logging.LogRecord(
            name="test",
            level=logging.INFO,
            pathname="",
            lineno=1,
            msg="value=%s count=%d",
            args=("foo", 42),
            exc_info=None,
        )
        result = fmt.format(record)
        parsed = json.loads(result)
        assert parsed["message"] == "value=foo count=42"

    def test_chinese_message(self):
        fmt = JsonFormatter()
        record = logging.LogRecord(
            name="test",
            level=logging.INFO,
            pathname="",
            lineno=1,
            msg="模型调用成功",
            args=(),
            exc_info=None,
        )
        result = fmt.format(record)
        parsed = json.loads(result)
        assert "模型调用成功" in parsed["message"]

    def test_sorted_keys(self):
        fmt = JsonFormatter()
        record = logging.LogRecord(
            name="test",
            level=logging.INFO,
            pathname="",
            lineno=1,
            msg="sort_test",
            args=(),
            exc_info=None,
        )
        result = fmt.format(record)
        keys = list(json.loads(result).keys())
        assert keys == sorted(keys)


# ── build_log_config 向后兼容测试 ──────────────────────────────


class TestBuildLogConfigBackwardCompat:
    def test_no_file_path_returns_console_only(self):
        config = build_log_config(level="INFO")
        assert "file" not in config["handlers"]
        assert "json" not in config["formatters"]
        assert config["loggers"]["coding.proxy"]["handlers"] == ["default"]

    def test_none_file_path_returns_console_only(self):
        config = build_log_config(level="INFO", file_path=None)
        assert "file" not in config["handlers"]

    def test_empty_string_file_path_returns_console_only(self):
        config = build_log_config(level="INFO", file_path="")
        assert "file" not in config["handlers"]

    def test_default_level_is_info(self):
        config = build_log_config()
        assert config["loggers"]["coding.proxy"]["level"] == "INFO"

    def test_console_handler_has_explicit_level(self):
        config = build_log_config(level="WARNING")
        assert config["handlers"]["default"]["level"] == "WARNING"


# ── build_log_config 双写测试 ──────────────────────────────────


class TestBuildLogConfigDualWrite:
    def test_file_handler_injected(self, tmp_path):
        log_file = tmp_path / "test.log"
        config = build_log_config(level="INFO", file_path=str(log_file))
        assert "file" in config["handlers"]
        assert "json" in config["formatters"]

    def test_file_handler_uses_rotating(self, tmp_path):
        log_file = tmp_path / "test.log"
        config = build_log_config(level="INFO", file_path=str(log_file))
        fh = config["handlers"]["file"]
        # 使用工厂函数创建（dictConfig 兼容）
        assert "coding.proxy.logging._create_rotating_file_handler" in fh.get("()", "")
        assert fh["maxBytes"] == 5 * 1024 * 1024
        assert fh["backupCount"] == 5

    def test_custom_max_bytes_and_backup(self, tmp_path):
        log_file = tmp_path / "test.log"
        config = build_log_config(
            level="INFO",
            file_path=str(log_file),
            max_bytes=1024,
            backup_count=3,
        )
        fh = config["handlers"]["file"]
        assert fh["maxBytes"] == 1024
        assert fh["backupCount"] == 3

    def test_coding_proxy_logger_has_both_handlers(self, tmp_path):
        log_file = tmp_path / "test.log"
        config = build_log_config(level="INFO", file_path=str(log_file))
        handlers = config["loggers"]["coding.proxy"]["handlers"]
        assert "default" in handlers
        assert "file" in handlers

    def test_uvicorn_loggers_get_file_handler(self, tmp_path):
        log_file = tmp_path / "test.log"
        config = build_log_config(level="WARNING", file_path=str(log_file))
        for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
            handlers = config["loggers"][name]["handlers"]
            assert "file" in handlers, f"{name} missing file handler"

    def test_file_logger_level_is_debug(self, tmp_path):
        log_file = tmp_path / "test.log"
        config = build_log_config(level="INFO", file_path=str(log_file))
        assert config["loggers"]["coding.proxy"]["level"] == "DEBUG"

    def test_gzip_factory_creates_handler_with_namer_rotator(self, tmp_path):
        """验证工厂函数正确设置 namer 和 rotator 属性."""
        from coding.proxy.logging import _create_rotating_file_handler

        log_file = tmp_path / "factory_test.log"
        handler = _create_rotating_file_handler(
            filename=str(log_file),
            maxBytes=1024,
            backupCount=3,
        )
        assert handler.namer is not None
        assert handler.rotator is not None
        assert handler.namer("test.log") == "test.log.gz"
        handler.close()


# ── GzipRotator 功能测试 ───────────────────────────────────────


class TestGzipRotation:
    def test_gzip_namer_adds_extension(self):
        assert _gzip_namer("test.log") == "test.log.gz"
        assert _gzip_namer("/var/log/test.log.1") == "/var/log/test.log.1.gz"

    def test_gzip_rotator_creates_compressed_file(self, tmp_path):
        src = tmp_path / "source.log"
        dst = tmp_path / "dest.log.gz"
        original_content = b"x" * 1000
        src.write_bytes(original_content)

        _gzip_rotator(str(src), str(dst))

        assert not src.exists()
        assert dst.exists()
        compressed = dst.read_bytes()
        decompressed = gzip.decompress(compressed)
        assert decompressed == original_content
        assert len(compressed) < len(original_content)


# ── LoggingConfig 模型测试 ─────────────────────────────────────


class TestLoggingConfig:
    def test_defaults(self):
        cfg = LoggingConfig()
        assert cfg.level == "INFO"
        assert cfg.file is None
        assert cfg.max_bytes == 5 * 1024 * 1024
        assert cfg.backup_count == 5

    def test_custom_values(self):
        cfg = LoggingConfig(
            level="DEBUG",
            file="/var/log/app.log",
            max_bytes=10 * 1024 * 1024,
            backup_count=10,
        )
        assert cfg.level == "DEBUG"
        assert cfg.file == "/var/log/app.log"

    def test_file_can_be_disabled(self):
        cfg = LoggingConfig(file=None)
        assert cfg.file is None


# ── 集成测试：端到端双写验证 ──────────────────────────────────


class TestDualWriteIntegration:
    @staticmethod
    def _reset_logging_state() -> None:
        """彻底重置 logging 模块全局状态，消除 dictConfig 的副作用.

        ``logging.config.dictConfig()`` 会修改管理器内部状态：
        - 移除已有 handler 并注入新的（StreamHandler + RotatingFileHandler）
        - 将 logger 级别设为 DEBUG
        - **将 propagate 设为 False**（这是导致后续 caplog 测试失败的关键原因）

        若不重置，``coding.proxy`` 及其子 logger 的消息无法传播到 root logger，
        而 pytest 的 caplog fixture 恰好通过 root logger 捕获记录。
        """
        manager = logging.root.manager
        for name, lg in list(manager.loggerDict.items()):
            if isinstance(lg, logging.Logger):
                lg.handlers.clear()
                lg.level = logging.NOTSET
                lg.propagate = True
        root = logging.getLogger()
        root.setLevel(logging.WARNING)

    def test_debug_visible_in_file_not_console(self, tmp_path, capsys):
        """验证 DEBUG 级别消息写入文件但不显示在控制台."""
        log_file = tmp_path / "integration.log"
        config = build_log_config(level="INFO", file_path=str(log_file))

        try:
            logging.config.dictConfig(config)

            logger = logging.getLogger("coding.proxy.test_integration")
            logger.debug("debug_only_message")
            logger.info("info_message")

            # 验证文件包含两条记录
            log_content = log_file.read_text()
            lines = [line for line in log_content.strip().split("\n") if line]
            assert len(lines) == 2

            messages = [json.loads(line)["message"] for line in lines]
            assert "debug_only_message" in messages
            assert "info_message" in messages
        finally:
            self._reset_logging_state()

    def test_console_only_shows_info_and_above(self, tmp_path, capsys):
        """验证控制台只输出 INFO+ 级别的消息."""
        log_file = tmp_path / "console_filter.log"
        config = build_log_config(level="INFO", file_path=str(log_file))

        try:
            logging.config.dictConfig(config)

            logger = logging.getLogger("coding.proxy.test_console")
            logger.debug("should_not_appear")
            logger.info("should_appear")

            captured = capsys.readouterr()
            assert "should_not_appear" not in captured.err
            assert "should_appear" in captured.err
        finally:
            self._reset_logging_state()
