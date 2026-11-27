"""配置读写服务（与框架无关，HTTP 与 CLI 共用）.

写入流程（乐观并发）：读快照并校验 revision → 边界校验（仅针对有变化的值）→
计算新的有效文档与密钥记录 → 构建 ProxyConfig（错误不含 input）→ 事务外 preflight →
``BEGIN IMMEDIATE`` 内确认存储自快照后未变（否则重算，最多 3 次）→ 最小化写入 → 提交。
日志只记录 section 名与密钥路径，从不记录值。
"""

from __future__ import annotations

import copy
import json
import logging
import math
import os
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, get_args, get_origin
from urllib.parse import urlsplit

import yaml
from pydantic import BaseModel, ValidationError

from .defaults import packaged_defaults_raw
from .home import config_db_path, ensure_home
from .importer import (
    ALIAS_KEYS,
    REDACTED,
    ImportPlan,
    ImportPlanError,
    apply_import,
    config_diff_paths,
    make_marker,
    plan_import,
    verify_equivalence,
)
from .loader import _LEGACY_FLAT_KEYS
from .normalize import (
    NO_OVERRIDE,
    TMPL_KEY,
    VENDOR_SECRET_FIELD_NAMES,
    compose_raw,
    deep_merge,
    denormalize,
    is_tmpl,
    iter_literal_tmpl_locs,
    iter_tmpl_strings,
    minimize_override,
    stored_vendor_types,
    strip_vendor_secrets,
    vendor_map,
    whole_env_var,
)
from .resolver import (
    MODE_LEGACY,
    ResolvedConfig,
    build_config_from,
    resolve_config,
    section_container,
    validation_errors,
)
from .routing import VendorConfig
from .schema import ProxyConfig
from .secrets import (
    SECRET_FIELDS_BY_VENDOR,
    SECTION_SECRET_FIELDS,
    MasterKeyError,
    SecretCipher,
    applicable_secret_paths,
    default_env_var,
    legacy_secret_paths,
    secret_hint,
    strip_section_secrets,
)
from .store import (
    ConfigStore,
    RevisionConflict,
    SecretRecord,
    StoreSnapshot,
    StoreTxn,
)
from .ui_registry import find_spec, is_hidden, registry_payload

logger = logging.getLogger(__name__)

EDITABLE_SECTIONS: tuple[str, ...] = (
    "vendors",
    "tiers",
    "failover",
    "model_mapping",
    "pricing",
    "native_api",
    "session_policies",
    "antigravity_gateway",
)
BOOTSTRAP_SECTIONS: tuple[str, ...] = ("server", "logging", "database", "auth")
ALL_SECTIONS: frozenset[str] = frozenset(ProxyConfig.model_fields) | ALIAS_KEYS
BOOTSTRAP_MSG = "bootstrap 字段仅可通过 CLI 修改"
SECRET_OPS = frozenset({"keep", "set", "clear", "default_env", "env"})
_ENV_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
# 密钥值会成为 HTTP header：拒绝控制字符（C0/DEL/C1）与超出 Latin-1 的字符，
# 否则被 httpx 拒绝的异常消息会携带密钥明文（I2）。
_ILLEGAL_SECRET_RE = re.compile(r"[\x00-\x1f\x7f-\x9f]|[^\x00-\xff]")
_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})
_URL_FIELDS = ("base_url", "token_url")
_URL_DEFAULTS = {k: VendorConfig.model_fields[k].default for k in _URL_FIELDS}
_ACCOUNT_TYPES = ("individual", "business", "enterprise")
_VENDOR_TYPE_LIST: tuple[str, ...] = get_args(
    VendorConfig.model_fields["vendor"].annotation
)
_VENDOR_TYPES = frozenset(_VENDOR_TYPE_LIST)
_MAX_PATTERN = 512
_MAX_ATTEMPTS = 3
_MISSING = object()


class ConfigValidationError(Exception):
    """配置校验失败；``errors`` 为 ``[{loc, msg}]``，不含任何输入值."""

    def __init__(self, errors: list[dict]) -> None:
        self.errors = errors
        locs = ", ".join(".".join(map(str, e["loc"])) or "<root>" for e in errors)
        super().__init__(f"配置校验失败: {locs}")


class ConfigStoreMissing(RuntimeError):
    """config.db 尚未初始化."""


@dataclass
class ChangeResult:
    revisions: dict[str, int]
    config: ProxyConfig
    reset_runtime_note: list[str] = field(default_factory=list)
    cleared_secrets: list[str] = field(default_factory=list)
    changed_sections: list[str] = field(default_factory=list)
    # 移除 vendor 覆盖后保持为空、未回落到包内默认 ``${VAR}`` 的密钥路径
    pinned_secrets: list[str] = field(default_factory=list)


@dataclass
class _PlannedChange:
    """一次写入的计算结果（尚未落盘；``plaintexts`` 为待加密的 set 值）."""

    sections: dict[str, Any]
    records: dict[str, SecretRecord]
    plaintexts: dict[str, str] = field(repr=False)
    config: ProxyConfig
    cleared: list[str]
    pinned: list[str]


def _err(loc: list, msg: str) -> dict:
    return {"loc": list(loc), "msg": msg}


# ── 边界校验（仅 API / CLI 写入；加载路径保持旧版宽松语义） ──────────


def _unwrap(annotation: Any) -> Any:
    """``X | None`` → ``X``."""
    args = get_args(annotation)
    if type(None) in args:
        rest = [a for a in args if a is not type(None)]
        if len(rest) == 1:
            return rest[0]
    return annotation


