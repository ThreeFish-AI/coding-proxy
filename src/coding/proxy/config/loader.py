"""YAML 配置加载 + 环境变量展开 + 默认配置深度合并."""

from __future__ import annotations

import logging
import os
import re
from pathlib import Path

import yaml

from .schema import ProxyConfig

logger = logging.getLogger(__name__)

_ENV_VAR_RE = re.compile(r"\$\{([^}]+)\}")

# ── Legacy flat 格式字段集合（用于检测旧配置，避免与 default vendors 冲突） ──
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
        defaults: 基础字典（通常来自 config.default.yaml）
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


def _get_default_config_path() -> Path | None:
    """定位 config.default.yaml 文件路径.

    查找策略（按优先级）：
    1. 包内资源（importlib.resources）— 适用于 pip/uv 正式安装
    2. 源码树回溯（loader.py → 项目根目录）— 适用于 editable 开发安装

    Returns:
        文件路径对象，未找到时返回 None（触发降级至 Pydantic 默认值）
    """
    # 策略 1：包内资源查找（覆盖所有安装方式）
    try:
        from importlib.resources import files as _pkg_files

        pkg_data = _pkg_files("coding.proxy.config")
        candidate = pkg_data / "config.default.yaml"
        if candidate.is_file():
            return Path(candidate)
    except Exception:
        pass

    # 策略 2：源码树回溯（保留 editable 开发兼容性）
    current = Path(__file__).resolve().parent
    # 先检查当前层，再逐级向上（共检查 5 层：config/ ~ project_root/）
    for _ in range(5):
        candidate = current / "config.default.yaml"
        if candidate.is_file():
            return candidate
        current = current.parent

    logger.warning(
        "未找到 config.default.yaml，将使用 Pydantic 默认值。"
        "这可能导致 pricing（定价）、vendors（供应商）等字段为空。"
    )
    return None


def _ensure_user_config() -> Path | None:
    """确保 ~/.coding-proxy/config.yaml 存在（不存在则从 default 复制）.

    首次运行时自动将 config.default.yaml 拷贝到用户目录，
    作为用户可编辑的配置基础。幂等、非破坏性、优雅降级。

    Returns:
        创建/已存在的配置文件路径，失败时返回 None。
    """
    import shutil

    _home_config = Path("~/.coding-proxy/config.yaml").expanduser()
    _cwd_config = Path("config.yaml")

    # 已有配置 → 直接返回（不覆盖）
    if _cwd_config.exists():
        return _cwd_config
    if _home_config.exists():
        return _home_config

    # 无配置 → 从 default 复制
    default_path = _get_default_config_path()
    if default_path is None:
        logger.warning("无法定位 config.default.yaml，跳过用户配置初始化。")
        return None

    try:
        _home_config.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(default_path, _home_config)
        logger.info("已初始化用户配置文件: %s", _home_config)
        return _home_config
    except OSError as exc:
        logger.warning("无法创建用户配置文件 %s: %s", _home_config, exc)
        return None


def _log_merge_diagnostics(defaults: dict, user_raw: dict, merged: dict) -> None:
    """记录合并诊断信息，帮助排查配置缺失问题."""
    critical_fields = {
        "pricing": "模型定价（Usage Cost 计算）",
        "vendors": "供应商定义（服务启动必需）",
        "model_mapping": "模型映射规则",
    }
    for field, desc in critical_fields.items():
        in_defaults = field in defaults and len(defaults.get(field, [])) > 0
        in_merged = field in merged and len(merged.get(field, [])) > 0
        if in_defaults and not in_merged:
            logger.warning(
                "配置合并后 %s 为空（%s）。"
                "config.default.yaml 有 %d 条默认值，用户%s提供该字段。",
                field,
                desc,
                len(defaults.get(field, [])),
                "显式" if field in user_raw else "未",
            )


def load_config(path: Path | None = None) -> ProxyConfig:
    """加载配置文件，以 config.default.yaml 为基础进行深度合并.

    加载优先级（低→高）：
    1. config.default.yaml 内置完整默认值
    2. 用户配置文件（CWD/config.yaml > ~/.coding-proxy/config.yaml > -c 指定路径）

    环境变量展开（${VAR}）在深度合并之后执行，确保用户可通过环境变量覆盖任意字段。
    """
    # ── 第 0 步：首次运行自动初始化用户配置文件 ─────────────
    # 仅在未指定显式路径时触发（用户通过 -c 显式指定时不干预）
    if path is None:
        _ensure_user_config()

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

    # ── 第 2 步：加载默认配置 ────────────────────────────────
    default_path = _get_default_config_path()
    if default_path is None:
        # 降级：无默认文件时使用纯 Pydantic 默认值（向后兼容）
        expanded = _expand_env_recursive(user_raw)
        return ProxyConfig(**expanded)

    with open(default_path) as f:
        defaults = yaml.safe_load(f) or {}

    # ── Legacy 兼容：旧 flat 格式用户配置不应继承 default 的 vendors/tiers ──
    # 当用户使用 legacy 字段时，移除 defaults 中的 vendors 和 tiers，
    # 让 ProxyConfig._migrate_legacy_fields 迁移器正常接管 vendors 构建，
    # 并避免 default tiers 引用迁移后不存在的 vendor 导致校验失败。
    if any(k in user_raw for k in _LEGACY_FLAT_KEYS):
        defaults.pop("vendors", None)
        defaults.pop("tiers", None)

    # ── 第 3 步：深度合并 ─────────────────────────────────────
    merged = _deep_merge(defaults, user_raw)

    # ── 防止 default tiers 泄漏到自定义 vendors 配置中 ───────────
    # 当用户显式定义了 vendors 但未定义 tiers 时，
    # 继承的 default tiers 可能引用用户未配置的 vendor，导致校验失败。
    # 此时移除 tiers，回退到 vendors 列表原始顺序作为优先级。
    if "vendors" in user_raw and "tiers" not in user_raw:
        merged.pop("tiers", None)

    # ── 诊断日志：关键字段合并结果校验 ────────────────────────
    _log_merge_diagnostics(defaults, user_raw, merged)

    # ── 第 4 步：环境变量展开（必须在合并之后） ────────────────
    expanded = _expand_env_recursive(merged)

    return ProxyConfig(**expanded)
