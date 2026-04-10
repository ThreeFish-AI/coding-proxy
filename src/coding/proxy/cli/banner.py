"""CLI 启动品牌横幅.

使用 Rich Panel 构建紧凑的品牌展示面板，
在服务启动前向终端输出版本与监听地址信息。

设计原则:
- 正交分解: 展示逻辑独立于业务逻辑
- 复用驱动: 使用项目已有的 Rich Console 模式
- 最小干预: 无新依赖，纯组合现有组件
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from rich.panel import Panel
from rich.text import Text

if TYPE_CHECKING:
    from rich.console import Console

from .. import __version__


def build_banner(host: str = "127.0.0.1", port: int = 8046) -> Panel:
    """构建品牌横幅 Panel.

    Args:
        host: 监听地址.
        port: 监听端口.

    Returns:
        配置完成的 Rich Panel 实例.
    """
    brand_text = Text()
    brand_text.append("⚡ ", style="bold")
    brand_text.append("Coding Proxy", style="bold cyan")
    brand_text.append("\n\n")

    info_text = Text()
    info_text.append("Version: ", style="dim")
    info_text.append(__version__, style="green")
    info_text.append("  │  ", style="dim")
    info_text.append("Listening: ", style="dim")
    info_text.append(f"http://{host}:{port}", style="blue")
    brand_text.append(info_text)

    return Panel(brand_text, border_style="cyan", padding=(1, 2), expand=False)


def print_banner(console: Console, host: str = "127.0.0.1", port: int = 8046) -> None:
    """通过指定 Console 打印品牌横幅.

    Args:
        console: Rich Console 实例（复用 CLI 层已有实例）.
        host: 监听地址.
        port: 监听端口.
    """
    console.print()
    console.print(build_banner(host=host, port=port))
    console.print()