def _check_keys(value: Any, model: type[BaseModel], loc: list, errors: list) -> None:
    """extra=forbid 语义：拒绝 schema 中不存在的键（extra=allow 的模型除外）."""
    if not isinstance(value, dict) or is_tmpl(value):
        return
    allow_extra = model.model_config.get("extra") == "allow"
    for key, sub in value.items():
        info = model.model_fields.get(key)
        if info is None:
            if not allow_extra:
                errors.append(_err([*loc, key], "未知字段"))
            continue
        annotation = _unwrap(info.annotation)
        if isinstance(annotation, type) and issubclass(annotation, BaseModel):
            _check_keys(sub, annotation, [*loc, key], errors)
        elif get_origin(annotation) is list and isinstance(sub, list):
            inner = _unwrap(get_args(annotation)[0]) if get_args(annotation) else None
            if isinstance(inner, type) and issubclass(inner, BaseModel):
                for index, item in enumerate(sub):
                    _check_keys(item, inner, [*loc, key, index], errors)


def _check_numbers(value: Any, loc: list, errors: list) -> None:
    if is_tmpl(value):
        return
    if isinstance(value, dict):
        for key, sub in value.items():
            sub_loc = [*loc, key]
            if (
                isinstance(sub, (int, float))
                and not isinstance(sub, bool)
                and math.isfinite(sub)
            ):
                if key == "threshold_percent" and not 0 <= sub <= 100:
                    errors.append(_err(sub_loc, "必须在 0–100 之间"))
                    continue
                if key in ("failure_threshold", "success_threshold") and sub < 1:
                    errors.append(_err(sub_loc, "必须 ≥ 1"))
                    continue
            _check_numbers(sub, sub_loc, errors)
    elif isinstance(value, list):
        for index, sub in enumerate(value):
            _check_numbers(sub, [*loc, index], errors)
    elif isinstance(value, float) and not math.isfinite(value):
        errors.append(_err(loc, "必须为有限数值"))
    elif isinstance(value, (int, float)) and not isinstance(value, bool) and value < 0:
        errors.append(_err(loc, "不能为负数"))


def _check_url(value: Any, loc: list, errors: list, *, allow_empty: bool) -> None:
    if is_tmpl(value):
        return
    if not isinstance(value, str):
        errors.append(_err(loc, "必须为字符串"))
        return
    if value == "" and allow_empty:
        return
    try:
        parts = urlsplit(value)
        host = parts.hostname
    except ValueError:
        errors.append(_err(loc, "URL 格式无效"))
        return
    if parts.username is not None or parts.password is not None or "@" in parts.netloc:
        errors.append(_err(loc, "URL 不允许包含用户信息"))
    elif not host:
        errors.append(_err(loc, "URL 缺少主机名"))
    elif parts.scheme == "https":
        return
    elif parts.scheme == "http" and host.lower() in _LOOPBACK_HOSTS:
        return
    else:
        errors.append(_err(loc, "URL 必须为 https（仅本机地址允许 http）"))


def _check_vendors(value: Any, errors: list, rules: list) -> None:
    if not isinstance(value, list):
        errors.append(_err(["vendors"], "必须为列表"))
        return
    seen: set[str] = set()
    for index, vendor in enumerate(value):
        if not isinstance(vendor, dict):
            errors.append(_err(["vendors", index], "必须为对象"))
            continue
        vtype = vendor.get("vendor")
        if vtype not in _VENDOR_TYPES:
            errors.append(_err(["vendors", index, "vendor"], "未知 vendor 类型"))
            continue
        loc = ["vendors", vtype]
        if vtype in seen:
            errors.append(_err(loc, "vendor 类型重复"))
            continue
        seen.add(vtype)
        # 与非 vendors section 一致：密钥字段经 secret_ops / CLI set-secret 写入，不静默丢弃
        errors += [
            _err([*loc, field_name], "密钥请使用 secret_ops / `config set-secret`")
            for field_name in VENDOR_SECRET_FIELD_NAMES
            if field_name in vendor
        ]
        _check_keys(strip_vendor_secrets(vendor), VendorConfig, loc, rules)
        account_type = vendor.get("account_type", "individual")
        if not is_tmpl(account_type) and account_type not in _ACCOUNT_TYPES:
            rules.append(
                _err(
                    [*loc, "account_type"], "必须为 individual / business / enterprise"
                )
            )
        for url_field in _URL_FIELDS:
            if url_field in vendor:
                _check_url(
                    vendor[url_field],
                    [*loc, url_field],
                    rules,
                    allow_empty=url_field == "base_url",
                )
        _check_numbers(strip_vendor_secrets(vendor), loc, rules)


def _check_model_mapping(value: Any, errors: list, rules: list) -> None:
    if not isinstance(value, list):
        errors.append(_err(["model_mapping"], "必须为列表"))
        return
    for index, rule in enumerate(value):
        if not isinstance(rule, dict):
            errors.append(_err(["model_mapping", index], "必须为对象"))
            continue
        pattern = rule.get("pattern")
        if not isinstance(pattern, str):
            continue
        if len(pattern) > _MAX_PATTERN:
            rules.append(_err(["model_mapping", index, "pattern"], "长度不能超过 512"))
        elif rule.get("is_regex"):
            try:
                re.compile(pattern)
            except re.error:
                rules.append(
                    _err(["model_mapping", index, "pattern"], "正则表达式无效")
                )


def _check_tiers(value: Any, errors: list) -> None:
    if value is None:
        return
    if not isinstance(value, list) or not all(isinstance(t, str) for t in value):
        errors.append(_err(["tiers"], "必须为 vendor 名称列表或 null"))
        return
    if len(set(value)) != len(value):
        errors.append(_err(["tiers"], "包含重复的 vendor"))


def _indexed(section: str, value: Any) -> Any:
    """按错误 ``loc`` 的寻址方式组织 section 值（vendors 按类型、policies 包装）."""
    if section == "vendors" and isinstance(value, list):
        return vendor_map(value)
    if section == "session_policies" and isinstance(value, list):
        return {"policies": value}
    return value


