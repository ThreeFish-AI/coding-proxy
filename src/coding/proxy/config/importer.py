"""旧 YAML → config.db 一次性导入、等价门与启动引导.

- :func:`plan_import`：纯函数，把用户 YAML 的隐式规则物化为显式覆盖
  （legacy flat → vendors、有 vendors 无 tiers → ``tiers: null``），抽取密钥，
  与包内默认做最小化；
- :func:`verify_equivalence`：新解析结果必须与 legacy ``load_config`` 完全相等
  （ProxyConfig ``==``，含 pricing 币种私有属性）；报告只含字段路径，不含值；
- :func:`boot`：``start`` 专用，基于 import_marker 幂等导入，源文件不改名、不改写。
"""

from __future__ import annotations

import copy
import hashlib
import json
import logging
import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ValidationError

from .defaults import packaged_defaults_raw
from .home import (
    config_db_path,
    default_compat_db_path,
    default_token_store_path,
    default_usage_db_path,
    ensure_home,
    legacy_home_yaml_path,
)
from .loader import _LEGACY_FLAT_KEYS, load_config, read_user_raw
from .normalize import (
    NO_OVERRIDE,
    VENDOR_SECRET_FIELD_NAMES,
    deep_merge,
    is_tmpl,
    minimize_override,
    minimize_vendors,
    normalize_raw,
    strip_vendor_secrets,
    vendor_map,
    whole_env_var,
)
from .resolver import (
    MODE_LEGACY,
    ResolvedConfig,
    build_config_from,
    resolve_config,
    strip_secrets,
    validation_errors,
)
from .schema import ProxyConfig
from .secrets import SECTION_SECRET_FIELDS, SecretCipher
from .store import ConfigStore, SecretRecord, StoreSnapshot

logger = logging.getLogger(__name__)

# alias section → ProxyConfig 字段（与 ProxyConfig._migrate_legacy_fields 一致：
# 目标字段同时存在时 alias 被忽略）
ALIAS_TARGETS: dict[str, str] = {"anthropic": "primary", "zhipu": "fallback"}
ALIAS_KEYS: frozenset[str] = frozenset(ALIAS_TARGETS)
REDACTED = "<redacted>"
MARKER_KEY = "import_marker"
_ABSENT = object()


class ImportPlanError(ValueError):
    """导入计划无法生成（消息只含字段路径）."""


class StaleYamlError(RuntimeError):
    """旧版会读取的 YAML 与 config.db 不一致，直接启动会改变生效配置."""

    def __init__(self, path: Path, *, first_boot: bool = False) -> None:
        self.path = path
        self.first_boot = first_boot
        if first_boot:
            message = (
                f"检测到 {path}：旧版会优先读取当前目录下的 config.yaml，但新版不会"
                "自动导入它，直接启动会改变生效的配置。如需使用该文件，请执行 "
                f"`coding-proxy start -c {path}` 或 `coding-proxy config import "
                f"{path}`；若它与 coding-proxy 无关，请重命名该文件或换一个目录启动"
            )
        else:
            message = (
                f"检测到 {path} 与 config.db 中的配置不一致（config.db 已是唯一"
                "配置源，YAML 不再被读取）。如需以该文件为准，请执行 "
                f"`coding-proxy config import {path}`；若该文件已不再使用，请删除"
                "或重命名；也可为该实例设置独立的 CODING_PROXY_HOME"
            )
        super().__init__(message)


class LegacyConfigError(RuntimeError):
    """旧 YAML 本身无法通过校验（与旧版行为一致：拒绝启动）."""

    def __init__(self, path: Path, errors: list[dict]) -> None:
        self.path = path
        self.errors = errors
        locs = ", ".join(".".join(map(str, e["loc"])) or "<root>" for e in errors)
        super().__init__(f"配置文件 {path} 校验失败: {locs}")


class StoreNotReadyError(RuntimeError):
    """配置尚未迁移（存在待导入的 YAML）."""


class ExplicitConfigMissingError(RuntimeError):
    """``-c`` 显式指定的配置文件不存在（fail-fast，不固化空 marker）."""

    def __init__(self, path: Path) -> None:
        self.path = path
        super().__init__(f"配置文件不存在: {path}（`-c` 只指定已有文件，不会创建）")


