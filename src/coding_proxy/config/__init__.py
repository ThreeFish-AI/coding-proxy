"""配置模块."""

from .loader import load_config
from .schema import ProxyConfig

__all__ = ["load_config", "ProxyConfig"]