def _lookup(value: Any, keys: list) -> Any:
    for key in keys:
        if isinstance(value, dict) and key in value:
            value = value[key]
        elif isinstance(value, list) and isinstance(key, int) and key < len(value):
            value = value[key]
        else:
            return _MISSING
    return value


def boundary_errors(section: str, value: Any, current: Any = _MISSING) -> list[dict]:
    """写入边界校验（不回显值）.

    结构规则（类型、重复、未知 vendor、tiers、密钥字段）总是生效；值规则（未知键、
    URL、数值范围、枚举、正则）只作用于相对 ``current`` 有变化的值，避免已导入且未改动
    的旧值（例如局域网 http 地址）阻塞同 section 其它字段的保存。
    """
    errors: list[dict] = []
    rules: list[dict] = []
    if section == "vendors":
        _check_vendors(value, errors, rules)
    elif section == "tiers":
        _check_tiers(value, errors)
    else:
        keyed = _indexed(section, strip_section_secrets(section, value))
        if section == "model_mapping":
            _check_model_mapping(value, errors, rules)
        if isinstance(value, dict):
            errors += [
                _err([section, key], "密钥请使用 secret_ops / `config set-secret`")
                for key in SECTION_SECRET_FIELDS.get(section, ())
                if key in value
            ]
        if section == "native_api" and isinstance(value, dict):
            for provider, conf in value.items():
                if provider not in ("openai", "gemini", "anthropic"):
                    rules.append(_err(["native_api", provider], "未知 provider"))
                elif isinstance(conf, dict) and "base_url" in conf:
                    _check_url(
                        conf["base_url"],
                        ["native_api", provider, "base_url"],
                        rules,
                        allow_empty=True,
                    )
        if section == "antigravity_gateway" and isinstance(value, dict):
            if "base_url" in value:
                _check_url(
                    value["base_url"],
                    ["antigravity_gateway", "base_url"],
                    rules,
                    allow_empty=True,
                )
        info = ProxyConfig.model_fields.get(section)
        if info is not None:
            annotation = _unwrap(info.annotation)
            if isinstance(annotation, type) and issubclass(annotation, BaseModel):
                _check_keys(keyed, annotation, [section], rules)
            elif get_origin(annotation) is list and isinstance(value, list):
                inner = get_args(annotation)[0]
                if isinstance(inner, type) and issubclass(inner, BaseModel):
                    for index, item in enumerate(value):
                        _check_keys(item, inner, [section, index], rules)
        _check_numbers(keyed, [section], rules)
    if current is not _MISSING:
        new_doc, old_doc = _indexed(section, value), _indexed(section, current)
        rules = [
            e
            for e in rules
            if (old := _lookup(old_doc, e["loc"][1:])) is _MISSING
            or _lookup(new_doc, e["loc"][1:]) != old
        ]
    return [*errors, *rules]


def _pricing_shrink_errors(
    current_raw: Mapping[str, Any], new_raw: Mapping[str, Any]
) -> list[dict]:
    """pricing 行不可删除：任何 (vendor, model) 行消失（删除 / 改名 / 恢复默认丢行）都拒绝.

    pricing 行是 Model Bind 目录的派生来源与下线锚点，删除会使已绑该模型的
    会话在热切换时被整条丢弃；退役请将该行 ``online`` 置 false。错误只带
    消失条数，不回显任何值。
    """

    def _keys(rows: Any) -> set[tuple[str, str]]:
        out: set[tuple[str, str]] = set()
        if isinstance(rows, list):
            for row in rows:
                if isinstance(row, dict):
                    vendor, model = row.get("vendor"), row.get("model")
                    if (
                        isinstance(vendor, str)
                        and isinstance(model, str)
                        and vendor
                        and model
                    ):
                        out.add((vendor, model))
        return out

    removed = _keys(current_raw.get("pricing")) - _keys(new_raw.get("pricing"))
    if removed:
        return [
            _err(
                ["pricing"],
                f"pricing 行不可删除（将移除 {len(removed)} 行）；"
                "下线请将该行 online 置为 false",
            )
        ]
    return []


# ── 服务 ────────────────────────────────────────────────────────