class StoredConfigInvalidError(RuntimeError):
    """config.db 中的配置无法通过校验（消息只含字段路径）."""


@dataclass
class PlannedSecret:
    kind: str
    env_var: str | None = None
    plaintext: str | None = field(default=None, repr=False)
    keep: bool = False


@dataclass
class ImportPlan:
    source: str
    source_sha256: str
    sections: dict[str, Any] = field(default_factory=dict)
    secrets: dict[str, PlannedSecret] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)

    def secret_records(
        self, existing: Mapping[str, SecretRecord] | None = None
    ) -> dict[str, SecretRecord]:
        records: dict[str, SecretRecord] = {}
        for path, ps in self.secrets.items():
            if ps.keep:
                if existing and path in existing:
                    records[path] = existing[path]
                continue
            records[path] = SecretRecord(kind=ps.kind, env_var=ps.env_var)
        return records

    def plaintexts(self) -> dict[str, str]:
        return {
            path: ps.plaintext
            for path, ps in self.secrets.items()
            if ps.kind == "stored" and ps.plaintext is not None and not ps.keep
        }

    def missing_keep(self, existing: Mapping[str, SecretRecord]) -> list[str]:
        """``<redacted>`` 但本地没有可保留记录的密钥路径."""
        return sorted(
            p for p, ps in self.secrets.items() if ps.keep and p not in existing
        )

    def require_keep_records(self, existing: Mapping[str, SecretRecord]) -> None:
        missing = self.missing_keep(existing)
        if missing:
            raise ImportPlanError(f"{', '.join(missing)}: 本地不存在可保留的密钥")


@dataclass
class BootResult:
    resolved: ResolvedConfig
    notices: list[str] = field(default_factory=list)


def file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


# ── 计划 ────────────────────────────────────────────────────────


def _classify_secret(value: Any, path: str) -> PlannedSecret:
    var = whole_env_var(value)
    if var is not None:
        return PlannedSecret(kind="env", env_var=var)
    if is_tmpl(value):
        raise ImportPlanError(f"{path}: 密钥字段只支持完整的 ${{VAR}} 引用")
    if value == "":
        return PlannedSecret(kind="empty")
    if value == REDACTED:
        return PlannedSecret(kind="stored", keep=True)
    if isinstance(value, str):
        return PlannedSecret(kind="stored", plaintext=value)
    raise ImportPlanError(f"{path}: 密钥字段必须为字符串")


def _secret_source(container: Mapping | None, key: str) -> Any:
    if not isinstance(container, Mapping) or key not in container:
        return _ABSENT
    value = container[key]
    var = whole_env_var(value)
    if var is not None:
        return ("env", var)
    return ("value", json.dumps(value, sort_keys=True, default=str))


def _plan_vendors(
    user_vendors: Any,
    default_vendors: list[dict],
    secrets: dict[str, PlannedSecret],
) -> Any:
    if not isinstance(user_vendors, list):
        raise ImportPlanError("vendors: 必须为列表")
    defaults_by_type = vendor_map(default_vendors)
    seen: set[str] = set()
    stripped_list: list[dict] = []
    for index, vendor in enumerate(user_vendors):
        if not isinstance(vendor, dict) or not isinstance(vendor.get("vendor"), str):
            raise ImportPlanError(f"vendors[{index}]: 缺少 vendor 字段")
        vtype = vendor["vendor"]
        if vtype in seen:
            raise ImportPlanError(f"vendors[{vtype}]: vendor 类型重复")
        seen.add(vtype)
        stripped = strip_vendor_secrets(vendor)
        default_v = defaults_by_type.get(vtype)
        overridden = default_v is None or stripped != strip_vendor_secrets(default_v)
        for field_name in VENDOR_SECRET_FIELD_NAMES:
            path = f"vendors.{vtype}.{field_name}"
            user_src = _secret_source(vendor, field_name)
            if overridden:
                if user_src is not _ABSENT:
                    secrets[path] = _classify_secret(vendor[field_name], path)
                continue
            if user_src == _secret_source(default_v, field_name):
                continue
            if user_src is _ABSENT:
                secrets[path] = PlannedSecret(kind="empty")
            else:
                secrets[path] = _classify_secret(vendor[field_name], path)
        stripped_list.append(stripped)
    return minimize_vendors(default_vendors, stripped_list)


