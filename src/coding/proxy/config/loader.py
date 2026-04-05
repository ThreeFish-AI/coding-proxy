"""YAML 配置加载 + 环境变量展开 + 示例配置深度合并."""

from __future__ import annotations

import logging
import os
import re
from pathlib import Path

import yaml

from .schema import ProxyConfig

logger = logging.getLogger(__name__)

_ENV_VAR_RE = re.compile(r"\$\{([^}]+)\}")

# ── Legacy flat 格式字段集合（用于检测旧配置，避免与 example vendors 冲突） ──
_LEGACY_FLAT_KEYS: frozenset[str] = frozenset(
    {
        "primary",
        "copilot",
        "antigravity",
        "fallback",
        "circuit_breaker",
        "copilot_circuit_breaker",
        "antigravity_circuit_breaker",
        "quota_guard",
        "copilot_quota_guard",
        "antigravity_quota_guard",
    }
)


def _expand_env(value: str) -> str:
    """将 ${VAR} 替换为环境变量值."""

    def _replacer(match: re.Match) -> str:
        var_name = match.group(1)
        return os.environ.get(var_name, "")

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


def _deep_merge(defaults: dict, override: dict) -> dict:
    """深度合并两个字典.

    合并策略：
    - dict + dict → 递归合并子键（支持部分覆盖嵌套配置）
    - list         → override 完整替换 default（有序集合，顺序敏感）
    - 标量         → override 替换 default
    - override 中不存在于 defaults 的新键直接添加

    Args:
        defaults: 基础字典（通常来自 config.example.yaml）
        override: 覆盖字典（来自用户配置文件）

    Returns:
        合并后的新字典
    """
    result = dict(defaults)
    for key, ov in override.items():
        if key not in result:
            result[key] = ov
        elif isinstance(result.get(key), dict) and isinstance(ov, dict):
            result[key] = _deep_merge(result[key], ov)
        else:
            result[key] = ov
    return result


def _get_example_config_path() -> Path | None:
    """定位 config.example.yaml 文件路径.

    搜索策略：从 loader.py 所在目录向上回溯到项目根目录查找。
    路径链：config/ → proxy/ → coding/ → src/ → project_root

    Returns:
        文件路径对象，未找到时返回 None（触发降级至 Pydantic 默认值）
    """
    current = Path(__file__).resolve().parent
    # 先检查当前层，再逐级向上（共检查 5 层：config/ ~ project_root/）
    for _ in range(5):
        candidate = current / "config.example.yaml"
        if candidate.is_file():
            return candidate
        current = current.parent
    logger.debug("未找到 config.example.yaml，将使用 Pydantic 默认值")
    return None


def load_config(path: Path | None = None) -> ProxyConfig:
    """加载配置文件，以 config.example.yaml 为基础进行深度合并.

    加载优先级（低→高）：
    1. config.example.yaml 内置完整默认值
    2. 用户配置文件（CWD/config.yaml > ~/.coding-proxy/config.yaml > -c 指定路径）

    环境变量展开（${VAR}）在深度合并之后执行，确保用户可通过环境变量覆盖任意字段。
    """
    # ── 第 1 步：确定并加载用户配置 ─────────────────────────────
    user_raw: dict = {}
    if path is None:
        candidates = [
            Path("config.yaml"),
            Path("~/.coding-proxy/config.yaml").expanduser(),
        ]
        for candidate in candidates:
            if candidate.exists():
                path = candidate
                break

    if path and path.exists():
        with open(path) as f:
            user_raw = yaml.safe_load(f) or {}

    # ── 第 2 步：加载示例默认配置 ─────────────────────────────
    example_path = _get_example_config_path()
    if example_path is None:
        # 降级：无示例文件时使用纯 Pydantic 默认值（向后兼容）
        expanded = _expand_env_recursive(user_raw)
        return ProxyConfig(**expanded)

    with open(example_path) as f:
        defaults = yaml.safe_load(f) or {}

    # ── Legacy 兼容：旧 flat 格式用户配置不应继承 example 的 vendors ──
    # 当用户使用 legacy 字段时，移除 defaults 中的 vendors，
    # 让 ProxyConfig._migrate_legacy_fields 迁移器正常接管 vendors 构建
    if any(k in user_raw for k in _LEGACY_FLAT_KEYS):
        defaults.pop("vendors", None)

    # ── 第 3 步：深度合并 ─────────────────────────────────────
    merged = _deep_merge(defaults, user_raw)

    # ── 第 4 步：环境变量展开（必须在合并之后） ────────────────
    expanded = _expand_env_recursive(merged)

    return ProxyConfig(**expanded)