class ConfigService:
    """config.db 的读写门面."""

    def __init__(
        self,
        *,
        boot: ResolvedConfig | None = None,
        environ: Mapping[str, str] | None = None,
    ) -> None:
        self.store = ConfigStore(config_db_path())
        self._boot = boot
        self._environ = os.environ if environ is None else environ

    # ── 读取 ──────────────────────────────────────────────────

    def resolve(self, *, decrypt: bool = True) -> ResolvedConfig:
        if self._boot is not None and self._boot.mode == MODE_LEGACY:
            return self._boot
        return resolve_config(decrypt=decrypt, environ=self._environ)

    def view(self, running: dict | None = None) -> dict:
        """GET /api/config 载荷（不含任何明文密钥）."""
        errors: list[dict] = []
        try:
            resolved = self.resolve(decrypt=True)
        except MasterKeyError as exc:
            resolved = self.resolve(decrypt=False)
            errors.append(_err(["secrets"], str(exc)))
        defaults = packaged_defaults_raw()
        effective = resolved.effective_raw
        values: dict[str, Any] = {}
        for section in EDITABLE_SECTIONS:
            values[section] = copy.deepcopy(effective.get(section))
        present = [v.get("vendor") for v in values.get("vendors") or []]
        values["absent_vendors"] = [t for t in _VENDOR_TYPE_LIST if t not in present]
        for section in BOOTSTRAP_SECTIONS:
            base: dict = {}
            if resolved.config is not None:
                base = getattr(resolved.config, section).model_dump(mode="json")
            raw = effective.get(section)
            merged = deep_merge(base, raw) if isinstance(raw, dict) else base
            merged.pop("google_client_secret", None)
            values[section] = merged
        default_values: dict[str, Any] = {}
        for section in EDITABLE_SECTIONS:
            value = copy.deepcopy(defaults.get(section))
            if section == "vendors" and isinstance(value, list):
                value = [strip_vendor_secrets(v) for v in value]
            default_values[section] = value
        sections_for_rev = {
            *EDITABLE_SECTIONS,
            *BOOTSTRAP_SECTIONS,
            *resolved.revisions,
        }
        running = running or {}
        return {
            "mode": running.get("mode") or resolved.mode,
            "errors": [*resolved.errors, *errors, *(running.get("errors") or [])],
            "notices": list(running.get("notices") or []),
            "revisions": {
                s: resolved.revisions.get(s, 0) for s in sorted(sections_for_rev)
            },
            "overridden": sorted(resolved.overridden),
            "vendor_overrides": sorted(resolved.vendor_overrides),
            "values": values,
            "defaults": default_values,
            "secrets": copy.deepcopy(resolved.secret_states),
            "sources": {**resolved.sources, **(running.get("sources") or {})},
            "running": {
                "tiers": running.get("tiers"),
                "boot_revisions": dict(running.get("boot_revisions") or {}),
            },
            "registry": registry_payload(),
            "apply_supported": bool(running.get("apply_supported", False)),
        }

    def revision(self, section: str) -> int:
        return self.store.snapshot().revision(section)

    # ── 写入 ──────────────────────────────────────────────────

    def apply_changes(
        self,
        payload: Any,
        *,
        actor: str,
        allow_bootstrap: bool,
        allow_env_any: bool,
        preflight: Callable[[ProxyConfig], None] | None = None,
    ) -> ChangeResult:
        """校验并持久化一组变更.

        ``payload``：``{"revisions": {s: int}, "sections": {s: 有效值},
        "reset": [s], "secret_ops": {path: {"op": ..., "value"?, "var"?}}}``。
        ``preflight`` 在写事务之外执行；提交前确认存储自快照后未被改动，否则重算。
        """
        if self._boot is not None and self._boot.mode == MODE_LEGACY:
            raise ConfigValidationError([_err([], "当前以旧 YAML 只读运行，不可写入")])
        if not self.store.exists():
            raise ConfigStoreMissing("config.db 尚未初始化")
        sections, reset, secret_ops, expected = self._parse_payload(
            payload, allow_bootstrap=allow_bootstrap, allow_env_any=allow_env_any
        )
        defaults = packaged_defaults_raw()
        cipher = SecretCipher(environ=self._environ)

        moved: list[str] = []
        for _ in range(_MAX_ATTEMPTS):
            snap = self.store.snapshot()
            conflicts = [s for s, rev in expected.items() if snap.revision(s) != rev]
            if conflicts:
                raise RevisionConflict(conflicts)
            plan = self._plan(
                snap,
                defaults,
                cipher,
                sections,
                reset,
                secret_ops,
                allow_env_any=allow_env_any,
            )
            if preflight is not None:
                _run_preflight(preflight, plan.config)
            with self.store.transaction(expected) as txn:
                moved = _moved_since(snap, txn.snapshot)
                if not moved:
                    changed, changed_secrets = self._write(
                        txn, snap, plan, cipher, sorted({*sections, *reset}), reset
                    )
                    revisions = dict(txn.snapshot.revisions)
            if not moved:
                break
        else:
            raise RevisionConflict(moved)

        if changed or changed_secrets:
            logger.info(
                "config changed by %s: sections=%s, secrets=%s",
                actor,
                changed,
                changed_secrets,
            )
        if plan.cleared:
            logger.warning("目标地址变更，已清空密钥: %s", plan.cleared)
        return ChangeResult(
            revisions=revisions,
            config=plan.config,
            cleared_secrets=plan.cleared,
            changed_sections=changed,
            pinned_secrets=plan.pinned,
        )

    def _plan(
        self,
        snap: StoreSnapshot,
        defaults: dict,
        cipher: SecretCipher,
        sections: Mapping[str, Any],
        reset: list[str],
        secret_ops: Mapping[str, dict],
        *,
        allow_env_any: bool,
    ) -> _PlannedChange:
        """基于快照计算新文档 / 密钥记录并构建 ProxyConfig（纯计算，不写盘）."""
        current_raw = compose_raw(defaults, snap.sections)
        errors: list[dict] = []
        for section, value in sections.items():
            errors += boundary_errors(
                section, value, current_raw.get(section, _MISSING)
            )
        errors += _template_errors(sections, current_raw)
        if errors:
            raise ConfigValidationError(errors)

        new_sections = dict(snap.sections)
        for section in reset:
            new_sections.pop(section, None)
        for section, value in sections.items():
            value = copy.deepcopy(value)
            if section == "vendors":
                value = [strip_vendor_secrets(v) for v in value]
            if section == "session_policies" and isinstance(value, list):
                # list 形态（schema legacy 兼容写法）归一为 dict 后再落库
                value = {"policies": value}
            override = minimize_override(section, defaults.get(section), value)
            if override is NO_OVERRIDE:
                new_sections.pop(section, None)
            else:
                new_sections[section] = json.loads(
                    json.dumps(override, ensure_ascii=False)
                )
        new_raw = compose_raw(defaults, new_sections)
        errors += _pricing_shrink_errors(current_raw, new_raw)
        self._check_tiers_membership(new_raw, sections, errors)
        if not allow_env_any:
            errors += _http_env_binding_errors(defaults, new_raw, secret_ops)
        if errors:
            raise ConfigValidationError(errors)

        records = dict(snap.secrets)
        plaintexts: dict[str, str] = {}
        for path, op in secret_ops.items():
            if op["op"] == "set":
                plaintexts[path] = op["value"].strip()
                records[path] = SecretRecord(
                    kind="stored", hint=secret_hint(plaintexts[path])
                )
            elif op["op"] != "keep":
                records[path] = self._op_record(path, op, defaults)
        pinned = self._materialise(defaults, snap.sections, new_sections, records)
        cleared = self._target_address_rule(
            defaults, current_raw, new_raw, secret_ops, records
        )

        if any(
            r.kind == "stored" and path not in plaintexts for path, r in records.items()
        ):
            cipher.open(snap.meta)
        try:
            cfg = build_config_from(
                defaults,
                new_sections,
                records,
                cipher,
                self._environ,
                plaintexts=plaintexts,
            )
        except ValidationError as exc:
            raise ConfigValidationError(validation_errors(exc, new_raw)) from None
        return _PlannedChange(
            sections=new_sections,
            records=records,
            plaintexts=plaintexts,
            config=cfg,
            cleared=cleared,
            pinned=[p for p in pinned if p not in cleared],
        )

    @staticmethod
    def _write(
        txn: StoreTxn,
        snap: StoreSnapshot,
        plan: _PlannedChange,
        cipher: SecretCipher,
        touched: list[str],
        reset: list[str],
    ) -> tuple[list[str], list[str]]:
        """在写事务内落盘计划（调用方已确认存储自快照后未变）."""
        records = dict(plan.records)
        if plan.plaintexts:
            cipher.ensure_for_write(txn)
            for path, plaintext in plan.plaintexts.items():
                records[path] = cipher.make_record(plaintext)
        changed: list[str] = []
        for section in touched:
            had, has = section in snap.sections, section in plan.sections
            if has and (not had or snap.sections[section] != plan.sections[section]):
                txn.put_section(section, plan.sections[section])
                changed.append(section)
            elif (had and not has) or section in reset:
                txn.delete_section(section)
                changed.append(section)
        changed_secrets = sorted(
            path for path, rec in records.items() if snap.secrets.get(path) != rec
        )
        for path in changed_secrets:
            txn.put_secret(path, records[path])
        return changed, changed_secrets

    def _parse_payload(
        self, payload: Any, *, allow_bootstrap: bool, allow_env_any: bool
    ) -> tuple[dict, list, dict, dict]:
        if not isinstance(payload, dict):
            raise ConfigValidationError([_err([], "请求体必须为 JSON 对象")])
        errors: list[dict] = []
        for key in payload:
            if key not in ("revisions", "sections", "reset", "secret_ops"):
                errors.append(_err([key], "未知字段"))
        revisions = payload.get("revisions") or {}
        sections = payload.get("sections") or {}
        reset = payload.get("reset") or []
        secret_ops = payload.get("secret_ops") or {}
        if not isinstance(revisions, dict) or not all(
            isinstance(v, int) and not isinstance(v, bool) for v in revisions.values()
        ):
            errors.append(_err(["revisions"], "必须为 {section: int}"))
            revisions = {}
        if not isinstance(sections, dict):
            errors.append(_err(["sections"], "必须为对象"))
            sections = {}
        if not isinstance(reset, list) or not all(isinstance(s, str) for s in reset):
            errors.append(_err(["reset"], "必须为 section 名称列表"))
            reset = []
        if not isinstance(secret_ops, dict):
            errors.append(_err(["secret_ops"], "必须为对象"))
            secret_ops = {}

        for section in [*sections, *reset]:
            if section not in ALL_SECTIONS:
                errors.append(_err([section], "未知 section"))
            elif not allow_bootstrap and section not in EDITABLE_SECTIONS:
                errors.append(_err([section], BOOTSTRAP_MSG))
            elif section not in revisions:
                errors.append(_err(["revisions", section], "缺少 revision"))
        for section in set(sections) & set(reset):
            errors.append(_err([section], "不能同时修改与重置"))

        valid_paths = set(applicable_secret_paths())
        if allow_bootstrap:
            valid_paths |= set(legacy_secret_paths())
        defaults = packaged_defaults_raw()
        for path, op in secret_ops.items():
            loc = ["secret_ops", path]
            if path not in valid_paths:
                errors.append(_err(loc, "未知密钥路径"))
                continue
            if not isinstance(op, dict) or op.get("op") not in SECRET_OPS:
                errors.append(
                    _err(loc, "op 必须为 keep / set / clear / default_env / env")
                )
                continue
            extra = set(op) - {"op", "value", "var"}
            if extra:
                errors.append(_err(loc, "未知字段"))
                continue
            kind = op["op"]
            if kind == "set":
                value = op.get("value")
                if not isinstance(value, str) or not value.strip():
                    errors.append(_err([*loc, "value"], "必须为非空字符串"))
                elif _ILLEGAL_SECRET_RE.search(value):
                    errors.append(
                        _err(
                            [*loc, "value"],
                            "密钥包含非法控制字符（仅允许可打印 Latin-1 字符）",
                        )
                    )
            elif kind == "default_env" and default_env_var(path, defaults) is None:
                errors.append(_err(loc, "该字段没有包内默认环境变量"))
            elif kind == "env":
                if not allow_env_any:
                    errors.append(_err(loc, "仅 CLI 可绑定任意环境变量"))
                elif not isinstance(op.get("var"), str) or not _ENV_NAME_RE.match(
                    op["var"]
                ):
                    errors.append(_err([*loc, "var"], "环境变量名无效"))
        if errors:
            raise ConfigValidationError(errors)
        expected = {s: revisions[s] for s in [*sections, *reset]}
        return sections, reset, secret_ops, expected

    @staticmethod
    def _check_tiers_membership(new_raw: dict, sections: dict, errors: list) -> None:
        if "tiers" not in sections or sections["tiers"] is None:
            return
        present = {v.get("vendor") for v in new_raw.get("vendors") or []}
        for name in sections["tiers"]:
            if name not in present:
                errors.append(_err(["tiers"], "引用了未配置的 vendor"))
                return

    @staticmethod
    def _materialise(
        defaults: dict,
        old_sections: Mapping[str, Any],
        new_sections: Mapping[str, Any],
        records: dict[str, SecretRecord],
    ) -> list[str]:
        """vendor 覆盖状态变化时，把变化前生效的密钥来源物化为记录；返回移除覆盖时的物化路径.

        无记录的密钥按覆盖状态解析（覆盖 → ""，否则包内默认 ``${VAR}``），因此覆盖的
        新建或移除都会悄悄改变来源；物化后只有显式 secret op 能改变来源。
        - 新建覆盖：原来源为包内默认 → env 记录（无默认变量则 empty）；
        - 移除覆盖：原来源为 "" → empty 记录（包内默认为空时无需物化）。
        """
        old_types = set(stored_vendor_types(old_sections.get("vendors")))
        new_types = set(stored_vendor_types(new_sections.get("vendors")))
        default_vendors = vendor_map(defaults.get("vendors") or [])
        pinned: list[str] = []
        for vtype in sorted(old_types ^ new_types):
            default_dict = default_vendors.get(vtype) or {}
            created = vtype in new_types
            for field_name in SECRET_FIELDS_BY_VENDOR.get(vtype, ()):
                path = f"vendors.{vtype}.{field_name}"
                default_value = default_dict.get(field_name)
                if path in records or (not created and default_value in (None, "")):
                    continue
                var = whole_env_var(default_value) if created else None
                records[path] = (
                    SecretRecord(kind="env", env_var=var)
                    if var
                    else SecretRecord(kind="empty")
                )
                if not created:
                    pinned.append(path)
        return pinned

    @staticmethod
    def _op_record(path: str, op: dict, defaults: dict) -> SecretRecord:
        kind = op["op"]
        if kind == "clear":
            return SecretRecord(kind="empty")
        if kind == "default_env":
            return SecretRecord(kind="env", env_var=default_env_var(path, defaults))
        return SecretRecord(kind="env", env_var=op["var"])

    @staticmethod
    def _target_address_rule(
        defaults: dict,
        current_raw: dict,
        new_raw: dict,
        secret_ops: Mapping[str, dict],
        records: dict[str, SecretRecord],
    ) -> list[str]:
        """vendor 目标地址（base_url / token_url）变化 → 清空其密钥来源.

        清空仅发生在新目标**不是**包内默认端点时（reset 或改回默认目标保留密钥）。
        比较基准：已在有效列表中的 vendor 取变化前的值；新加入（含从 ``$absent``
        恢复）的 vendor 取包内默认值。同一请求显式指定的来源（``set`` / ``clear``；
        ``default_env`` 经 HTTP 时已限定目标为包内默认，见
        :func:`_http_env_binding_errors`；``env`` 仅 CLI）保留，其余记录（含先前移除
        vendor 时遗留的记录、物化生成的 env 记录）一律清空。
        """
        old_vendors = vendor_map(current_raw.get("vendors") or [])
        new_vendors = vendor_map(new_raw.get("vendors") or [])
        default_vendors = vendor_map(defaults.get("vendors") or [])
        cleared: list[str] = []
        for vtype, new_dict in new_vendors.items():
            before = old_vendors.get(vtype, default_vendors.get(vtype))
            if _target(before) == _target(new_dict):
                continue
            if _target(new_dict) == _target(default_vendors.get(vtype, new_dict)):
                # 新目标即包内默认端点（reset / 改回默认）：密钥不会发往新地址，保留
                continue
            for field_name in SECRET_FIELDS_BY_VENDOR.get(vtype, ()):
                path = f"vendors.{vtype}.{field_name}"
                if secret_ops.get(path, {}).get("op", "keep") != "keep":
                    continue
                # 无记录 = 已覆盖 vendor 的缺省值 ""（物化规则已为新覆盖补齐记录）
                previous = records.get(path)
                if previous is None or previous.kind == "empty":
                    continue
                records[path] = SecretRecord(kind="empty")
                cleared.append(path)
        return cleared

    # ── CLI 辅助 ──────────────────────────────────────────────

    def set_path(self, path: str, value: Any) -> tuple[ChangeResult, bool]:
        """CLI ``config set``：按点分路径修改单个值，返回 ``(结果, 是否 bootstrap)``."""
        parts = path.split(".")
        section = parts[0]
        if section not in ALL_SECTIONS:
            raise ConfigValidationError([_err(parts, "未知配置路径")])
        if (
            path in applicable_secret_paths()
            or path in legacy_secret_paths()
            or (
                len(parts) == 3
                and parts[0] == "vendors"
                and parts[2] in VENDOR_SECRET_FIELD_NAMES
            )
        ):
            raise ConfigValidationError([_err(parts, "密钥请使用 `config set-secret`")])
        if section in _LEGACY_FLAT_KEYS or section in ALIAS_KEYS:
            raise ConfigValidationError(
                [
                    _err(
                        parts,
                        "legacy 字段不参与运行时路由；请经 `config export` → 编辑 → "
                        "`config import` 往返调整",
                    )
                ]
            )
        pattern = ".".join("*" if p.isdigit() else p for p in parts)
        if len(parts) >= 3 and parts[0] == "vendors":
            pattern = ".".join(["vendors", "*", *parts[2:]])
        if len(parts) > 1:
            if is_hidden(pattern) or find_spec(pattern) is None:
                raise ConfigValidationError([_err(parts, "未知配置路径")])

        resolved = self.resolve(decrypt=False)
        current = copy.deepcopy(resolved.effective_raw.get(section))
        if len(parts) == 1:
            new_value = value
        elif section == "vendors":
            vendors = current or []
            target = next((v for v in vendors if v.get("vendor") == parts[1]), None)
            if target is None:
                raise ConfigValidationError(
                    [
                        _err(
                            parts[:2],
                            "vendor 未配置；请先通过 `config set vendors` 添加",
                        )
                    ]
                )
            _set_nested(target, parts[2:], value)
            new_value = vendors
        else:
            container = current if isinstance(current, (dict, list)) else {}
            _set_nested(container, parts[1:], value)
            new_value = container
        payload = {
            "revisions": {section: resolved.revisions.get(section, 0)},
            "sections": {section: new_value},
        }
        result = self.apply_changes(
            payload, actor="cli", allow_bootstrap=True, allow_env_any=True
        )
        return result, section in BOOTSTRAP_SECTIONS

    def reset_section(self, section: str) -> ChangeResult:
        payload = {"revisions": {section: self.revision(section)}, "reset": [section]}
        return self.apply_changes(
            payload, actor="cli", allow_bootstrap=True, allow_env_any=True
        )

    def set_secret(self, path: str, op: dict) -> ChangeResult:
        return self.apply_changes(
            {"secret_ops": {path: op}},
            actor="cli",
            allow_bootstrap=True,
            allow_env_any=True,
        )

    # ── 导入 / 导出 ───────────────────────────────────────────

    def export_yaml(self) -> str:
        """导出有效配置；密钥只输出 ``${VAR}`` / ``<redacted>`` / ``""``."""
        resolved = self.resolve(decrypt=False)
        snap = self.store.snapshot()
        doc = copy.deepcopy(resolved.effective_raw)
        defaults_by_type = vendor_map(packaged_defaults_raw().get("vendors") or [])

        def _placeholder(record: SecretRecord) -> Any:
            if record.kind == "env":
                return {TMPL_KEY: f"${{{record.env_var}}}"}
            if record.kind == "stored":
                return REDACTED
            return ""

        for vendor in doc.get("vendors") or []:
            vtype = vendor.get("vendor")
            for field_name in VENDOR_SECRET_FIELD_NAMES:
                record = snap.secrets.get(f"vendors.{vtype}.{field_name}")
                if record is not None:
                    vendor[field_name] = _placeholder(record)
                elif vtype not in resolved.vendor_overrides:
                    default_value = (defaults_by_type.get(vtype) or {}).get(field_name)
                    if default_value is not None:
                        vendor[field_name] = default_value
        for section, fields in SECTION_SECRET_FIELDS.items():
            for field_name in fields:
                record = snap.secrets.get(f"{section}.{field_name}")
                if record is None:
                    continue
                target = section_container(doc, section)
                if target is not None:
                    target[field_name] = _placeholder(record)
        # 防御：含 ${ 的字面量写成 YAML 后会被导入当作模板展开（写入侧已拒绝）
        keyed = {**doc, "vendors": vendor_map(doc.get("vendors") or [])}
        literal = list(iter_literal_tmpl_locs(keyed, []))
        if literal:
            raise ConfigValidationError(
                [_err(loc, _LITERAL_TMPL_MSG) for loc in literal]
            )
        header = (
            "# coding-proxy 配置导出（密钥已脱敏：${VAR} 为环境变量引用，"
            f"{REDACTED} 表示保留本地已存密钥）\n"
        )
        body = yaml.safe_dump(
            denormalize(doc),
            allow_unicode=True,
            sort_keys=False,
            default_flow_style=False,
        )
        return header + body

    def import_yaml(self, path: Path, *, dry_run: bool) -> dict:
        """导入 YAML（整体替换当前覆盖）；``<redacted>`` 表示保留既有密钥记录.

        ``diffs``：导入结果与 YAML 旧语义不一致的字段（非空则拒绝写入）；
        ``changes``：相对当前存储会改变的生效字段 / 密钥路径（导出→导入应为空）。
        """
        snap = self.store.snapshot()
        plan = plan_import(path)
        plan.require_keep_records(snap.secrets)
        diffs = verify_equivalence(plan, path, self._environ, existing=snap)
        summary = {
            "source": str(path),
            "sections": sorted(plan.sections),
            "removed_sections": sorted(set(snap.sections) - set(plan.sections)),
            "secrets": {
                p: ("keep" if ps.keep else ps.kind)
                for p, ps in sorted(plan.secrets.items())
            },
            "warnings": list(plan.warnings),
            "diffs": diffs,
            "changes": [] if diffs else self._import_changes(snap, plan),
            "applied": False,
        }
        if diffs and not dry_run:
            raise ImportPlanError(f"导入结果与 YAML 语义不一致: {', '.join(diffs)}")
        if not dry_run:
            ensure_home()
            apply_import(
                self.store,
                SecretCipher(environ=self._environ),
                plan,
                make_marker(path.resolve(), plan.source_sha256),
            )
            summary["applied"] = True
        return summary

    def _import_changes(self, snap: StoreSnapshot, plan: ImportPlan) -> list[str]:
        """导入计划相对当前存储改变的生效字段路径（密钥按来源比较，不解密）."""
        defaults = packaged_defaults_raw()
        try:
            before = _build_with_tokens(
                defaults, snap.sections, snap.secrets, set(), self._environ
            )
        except ValidationError:
            return ["<current>"]
        after = _build_with_tokens(
            defaults,
            plan.sections,
            plan.secret_records(snap.secrets),
            set(plan.plaintexts()),
            self._environ,
        )
        return config_diff_paths(before, after)