def _drop_derived_paths(user: dict) -> None:
    rules = (
        ("database", "path", default_usage_db_path()),
        ("database", "compat_state_path", default_compat_db_path()),
        ("auth", "token_store_path", default_token_store_path()),
    )
    for section, key, derived in rules:
        container = user.get(section)
        if not isinstance(container, dict):
            continue
        value = container.get(key)
        if isinstance(value, str) and Path(value).expanduser() == derived:
            container.pop(key)


def plan_from_raw(
    user_raw: Any,
    *,
    source: str = "",
    source_sha256: str = "",
    defaults_raw: dict | None = None,
) -> ImportPlan:
    """由用户 YAML 原始字典生成导入计划（纯函数，不写盘）."""
    if not isinstance(user_raw, dict):
        raise ImportPlanError("配置文件顶层必须为映射")
    defaults_raw = packaged_defaults_raw() if defaults_raw is None else defaults_raw
    plan = ImportPlan(source=source, source_sha256=source_sha256)

    known = set(ProxyConfig.model_fields) | ALIAS_KEYS
    unknown = sorted(str(k) for k in user_raw if k not in known)
    if unknown:
        plan.warnings.append(f"忽略未知顶层字段: {', '.join(unknown)}")
        logger.warning("导入时忽略未知顶层字段: %s", unknown)
    if user_raw and len(unknown) == len(user_raw):
        raise ImportPlanError("未识别到任何 coding-proxy 配置字段")
    user = normalize_raw(
        {k: v for k, v in copy.deepcopy(user_raw).items() if k in known}
    )
    try:
        json.dumps(user, allow_nan=False)
    except (TypeError, ValueError):
        raise ImportPlanError("配置包含无法保存的值类型（如日期）") from None

    # ── vendors / tiers：把旧 loader 的隐式规则物化为显式值 ─────────
    legacy = any(k in user for k in _LEGACY_FLAT_KEYS)
    user_vendors: Any = _ABSENT
    tiers_value: Any = _ABSENT
    if legacy:
        base = {k: v for k, v in defaults_raw.items() if k not in ("vendors", "tiers")}
        merged = deep_merge(copy.deepcopy(base), copy.deepcopy(user))
        if "vendors" in user and "tiers" not in user:
            merged.pop("tiers", None)
        migrated = ProxyConfig._migrate_legacy_fields(merged)
        user_vendors = migrated.get("vendors", [])
        tiers_value = migrated.get("tiers")
    else:
        if "vendors" in user:
            user_vendors = user["vendors"]
            tiers_value = user.get("tiers")
        elif "tiers" in user:
            tiers_value = user["tiers"]

    if user_vendors is not _ABSENT:
        vendors_override = _plan_vendors(
            user_vendors, defaults_raw.get("vendors") or [], plan.secrets
        )
        if vendors_override is not NO_OVERRIDE:
            plan.sections["vendors"] = vendors_override
    if tiers_value is not _ABSENT:
        tiers_override = minimize_override(
            "tiers", defaults_raw.get("tiers"), copy.deepcopy(tiers_value)
        )
        if tiers_override is not NO_OVERRIDE:
            plan.sections["tiers"] = tiers_override

    # ── 非 vendors 密钥抽取（含 legacy / alias section 中的全部 vendor 密钥字段） ──
    # 注入不依赖覆盖行（resolver.section_container），仅含密钥的 section 不保留空行
    for section, fields in SECTION_SECRET_FIELDS.items():
        container = user.get(section)
        if not isinstance(container, dict):
            continue
        for field_name in fields:
            if field_name in container:
                path = f"{section}.{field_name}"
                plan.secrets[path] = _classify_secret(container.pop(field_name), path)

    _drop_derived_paths(user)

    for section, value in user.items():
        if section in ("vendors", "tiers"):
            continue
        if section == "session_policies" and isinstance(value, list):
            # list 形态（YAML 注释示例引导的合法写法）归一为 dict，避免以 list 落库
            value = {"policies": value}
        override = minimize_override(section, defaults_raw.get(section), value)
        if override is not NO_OVERRIDE:
            plan.sections[section] = override

    plan.sections = json.loads(json.dumps(plan.sections, ensure_ascii=False))
    return plan


