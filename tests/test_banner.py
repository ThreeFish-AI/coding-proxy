"""CLI 品牌横幅测试."""

from unittest.mock import MagicMock

import pytest
from rich.panel import Panel

from coding.proxy.cli.banner import build_banner, print_banner


# ── build_banner() 测试 ────────────────────────────────────────


class TestBuildBanner:
    """验证 build_banner() 返回正确的 Panel 结构与内容."""

    def test_returns_panel_instance(self):
        """返回值应为 Rich Panel 实例."""
        panel = build_banner()
        assert isinstance(panel, Panel)

    def test_contains_brand_name(self):
        """横幅应包含品牌名称 'Coding Proxy'."""
        panel = build_banner()
        plain = panel.renderable.plain
        assert "Coding Proxy" in plain

    def test_contains_version(self):
        """横幅应包含当前版本号."""
        from coding.proxy import __version__

        panel = build_banner()
        plain = panel.renderable.plain
        assert __version__ in plain

    def test_default_host_port(self):
        """默认参数应使用 127.0.0.1:8046."""
        panel = build_banner()
        plain = panel.renderable.plain
        assert "127.0.0.1" in plain
        assert "8046" in plain

    def test_custom_host_port(self):
        """自定义 host/port 应正确渲染至横幅."""
        panel = build_banner(host="0.0.0.0", port=9090)
        plain = panel.renderable.plain
        assert "0.0.0.0" in plain
        assert "9090" in plain

    def test_contains_listening_url(self):
        """横幅应包含完整的监听 URL 格式."""
        panel = build_banner(host="192.168.1.1", port=3000)
        plain = panel.renderable.plain
        assert "http://192.168.1.1:3000" in plain


# ── print_banner() 测试 ───────────────────────────────────────


class TestPrintBanner:
    """验证 print_banner() 通过 Console 输出."""

    def test_prints_to_console(self):
        """print_banner 应调用 console.print()."""
        mock_console = MagicMock()
        print_banner(mock_console)
        assert mock_console.print.called

    def test_print_called_multiple_times(self):
        """print_banner 应调用 console.print() 多次（空行 + Panel + 空行）."""
        mock_console = MagicMock()
        print_banner(mock_console)
        # 预期调用次数：3 次（空行 + banner + 空行）
        assert mock_console.print.call_count == 3

    def test_print_includes_panel(self):
        """print_banner 的某次调用应传入 Panel 实例."""
        mock_console = MagicMock()
        print_banner(mock_console)
        call_args_list = mock_console.print.call_args_list
        has_panel = any(
            isinstance(call[0][0], Panel) for call in call_args_list if call[0]
        )
        assert has_panel