def _build_with_tokens(
    defaults: dict,
    sections: Mapping[str, Any],
    records: Mapping[str, SecretRecord],
    new_paths: set[str],
    environ: Mapping[str, str],
) -> ProxyConfig:
    """不解密地构建 ProxyConfig：stored 密钥以来源标记代替明文.

    导入计划中的 stored 记录要么沿用当前记录（``<redacted>``），要么是新明文
    （``new_paths``），因此「路径 + 是否新值」即可区分来源；env / empty 记录照常解析
    （与包内默认 ``${VAR}`` 的展开结果可直接比较）。
    """
    tokens: dict[str, str] = {}
    for path, record in records.items():
        if path in new_paths:
            tokens[path] = f"<set:{path}>"
        elif record.kind == "stored":
            tokens[path] = f"<stored:{path}>"
    return build_config_from(
        defaults, sections, records, None, environ, decrypt=False, plaintexts=tokens
    )


def _run_preflight(preflight: Callable[[ProxyConfig], None], cfg: ProxyConfig) -> None:
    try:
        preflight(cfg)
    except ConfigValidationError:
        raise
    except Exception as exc:
        raise ConfigValidationError(
            [_err([], f"preflight 失败: {type(exc).__name__}")]
        ) from None


def _moved_since(before: StoreSnapshot, now: StoreSnapshot) -> list[str]:
    """快照之后被其它写入方改动的 section（密钥 / 主密钥变化记为 ``secrets``）."""
    names = set(before.revisions) | set(now.revisions)
    moved = sorted(s for s in names if before.revision(s) != now.revision(s))
    if before.secrets != now.secrets or before.meta.get("key_check") != now.meta.get(
        "key_check"
    ):
        moved.append("secrets")
    return moved