def plan_import(source: Path, *, defaults_raw: dict | None = None) -> ImportPlan:
    """读取 YAML 并生成导入计划（不写盘）."""
    try:
        raw = read_user_raw(source)
    except Exception as exc:
        raise ImportPlanError(f"无法读取 {source}: {type(exc).__name__}") from None
    return plan_from_raw(
        raw,
        source=str(source),
        source_sha256=file_sha256(source),
        defaults_raw=defaults_raw,
    )


# ── 等价门 ──────────────────────────────────────────────────────


def _expand_paths(cfg: ProxyConfig) -> ProxyConfig:
    def _exp(value: str) -> str:
        return str(Path(value).expanduser()) if value else value

    database = cfg.database.model_copy(
        update={
            "path": _exp(cfg.database.path),
            "compat_state_path": _exp(cfg.database.compat_state_path),
        }
    )
    auth = cfg.auth.model_copy(
        update={"token_store_path": _exp(cfg.auth.token_store_path)}
    )
    return cfg.model_copy(update={"database": database, "auth": auth})


def _blank_secret_paths(
    cfg: ProxyConfig, paths: set[str], present: frozenset[str] = frozenset()
) -> ProxyConfig:
    """把密钥路径对应的字段置空；``present`` 为源 YAML 顶层键（判断 alias 是否生效）."""
    if not paths:
        return cfg
    cfg = cfg.model_copy(deep=True)
    for path in paths:
        parts = path.split(".")
        if parts[0] == "vendors" and len(parts) == 3:
            for vendor in cfg.vendors:
                if vendor.vendor == parts[1]:
                    setattr(vendor, parts[2], "")
        elif len(parts) == 2:
            attr = ALIAS_TARGETS.get(parts[0], parts[0])
            if attr in present and attr != parts[0]:
                continue  # 目标字段已存在，alias 被 ProxyConfig 忽略
            target = getattr(cfg, attr, None)
            if isinstance(target, BaseModel) and hasattr(target, parts[1]):
                setattr(target, parts[1], "")
    return cfg


def config_diff_paths(old: ProxyConfig, new: ProxyConfig) -> list[str]:
    """两份配置的差异字段路径（路径字段先展开 ``~``；只含路径，不含值）."""
    diffs = _diff_paths(_expand_paths(old), _expand_paths(new), "")
    return [p.lstrip(".") for p in dict.fromkeys(diffs)]


def _diff_paths(a: Any, b: Any, path: str) -> list[str]:
    if a == b:
        return []
    if isinstance(a, BaseModel) and isinstance(b, BaseModel) and type(a) is type(b):
        diffs: list[str] = []
        for name in type(a).model_fields:
            diffs += _diff_paths(getattr(a, name), getattr(b, name), f"{path}.{name}")
        if a.__pydantic_private__ != b.__pydantic_private__:
            diffs.append(f"{path}.<private>")
        if (a.model_extra or {}) != (b.model_extra or {}):
            diffs.append(f"{path}.<extra>")
        return diffs or [path]
    if isinstance(a, list) and isinstance(b, list) and len(a) == len(b):
        diffs = []
        for index, (x, y) in enumerate(zip(a, b, strict=True)):
            label = getattr(x, "vendor", index) if path == ".vendors" else index
            diffs += _diff_paths(x, y, f"{path}[{label}]")
        return diffs or [path]
    if isinstance(a, dict) and isinstance(b, dict):
        diffs = []
        for key in sorted(set(a) | set(b), key=str):
            diffs += _diff_paths(a.get(key), b.get(key), f"{path}.{key}")
        return diffs or [path]
    return [path]


