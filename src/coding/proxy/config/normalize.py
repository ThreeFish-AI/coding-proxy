"""配置原始文档代数：``$tmpl`` 模板节点 + section 合并 / 最小化覆盖.

- 模板：凡含 ``${`` 的字符串统一表示为 ``{"$tmpl": s}``，包内默认与导入数据走同一
  :func:`normalize_raw`；API 写入不推断模板，且拒绝新写入含 ``${`` 的字面量（YAML
  导出无法区分二者，见 :func:`iter_literal_tmpl_locs`）。
- 合并：除 ``vendors`` 外与旧 loader 的 ``_deep_merge`` 完全一致（dict 递归，
  list / 标量整体替换）；``vendors`` 以 vendor 类型为 key 做整条替换。
- 最小化：只持久化与包内默认不同的部分，默认值升级可继续下发。
"""

from __future__ import annotations

import copy
import re
from collections.abc import Iterator, Mapping
from typing import Any

TMPL_KEY = "$tmpl"
ORDER_KEY = "$order"
ABSENT_KEY = "$absent"

_ENV_VAR_RE = re.compile(r"\$\{([^}]+)\}")
_WHOLE_VAR_RE = re.compile(r"^\$\{([^}]+)\}$")

# 存在于全部 vendor dict 中、需从 section 文档剥离的密钥字段（与 secrets 模块对齐）
VENDOR_SECRET_FIELD_NAMES: tuple[str, ...] = (
    "github_token",
    "client_id",
    "client_secret",
    "refresh_token",
    "api_key",
)


class _NoOverride:
    """哨兵：section 与默认值相同，不应存在覆盖行（区别于 JSON ``null`` 覆盖）."""

    _instance: _NoOverride | None = None

    def __new__(cls) -> _NoOverride:
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __repr__(self) -> str:
        return "NO_OVERRIDE"


NO_OVERRIDE = _NoOverride()


# ── $tmpl 模板节点 ──────────────────────────────────────────────


def is_tmpl(value: Any) -> bool:
    return isinstance(value, dict) and len(value) == 1 and TMPL_KEY in value


def whole_env_var(value: Any) -> str | None:
    """``{"$tmpl": "${VAR}"}`` → ``VAR``；其余返回 None."""
    if is_tmpl(value):
        m = _WHOLE_VAR_RE.match(str(value[TMPL_KEY]))
        if m:
            return m.group(1)
    return None