def _target(vendor: Mapping[str, Any] | None) -> tuple:
    """vendor 的目标地址（缺省字段按 Pydantic 默认补齐）."""
    vendor = vendor or {}
    return tuple(vendor.get(k, _URL_DEFAULTS[k]) for k in _URL_FIELDS)


def _http_env_binding_errors(
    defaults: dict, new_raw: dict, secret_ops: Mapping[str, dict]
) -> list[dict]:
    """HTTP 只能在 vendor 目标地址等于包内默认值时绑定默认环境变量."""
    new_vendors = vendor_map(new_raw.get("vendors") or [])
    default_vendors = vendor_map(defaults.get("vendors") or [])
    errors: list[dict] = []
    for path, op in secret_ops.items():
        parts = path.split(".")
        if op.get("op") != "default_env" or len(parts) != 3 or parts[0] != "vendors":
            continue
        vendor = new_vendors.get(parts[1])
        if vendor is not None and _target(vendor) != _target(
            default_vendors.get(parts[1])
        ):
            errors.append(
                _err(
                    ["secret_ops", path],
                    "目标地址不是包内默认值，请改用 set，或通过 CLI "
                    "`config set-secret --default-env` 绑定环境变量",
                )
            )
    return errors


_LITERAL_TMPL_MSG = (
    "不支持写入含 ${ 的字符串（YAML 导出后会被当作环境变量模板）；"
    "模板请通过 `coding-proxy config import` 导入"
)