def verify_equivalence(
    plan: ImportPlan,
    source: Path,
    environ: Mapping[str, str] | None = None,
    *,
    defaults_raw: dict | None = None,
    existing: StoreSnapshot | None = None,
) -> list[str]:
    """比较「导入后解析结果」与 legacy ``load_config(source)``，返回差异字段路径.

    ``<redacted>``（保留既有记录）的密钥字段不参与比较；legacy 本身校验失败时抛
    :class:`LegacyConfigError`。
    """
    environ = os.environ if environ is None else environ
    defaults_raw = packaged_defaults_raw() if defaults_raw is None else defaults_raw
    try:
        old = load_config(source)
    except ValidationError as exc:
        raise LegacyConfigError(source, validation_errors(exc)) from None
    keep = {p for p, ps in plan.secrets.items() if ps.keep}
    present = frozenset(read_user_raw(source)) if keep else frozenset()
    records = plan.secret_records(existing.secrets if existing else None)
    try:
        new = build_config_from(
            defaults_raw,
            plan.sections,
            records,
            None,
            environ,
            decrypt=False,
            plaintexts=plan.plaintexts(),
        )
    except ValidationError as exc:
        return [
            "." + ".".join(map(str, e["loc"])) if e["loc"] else "<root>"
            for e in validation_errors(exc)
        ]
    return config_diff_paths(
        _blank_secret_paths(old, keep, present),
        _blank_secret_paths(new, keep, present),
    )


# ── 写入 ────────────────────────────────────────────────────────


def make_marker(source: Path | None, sha256: str | None) -> dict:
    from .. import __version__

    return {
        "source": str(source) if source is not None else None,
        "sha256": sha256,
        "version": __version__,
        "imported_at": datetime.now(UTC).isoformat(timespec="seconds"),
    }


def apply_import(
    store: ConfigStore,
    cipher: SecretCipher,
    plan: ImportPlan,
    marker: dict,
    *,
    replace: bool = True,
) -> None:
    """单事务写入 sections / secrets / import_marker（``replace`` 时清除计划外的覆盖）."""
    with store.transaction() as txn:
        current = txn.snapshot
        if replace:
            for section in list(current.sections):
                if section not in plan.sections:
                    txn.delete_section(section)
        for section, data in plan.sections.items():
            if section not in current.sections or current.sections[section] != data:
                txn.put_section(section, data)
        if any(ps.kind == "stored" and not ps.keep for ps in plan.secrets.values()):
            cipher.ensure_for_write(txn)
        for path, ps in plan.secrets.items():
            if ps.keep:
                continue
            if ps.kind == "stored":
                record = cipher.make_record(ps.plaintext or "")
            else:
                record = SecretRecord(kind=ps.kind, env_var=ps.env_var)
            txn.put_secret(path, record)
        if replace:
            for path in list(current.secrets):
                if path not in plan.secrets:
                    txn.put_secret(path, None)
        txn.set_meta(MARKER_KEY, json.dumps(marker, ensure_ascii=False))


def read_marker(snap: StoreSnapshot) -> dict | None:
    raw = snap.meta.get(MARKER_KEY)
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


# ── 启动引导 ────────────────────────────────────────────────────


def _legacy_readonly(source: Path, errors: list[dict]) -> ResolvedConfig:
    try:
        cfg = load_config(source)
    except ValidationError as exc:
        raise LegacyConfigError(source, validation_errors(exc)) from None
    effective = _legacy_effective_raw(source)
    return ResolvedConfig(
        config=cfg,
        effective_raw=strip_secrets(effective),
        mode=MODE_LEGACY,
        errors=errors,
    )


def _legacy_effective_raw(source: Path) -> dict:
    """复刻旧 loader 的合并结果（未展开模板），仅用于只读展示."""
    user_raw = normalize_raw(read_user_raw(source) or {})
    defaults = packaged_defaults_raw()
    if any(k in user_raw for k in _LEGACY_FLAT_KEYS):
        defaults.pop("vendors", None)
        defaults.pop("tiers", None)
    merged = deep_merge(defaults, user_raw)
    if "vendors" in user_raw and "tiers" not in user_raw:
        merged.pop("tiers", None)
    return merged


def _initial_source(explicit_path: Path | None) -> Path | None:
    if explicit_path is not None:
        # 显式 -c 不做内容过滤：缺失即 fail-fast，内容由等价门把关
        return explicit_path if explicit_path.is_file() else None
    home_yaml = legacy_home_yaml_path()
    # 与 cwd 候选同口径：无关 YAML 不作为待导入源（避免永久 legacy_readonly 写锁）
    if home_yaml.is_file() and _is_proxy_yaml(home_yaml):
        return home_yaml
    return None


