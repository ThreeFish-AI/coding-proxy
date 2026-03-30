"""YAML 配置加载 + 环境变量展开."""

from __future__ import annotations

import os
import re
from pathlib import Path

import yaml

from .schema import ProxyConfig

_ENV_VAR_RE = re.compile(r"\$\{([^}]+)\}")


def _expand_env(value: str) -> str:
    """将 ${VAR} 替换为环境变量值."""
    def _replacer(match: re.Match) -> str:
        var_name = match.group(1)
        return os.environ.get(var_name, match.group(0))
    return _ENV_VAR_RE.sub(_replacer, value)


def _expand_env_recursive(obj):
    """递归展开字典中的环境变量."""
    if isinstance(obj, dict):
        return {k: _expand_env_recursive(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_expand_env_recursive(v) for v in obj]
    if isinstance(obj, str):
        return _expand_env(obj)
    return obj


def load_config(path: Path | None = None) -> ProxyConfig:
    """加载配置文件，合并默认值."""
    if path is None:
        default_path = Path("~/.coding-proxy/config.yaml").expanduser()
        if default_path.exists():
            path = default_path
        else:
            return ProxyConfig()

    if not path.exists():
        return ProxyConfig()

    with open(path) as f:
        raw = yaml.safe_load(f) or {}

    expanded = _expand_env_recursive(raw)
    return ProxyConfig(**expanded)