def normalize_raw(obj: Any) -> Any:
    """递归把含 ``${`` 的字符串转换为 ``{"$tmpl": s}``（返回新对象）."""
    if is_tmpl(obj):
        return {TMPL_KEY: obj[TMPL_KEY]}
    if isinstance(obj, dict):
        return {k: normalize_raw(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [normalize_raw(v) for v in obj]
    if isinstance(obj, str) and "${" in obj:
        return {TMPL_KEY: obj}
    return obj


def expand_env_str(value: str, environ: Mapping[str, str]) -> str:
    """与旧 loader 语义一致：``${VAR}`` 替换为环境变量值，未设置时为空串."""
    return _ENV_VAR_RE.sub(lambda m: environ.get(m.group(1), ""), value)


def expand_tmpl(obj: Any, environ: Mapping[str, str]) -> Any:
    """展开全部 ``$tmpl`` 节点（返回新对象，Pydantic 校验前调用）."""
    if is_tmpl(obj):
        return expand_env_str(str(obj[TMPL_KEY]), environ)
    if isinstance(obj, dict):
        return {k: expand_tmpl(v, environ) for k, v in obj.items()}
    if isinstance(obj, list):
        return [expand_tmpl(v, environ) for v in obj]
    return obj


def denormalize(obj: Any) -> Any:
    """``{"$tmpl": s}`` → ``s``（用于导出 YAML）."""
    if is_tmpl(obj):
        return obj[TMPL_KEY]
    if isinstance(obj, dict):
        return {k: denormalize(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [denormalize(v) for v in obj]
    return obj


def iter_tmpl_strings(obj: Any):
    """遍历对象中全部 ``$tmpl`` 字符串."""
    if is_tmpl(obj):
        yield str(obj[TMPL_KEY])
    elif isinstance(obj, dict):
        for v in obj.values():
            yield from iter_tmpl_strings(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from iter_tmpl_strings(v)


def iter_literal_tmpl_locs(obj: Any, loc: list) -> Iterator[list]:
    """含 ``${`` 的普通字符串（非 ``$tmpl`` 节点）所在位置.

    这类字面量无法用 YAML 表达（导出→导入后会变成环境变量展开），因此不允许落库。
    """
    if is_tmpl(obj):
        return
    if isinstance(obj, str):
        if "${" in obj:
            yield loc
    elif isinstance(obj, dict):
        for key, sub in obj.items():
            yield from iter_literal_tmpl_locs(sub, [*loc, key])
    elif isinstance(obj, list):
        for index, sub in enumerate(obj):
            yield from iter_literal_tmpl_locs(sub, [*loc, index])


# ── 合并 ────────────────────────────────────────────────────────


def deep_merge(defaults: dict, override: dict) -> dict:
    """与 legacy ``loader._deep_merge`` 相同的合并语义（返回新 dict）."""
    result = dict(defaults)
    for key, ov in override.items():
        if isinstance(result.get(key), dict) and isinstance(ov, dict):
            result[key] = deep_merge(result[key], ov)
        else:
            result[key] = ov
    return result


def strip_vendor_secrets(vendor: dict) -> dict:
    return {k: v for k, v in vendor.items() if k not in VENDOR_SECRET_FIELD_NAMES}


def vendor_map(vendors: list[dict]) -> dict[str, dict]:
    return {v["vendor"]: v for v in vendors if isinstance(v, dict) and "vendor" in v}


def stored_vendor_types(override: dict | None) -> list[str]:
    if not isinstance(override, dict):
        return []
    return [k for k in override if not k.startswith("$")]


def effective_vendors(default_vendors: list[dict], override: Any) -> list[dict]:
    """按 vendors 覆盖对象计算有效 vendor 列表（深拷贝）."""
    if not isinstance(override, dict):
        return copy.deepcopy(default_vendors)
    defaults_by_type = vendor_map(default_vendors)
    stored = {t: override[t] for t in stored_vendor_types(override)}
    absent = set(override.get(ABSENT_KEY) or [])
    order: list[str] = list(override.get(ORDER_KEY) or [])
    for t in [*defaults_by_type, *stored]:
        if t not in order:
            order.append(t)
    result: list[dict] = []
    for t in order:
        if t in absent:
            continue
        if t in stored:
            result.append({**copy.deepcopy(stored[t]), "vendor": t})
        elif t in defaults_by_type:
            result.append(copy.deepcopy(defaults_by_type[t]))
    return result


def compose_raw(defaults_raw: dict, sections: Mapping[str, Any]) -> dict:
    """包内默认 ⊕ section 覆盖 → 有效原始文档（未展开模板、未注入密钥）."""
    base = {k: v for k, v in defaults_raw.items() if k != "vendors"}
    others = {k: v for k, v in sections.items() if k != "vendors"}
    merged = deep_merge(copy.deepcopy(base), copy.deepcopy(others))
    if "vendors" in defaults_raw or "vendors" in sections:
        merged["vendors"] = effective_vendors(
            defaults_raw.get("vendors") or [], sections.get("vendors")
        )
    return merged


# ── 最小化覆盖 ──────────────────────────────────────────────────


def _dict_diff(default: dict, effective: dict) -> dict:
    diff: dict = {}
    for key, value in effective.items():
        if key not in default:
            diff[key] = copy.deepcopy(value)
            continue
        dv = default[key]
        if isinstance(dv, dict) and isinstance(value, dict):
            sub = _dict_diff(dv, value)
            if sub:
                diff[key] = sub
        elif dv != value or type(dv) is not type(value) and not _num_equal(dv, value):
            diff[key] = copy.deepcopy(value)
    return diff


def _num_equal(a: Any, b: Any) -> bool:
    """int / float 数值相等视为相同（bool 除外，避免 True == 1）."""
    numeric = (int, float)
    if isinstance(a, bool) or isinstance(b, bool):
        return False
    return isinstance(a, numeric) and isinstance(b, numeric) and a == b


def minimize_vendors(default_vendors: list[dict], effective: list[dict]) -> Any:
    """计算 vendors 覆盖对象；与默认一致时返回 :data:`NO_OVERRIDE`.

    入参为已剥离密钥字段的 vendor dict 列表。
    """
    defaults_by_type = {
        t: strip_vendor_secrets(v) for t, v in vendor_map(default_vendors).items()
    }
    default_order = list(defaults_by_type)
    present = [v["vendor"] for v in effective]
    override: dict[str, Any] = {}
    for item in effective:
        t = item["vendor"]
        stripped = strip_vendor_secrets(item)
        if t not in defaults_by_type or stripped != defaults_by_type[t]:
            override[t] = copy.deepcopy(stripped)
    natural = [t for t in [*default_order, *override] if t in present]
    natural = list(dict.fromkeys(natural))
    if natural != present:
        override[ORDER_KEY] = list(present)
    absent = [t for t in default_order if t not in present]
    if absent:
        override[ABSENT_KEY] = absent
    return override or NO_OVERRIDE


def minimize_override(section: str, default_value: Any, effective_value: Any) -> Any:
    """计算 section 的最小覆盖；与默认一致时返回 :data:`NO_OVERRIDE`.

    - ``vendors``：见 :func:`minimize_vendors`；
    - dict section：递归 diff，仅保留与默认不同的键（list 整体比较）；
    - list / 标量 section：不同则整体保存。
    默认值缺失时视为 ``{}``（dict）或无默认（其它）。
    """
    if section == "vendors" and isinstance(effective_value, list):
        return minimize_vendors(default_value or [], effective_value)
    if isinstance(effective_value, dict):
        base = default_value if isinstance(default_value, dict) else None
        if base is None:
            if default_value is None and effective_value == {}:
                return NO_OVERRIDE
            return copy.deepcopy(effective_value)
        diff = _dict_diff(base, effective_value)
        return diff if diff else NO_OVERRIDE
    if effective_value == default_value and type(effective_value) is type(
        default_value
    ):
        return NO_OVERRIDE
    return copy.deepcopy(effective_value)


__all__ = [
    "TMPL_KEY",
    "ORDER_KEY",
    "ABSENT_KEY",
    "NO_OVERRIDE",
    "VENDOR_SECRET_FIELD_NAMES",
    "is_tmpl",
    "whole_env_var",
    "normalize_raw",
    "expand_env_str",
    "expand_tmpl",
    "denormalize",
    "iter_tmpl_strings",
    "iter_literal_tmpl_locs",
    "deep_merge",
    "strip_vendor_secrets",
    "vendor_map",
    "stored_vendor_types",
    "effective_vendors",
    "compose_raw",
    "minimize_vendors",
    "minimize_override",
]