def _is_proxy_yaml(path: Path) -> bool:
    """文件是否为 coding-proxy 配置（顶层至少一个 ProxyConfig 字段或 alias）."""
    try:
        raw = read_user_raw(path)
    except Exception:
        return False
    known = set(ProxyConfig.model_fields) | ALIAS_KEYS
    return isinstance(raw, dict) and any(k in known for k in raw)


def _same_file(a: Path | None, b: Path | None) -> bool:
    return a is not None and b is not None and a.resolve() == b.resolve()


def _check_yaml_sources(
    explicit_path: Path | None,
    *,
    expected_sha: str | None,
    imported_from: Path | None,
    first_boot: bool,
) -> list[str]:
    """检查旧 YAML 与 config.db 是否一致；需拒绝启动时抛 :class:`StaleYamlError`.

    - 指定 ``-c``：只检查该文件（旧版 ``-c`` 替换整个搜索链）；
    - 未指定 ``-c``：旧版会读取的文件（含 coding-proxy 字段的 ./config.yaml，否则
      home 下 config.yaml）在首启、无导入源或其即导入源时必须一致；其余候选
      （含导入源本身）只提示；不含 coding-proxy 字段的文件一律忽略。
    """
    if explicit_path is not None:
        strict = explicit_path if explicit_path.is_file() else None
        others: list[Path] = []
    else:
        cwd_yaml, home_yaml = Path("config.yaml"), legacy_home_yaml_path()
        legacy_read = next(
            (p for p in (cwd_yaml, home_yaml) if p.is_file() and _is_proxy_yaml(p)),
            None,
        )
        strict = (
            legacy_read
            if first_boot
            or imported_from is None
            or _same_file(legacy_read, imported_from)
            else None
        )
        others = []
        for path in () if first_boot else (cwd_yaml, home_yaml, imported_from):
            if (
                path is not None
                and path.is_file()
                and not _same_file(path, strict)
                and not any(_same_file(path, o) for o in others)
                and _is_proxy_yaml(path)
            ):
                others.append(path)

    notices: list[str] = []
    if strict is not None:
        if file_sha256(strict) != expected_sha:
            raise StaleYamlError(strict, first_boot=first_boot)
        if not first_boot:
            notices.append(_migrated_notice(strict))
    for path in others:
        if file_sha256(path) == expected_sha:
            notices.append(_migrated_notice(path))
        else:
            notices.append(
                f"{path} 与 config.db 中的配置不一致，且不再被读取；确认无用后请删除"
                "或重命名该文件（如需以它为准，可执行 "
                f"`coding-proxy config import {path}`）"
            )
    return notices


def _migrated_notice(path: Path) -> str:
    return (
        f"{path} 已迁移至 config.db，不再被读取；"
        "确认无误后可删除该文件（可能含明文密钥）"
    )


def boot(
    explicit_path: Path | None = None,
    *,
    environ: Mapping[str, str] | None = None,
) -> BootResult:
    """``start`` 的配置引导：必要时一次性导入，然后解析运行时配置."""
    if explicit_path is not None and not explicit_path.is_file():
        # 显式输入缺失即报错：静默以包内默认启动会把一次手误持久化为「已迁移」
        raise ExplicitConfigMissingError(explicit_path)
    environ = os.environ if environ is None else environ
    store = ConfigStore(config_db_path())
    snap = store.snapshot()
    marker = read_marker(snap)
    notices: list[str] = []

    if marker is None:
        source = _initial_source(explicit_path)
        # ./config.yaml 从不自动导入：旧版会读取它且内容不同则在写盘前拒绝启动
        _check_yaml_sources(
            explicit_path,
            expected_sha=file_sha256(source) if source else None,
            imported_from=source,
            first_boot=True,
        )
        cipher = SecretCipher(environ=environ)
        if source is None:
            ensure_home()
            plan = ImportPlan(source="", source_sha256="")
            apply_import(store, cipher, plan, make_marker(None, None), replace=False)
        else:
            errors: list[dict] = []
            try:
                plan = plan_import(source)
                plan.require_keep_records(snap.secrets)
                diffs = verify_equivalence(plan, source, environ, existing=snap)
                errors = [{"loc": [p], "msg": "import mismatch"} for p in diffs]
            except ImportPlanError as exc:
                errors = [{"loc": ["<import>"], "msg": str(exc)}]
            if errors:
                logger.error(
                    "配置导入未通过等价校验，字段: %s", [e["loc"][0] for e in errors]
                )
                resolved = _legacy_readonly(source, errors)
                notices.append(
                    f"{source} 导入未通过等价校验，本次以只读方式运行旧 YAML；"
                    f"可执行 `coding-proxy config import {source} --dry-run` 查看差异"
                )
                return BootResult(resolved=resolved, notices=notices)
            ensure_home()
            apply_import(
                store, cipher, plan, make_marker(source.resolve(), plan.source_sha256)
            )
            notices.append(
                f"已将 {source} 导入 config.db；该文件不再被读取，"
                "确认无误后可删除（可能含明文密钥）"
            )
            notices.extend(plan.warnings)
    else:
        imported_from = marker.get("source")
        notices.extend(
            _check_yaml_sources(
                explicit_path,
                expected_sha=marker.get("sha256"),
                imported_from=Path(imported_from) if imported_from else None,
                first_boot=False,
            )
        )

    resolved = resolve_config(environ=environ)
    return BootResult(resolved=resolved, notices=notices)


