"""供应商工具函数测试 — 聚焦 decode_error_body 的编码正确性与流式错误日志回归.

背景：上游 4xx/5xx 错误的流式日志曾将 ``bytes`` 响应体经 ``%s`` 直接格式化，
Python 对 ``bytes`` 走 ``repr()``，导致非 ASCII 的 UTF-8 字节被转义为
``\\xe6\\x82\\xa8`` 之类的不可读序列（中文乱码）。本测试锚定该根因并守护修复。
"""

import logging

import httpx
import pytest

from coding.proxy.config.schema import AnthropicConfig, FailoverConfig
from coding.proxy.model.vendor import decode_error_body
from coding.proxy.vendors.anthropic import AnthropicVendor

# ── 单元测试：decode_error_body ──────────────────────────────

_ZH_TEXT = "您的账户已达到速率限制,请您控制请求频率"


class TestDecodeErrorBody:
    def test_chinese_bytes_decoded_readable(self):
        """中文 UTF-8 bytes 应解码为可读字符,不残留转义序列或 bytes 前缀."""
        raw = f'{{"message":"[1302][{_ZH_TEXT}]"}}'.encode()
        out = decode_error_body(raw)
        assert _ZH_TEXT in out
        assert "\\x" not in out  # 无字节转义
        assert not out.startswith("b'")  # 非 bytes repr
        assert isinstance(out, str)

    def test_truncates_by_characters(self):
        """limit 语义为字符数;超长输入按字符截断."""
        raw = ("你" * 1000).encode()
        out = decode_error_body(raw, limit=100)
        assert len(out) == 100
        assert out == "你" * 100  # 未在多字节边界切断

    def test_invalid_bytes_do_not_raise(self):
        """非法字节以 errors='replace' 降级为占位符,绝不抛异常."""
        raw = b"\xff\xfe invalid"
        out = decode_error_body(raw)
        assert isinstance(out, str)
        assert "�" in out  # U+FFFD replacement character

    def test_empty_bytes(self):
        assert decode_error_body(b"") == ""

    def test_ascii_passthrough(self):
        raw = b'{"error":{"type":"rate_limit_error"}}'
        assert decode_error_body(raw) == '{"error":{"type":"rate_limit_error"}}'

    def test_regression_repr_vs_decode(self):
        """回归对照:复现旧 bug(%s 对 bytes 走 repr)并证明新实现修复之."""
        raw = _ZH_TEXT.encode()
        # 旧行为:bytes 经 %s → repr → 转义字节序列(刻意保留 % 格式化以精确
        # 复现旧 logger.warning("...%s...", error_body) 的缺陷路径,故 noqa UP031)
        assert "\\x" in "%s" % raw  # noqa: UP031
        # 新行为:先解码 → 可读中文,无转义
        assert "\\x" not in decode_error_body(raw)


# ── 集成测试:BaseVendor 流式错误日志路径 ────────────────────


@pytest.mark.asyncio
async def test_stream_error_log_decodes_chinese(caplog):
    """经 BaseVendor.send_message_stream 的流式错误日志应输出可读中文.

    以 AnthropicVendor(纯继承 BaseVendor,无重试/包装)具象化,
    注入挂载 MockTransport 的 AsyncClient 返回 400 + 含中文的 body,
    断言 WARNING 日志文本含中文且不含字节转义序列。
    """
    error_payload = f'{{"error":{{"message":"{_ZH_TEXT}"}}}}'.encode()

    def _handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            400,
            content=error_payload,
            headers={"content-type": "application/json; charset=utf-8"},
        )

    vendor = AnthropicVendor(AnthropicConfig(), FailoverConfig())
    # 注入 MockTransport 客户端(base_url 与真实一致,仅替换 transport)
    vendor._client = httpx.AsyncClient(
        base_url=vendor._base_url,
        transport=httpx.MockTransport(_handler),
    )

    with caplog.at_level(logging.WARNING):
        with pytest.raises(httpx.HTTPStatusError):
            async for _ in vendor.send_message_stream(
                {"model": "claude-opus-4-6", "messages": []},
                {"authorization": "Bearer sk-test"},
            ):
                pass

    await vendor.close()

    stream_errors = [
        r.getMessage() for r in caplog.records if "stream error" in r.getMessage()
    ]
    assert stream_errors, "未捕获到 stream error 日志"
    msg = stream_errors[0]
    assert _ZH_TEXT in msg  # 中文可读
    assert "\\x" not in msg  # 无字节转义
    assert "body=b'" not in msg  # 非 bytes repr