def _template_errors(sections: Mapping[str, Any], current_raw: dict) -> list[dict]:
    """写入只能保留既有 ``$tmpl`` 节点（同 section、同 vendor 的非密钥字段），不能新建.

    新写入的含 ``${`` 普通字符串同样拒绝：YAML 无法区分字面量与模板，导出→导入会
    把它变成环境变量展开。
    """
    errors: list[dict] = []
    for section, value in sections.items():
        current = _indexed(section, current_raw.get(section))
        if section == "vendors" and isinstance(value, list):
            for vendor in value:
                if not isinstance(vendor, dict):
                    continue
                vtype = vendor.get("vendor")
                stripped = strip_vendor_secrets(vendor)
                old = current.get(vtype) if isinstance(current, dict) else None
                allowed = set(iter_tmpl_strings(strip_vendor_secrets(old or {})))
                if not set(iter_tmpl_strings(stripped)) <= allowed:
                    errors.append(
                        _err(["vendors", vtype], "不允许通过写入新建 ${VAR} 模板")
                    )
            new_doc = vendor_map(
                [strip_vendor_secrets(v) for v in value if isinstance(v, dict)]
            )
        else:
            allowed = set(iter_tmpl_strings(current_raw.get(section)))
            if not set(iter_tmpl_strings(value)) <= allowed:
                errors.append(_err([section], "不允许通过写入新建 ${VAR} 模板"))
            new_doc = _indexed(section, value)
        errors += [
            _err(loc, _LITERAL_TMPL_MSG)
            for loc in iter_literal_tmpl_locs(new_doc, [section])
            if _lookup(current, loc[1:]) != _lookup(new_doc, loc[1:])
        ]
    return errors


def _set_nested(container: Any, keys: list[str], value: Any) -> None:
    for index, key in enumerate(keys):
        last = index == len(keys) - 1
        if isinstance(container, list):
            if not key.isdigit() or int(key) >= len(container):
                raise ConfigValidationError([_err(keys[: index + 1], "列表下标无效")])
            if last:
                container[int(key)] = value
                return
            container = container[int(key)]
        elif isinstance(container, dict):
            if last:
                container[key] = value
                return
            if not isinstance(container.get(key), (dict, list)):
                container[key] = {}
            container = container[key]
        else:
            raise ConfigValidationError([_err(keys[: index + 1], "路径不可写")])


__all__ = [
    "EDITABLE_SECTIONS",
    "BOOTSTRAP_SECTIONS",
    "BOOTSTRAP_MSG",
    "ConfigService",
    "ConfigValidationError",
    "ConfigStoreMissing",
    "ChangeResult",
    "boundary_errors",
]