def require_migrated_or_empty() -> bool:
    """已迁移返回 True；无待导入 YAML 返回 False；存在待导入 YAML 时抛错."""
    if read_marker(ConfigStore(config_db_path()).snapshot()) is not None:
        return True
    if legacy_home_yaml_path().is_file():
        raise StoreNotReadyError(
            f"配置尚未迁移：请先运行 `coding-proxy start`，或执行 "
            f"`coding-proxy config import {legacy_home_yaml_path()}`"
        )
    cwd_yaml = Path("config.yaml")
    if cwd_yaml.is_file() and _is_proxy_yaml(cwd_yaml):
        # 与 start 的首启门槛一致：旧版会读取该文件，先放行写入会被随后的导入整体丢弃
        raise StoreNotReadyError(
            f"配置尚未迁移：请先运行 `coding-proxy start -c {cwd_yaml}`，或执行 "
            f"`coding-proxy config import {cwd_yaml}`"
        )
    return False


def ensure_store_ready() -> None:
    """CLI 写操作前置：已迁移则直接返回；无待导入源时初始化空 store."""
    if require_migrated_or_empty():
        return
    store = ConfigStore(config_db_path())
    ensure_home()
    apply_import(
        store,
        SecretCipher(),
        ImportPlan(source="", source_sha256=""),
        make_marker(None, None),
        replace=False,
    )


def load_offline_config(*, decrypt: bool) -> tuple[ProxyConfig, str | None]:
    """离线命令（usage / auth）的配置读取：已迁移读 store，否则只读读取旧 YAML."""
    store = ConfigStore(config_db_path())
    if read_marker(store.snapshot()) is None and any(
        p.is_file() for p in (Path("config.yaml"), legacy_home_yaml_path())
    ):
        hint = (
            "提示：配置尚未迁移至 config.db，当前只读使用 YAML；"
            "运行 `coding-proxy start` 或 `coding-proxy config import <file>` 完成迁移"
        )
        return load_config(None), hint
    resolved = resolve_config(decrypt=decrypt)
    if resolved.config is None:
        locs = ", ".join(".".join(map(str, e["loc"])) for e in resolved.errors)
        raise StoredConfigInvalidError(
            f"存储的配置无效: {locs}；请用 `coding-proxy config set` / "
            "`coding-proxy config reset <section>` 修复"
        )
    return resolved.config, None


__all__ = [
    "ALIAS_KEYS",
    "ALIAS_TARGETS",
    "REDACTED",
    "ImportPlan",
    "PlannedSecret",
    "ImportPlanError",
    "StaleYamlError",
    "LegacyConfigError",
    "StoreNotReadyError",
    "ExplicitConfigMissingError",
    "StoredConfigInvalidError",
    "BootResult",
    "plan_import",
    "plan_from_raw",
    "verify_equivalence",
    "config_diff_paths",
    "apply_import",
    "make_marker",
    "read_marker",
    "boot",
    "ensure_store_ready",
    "require_migrated_or_empty",
    "load_offline_config",
    "file_sha256",
]
