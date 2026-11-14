"""运行时配置解析：包内默认 ⊕ config.db 覆盖 ⊕ 密钥注入 → ``$tmpl`` 展开 → ProxyConfig.

:func:`resolve_config` 无副作用（只读打开 config.db，不创建任何文件）。
NATIVE_*_BASE_URL 环境变量覆写仍在 ``NativeApiConfig`` 校验器内发生（与旧行为一致）。
"""

from __future__ import annotations

import copy
import logging
import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from pydantic import ValidationError

from ..native_api.config import _ENV_BASE_URL_MAP
from .auth_schema import AuthConfig
from .defaults import packaged_defaults_raw
from .home import config_db_path
from .normalize import (
    VENDOR_SECRET_FIELD_NAMES,
    compose_raw,
    expand_tmpl,
    stored_vendor_types,
    strip_vendor_secrets,
    vendor_map,
    whole_env_var,
)
from .schema import ProxyConfig
from .secrets import (
    AUTH_SECRET_PATH,
    SECRET_FIELDS_BY_VENDOR,
    SECTION_SECRET_FIELDS,
    SecretCipher,
    resolve_record,
    secret_hint,
    strip_section_secrets,
)
from .store import ConfigStore, SecretRecord, StoreSnapshot

logger = logging.getLogger(__name__)

MODE_STORE = "store"
MODE_DEFAULTS = "defaults"
MODE_LEGACY = "legacy_readonly"
MODE_DEGRADED = "degraded"


@dataclass
class ResolvedConfig:
    config: ProxyConfig | None
    effective_raw: dict
    revisions: dict[str, int] = field(default_factory=dict)
    overridden: set[str] = field(default_factory=set)
    vendor_overrides: set[str] = field(default_factory=set)
    secret_states: dict[str, dict] = field(default_factory=dict)
    sources: dict[str, str] = field(default_factory=dict)
    mode: str = MODE_STORE
    errors: list[dict] = field(default_factory=list)


def validation_errors(exc: ValidationError, raw: Mapping | None = None) -> list[dict]:
    """ValidationError → ``[{loc, msg}]``（不含 input；vendors 下标换成 vendor 类型）."""
    vendors = (raw or {}).get("vendors") if raw is not None else None
    result: list[dict] = []
    for err in exc.errors(
        include_input=False, include_url=False, include_context=False
    ):
        loc = list(err.get("loc", ()))
        if (
            len(loc) >= 2
            and loc[0] == "vendors"
            and isinstance(loc[1], int)
            and isinstance(vendors, list)
            and loc[1] < len(vendors)
            and isinstance(vendors[loc[1]], dict)
            and "vendor" in vendors[loc[1]]
        ):
            loc[1] = str(vendors[loc[1]]["vendor"])
        result.append({"loc": loc, "msg": str(err.get("msg", ""))})
    return result


# ── 密钥注入 ────────────────────────────────────────────────────


def _secret_value(
    path: str,
    secrets: Mapping[str, SecretRecord],
    cipher: SecretCipher | None,
    environ: Mapping[str, str],
    decrypt: bool,
    plaintexts: Mapping[str, str] | None,
) -> str | None:
    record = secrets.get(path)
    if record is None:
        return None
    value, _ = resolve_record(
        record,
        cipher=cipher,
        environ=environ,
        decrypt=decrypt,
        plaintext=(plaintexts or {}).get(path),
    )
    return value


def inject_secrets(
    raw: dict,
    secrets: Mapping[str, SecretRecord],
    *,
    cipher: SecretCipher | None,
    environ: Mapping[str, str],
    decrypt: bool = True,
    plaintexts: Mapping[str, str] | None = None,
) -> dict:
    """把密钥记录写回有效文档（就地修改并返回 raw）.

    规则：记录存在 → 记录值；无记录 → 保留文档原值（默认 vendor 中的 ``$tmpl``，
    或被覆盖 vendor 缺省字段 → Pydantic 默认 ""）。非 vendors 的 section 密钥不依赖
    覆盖行是否存在（缺失时新建 section dict），因此仅含密钥的 legacy / alias section
    无需保留空行，最小化不动点成立。
    """
    for vendor in raw.get("vendors") or []:
        if not isinstance(vendor, dict):
            continue
        for field_name in VENDOR_SECRET_FIELD_NAMES:
            value = _secret_value(
                f"vendors.{vendor.get('vendor')}.{field_name}",
                secrets,
                cipher,
                environ,
                decrypt,
                plaintexts,
            )
            if value is not None:
                vendor[field_name] = value
    for section, fields in SECTION_SECRET_FIELDS.items():
        for field_name in fields:
            value = _secret_value(
                f"{section}.{field_name}",
                secrets,
                cipher,
                environ,
                decrypt,
                plaintexts,
            )
            if value is None:
                continue
            target = section_container(raw, section)
            if target is not None:
                target[field_name] = value
    return raw


def section_container(raw: dict, section: str) -> dict | None:
    """密钥所在 section 的 dict（缺失时新建；显式非 dict 值保持原样并返回 None）."""
    if section not in raw:
        raw[section] = {}
    target = raw[section]
    return target if isinstance(target, dict) else None


def strip_secrets(raw: dict) -> dict:
    """返回去除密钥字段的有效文档副本."""
    result = copy.deepcopy(raw)
    if isinstance(result.get("vendors"), list):
        result["vendors"] = [
            strip_vendor_secrets(v) if isinstance(v, dict) else v
            for v in result["vendors"]
        ]
    for section in SECTION_SECRET_FIELDS:
        if isinstance(result.get(section), dict):
            result[section] = strip_section_secrets(section, result[section])
    return result


def _needs_cipher(secrets: Mapping[str, SecretRecord], plaintexts: Mapping | None):
    return any(
        r.kind == "stored" and path not in (plaintexts or {})
        for path, r in secrets.items()
    )


def build_config_from(
    defaults_raw: dict,
    sections: Mapping[str, Any],
    secrets: Mapping[str, SecretRecord],
    cipher: SecretCipher | None,
    environ: Mapping[str, str],
    *,
    decrypt: bool = True,
    plaintexts: Mapping[str, str] | None = None,
) -> ProxyConfig:
    """纯函数：构建 ProxyConfig（``plaintexts`` 用于尚未加密的导入计划）."""
    raw = compose_raw(defaults_raw, sections)
    inject_secrets(
        raw,
        secrets,
        cipher=cipher,
        environ=environ,
        decrypt=decrypt,
        plaintexts=plaintexts,
    )
    return ProxyConfig(**copy.deepcopy(expand_tmpl(raw, environ)))


# ── 密钥状态视图 ────────────────────────────────────────────────


def _state_from_value(value: Any, environ: Mapping[str, str]) -> dict:
    var = whole_env_var(value)
    if var is not None:
        env_value = environ.get(var, "")
        return {
            "kind": "env",
            "env_var": var,
            "resolved": bool(env_value),
            "hint": _fmt_hint(secret_hint(env_value)),
        }
    if value is None or value == "":
        return {"kind": "empty", "env_var": None, "resolved": False, "hint": ""}
    # 包内 / Pydantic 内置默认的字面量（例如公开的 OAuth client secret）
    return {"kind": "default", "env_var": None, "resolved": True, "hint": ""}


def _fmt_hint(hint: str) -> str:
    return f"••••{hint}" if hint else ""


def compute_secret_states(
    defaults_raw: dict,
    effective_raw: dict,
    secrets: Mapping[str, SecretRecord],
    vendor_overrides: set[str],
    *,
    environ: Mapping[str, str],
    resolved_stored: Mapping[str, bool] | None = None,
) -> dict[str, dict]:
    """每个可操作密钥路径的 ``{kind, env_var, resolved, hint, default_env}``."""
    default_vendors = vendor_map(defaults_raw.get("vendors") or [])
    states: dict[str, dict] = {}

    def _record_state(record: SecretRecord, path: str) -> dict:
        if record.kind == "env":
            env_value = environ.get(record.env_var or "", "")
            return {
                "kind": "env",
                "env_var": record.env_var,
                "resolved": bool(env_value),
                "hint": _fmt_hint(secret_hint(env_value)),
            }
        if record.kind == "empty":
            return {"kind": "empty", "env_var": None, "resolved": False, "hint": ""}
        return {
            "kind": "stored",
            "env_var": None,
            "resolved": bool((resolved_stored or {}).get(path, False)),
            "hint": _fmt_hint(record.hint),
        }

    for vendor_type, fields in SECRET_FIELDS_BY_VENDOR.items():
        default_dict = default_vendors.get(vendor_type) or {}
        for field_name in fields:
            path = f"vendors.{vendor_type}.{field_name}"
            record = secrets.get(path)
            if record is not None:
                state = _record_state(record, path)
            elif vendor_type in vendor_overrides:
                state = _state_from_value("", environ)
            else:
                state = _state_from_value(default_dict.get(field_name), environ)
            state["default_env"] = whole_env_var(default_dict.get(field_name))
            states[path] = state

    record = secrets.get(AUTH_SECRET_PATH)
    if record is not None:
        state = _record_state(record, AUTH_SECRET_PATH)
    else:
        auth_raw = effective_raw.get("auth") or {}
        value = auth_raw.get(
            "google_client_secret",
            AuthConfig.model_fields["google_client_secret"].default,
        )
        state = _state_from_value(value, environ)
    auth_default = defaults_raw.get("auth") if isinstance(defaults_raw, dict) else None
    state["default_env"] = (
        whole_env_var(auth_default.get("google_client_secret"))
        if isinstance(auth_default, dict)
        else None
    )
    states[AUTH_SECRET_PATH] = state
    return states


def env_sources() -> dict[str, str]:
    """NATIVE_*_BASE_URL 覆写的字段来源（校验器读取的是 os.environ）."""
    sources: dict[str, str] = {}
    for provider, env_name in _ENV_BASE_URL_MAP.items():
        if os.environ.get(env_name, "").strip():
            sources[f"native_api.{provider}.base_url"] = "env"
    return sources


# ── 入口 ────────────────────────────────────────────────────────


def resolve_from_snapshot(
    snap: StoreSnapshot,
    *,
    mode: str,
    decrypt: bool = True,
    environ: Mapping[str, str] | None = None,
) -> ResolvedConfig:
    environ = os.environ if environ is None else environ
    defaults_raw = packaged_defaults_raw()
    raw = compose_raw(defaults_raw, snap.sections)
    vendor_overrides = set(stored_vendor_types(snap.sections.get("vendors")))

    cipher: SecretCipher | None = None
    resolved_stored: dict[str, bool] = {}
    if decrypt and _needs_cipher(snap.secrets, None):
        cipher = SecretCipher(environ=environ)
        cipher.open(snap.meta)

    full = inject_secrets(
        copy.deepcopy(raw),
        snap.secrets,
        cipher=cipher,
        environ=environ,
        decrypt=decrypt,
    )
    if decrypt:
        resolved_stored = {
            path: True for path, r in snap.secrets.items() if r.kind == "stored"
        }
    result = ResolvedConfig(
        config=None,
        effective_raw=strip_secrets(raw),
        revisions=dict(snap.revisions),
        overridden=set(snap.sections),
        vendor_overrides=vendor_overrides,
        secret_states=compute_secret_states(
            defaults_raw,
            raw,
            snap.secrets,
            vendor_overrides,
            environ=environ,
            resolved_stored=resolved_stored,
        ),
        sources=env_sources(),
        mode=mode,
    )
    try:
        result.config = ProxyConfig(**copy.deepcopy(expand_tmpl(full, environ)))
    except ValidationError as exc:
        result.mode = MODE_DEGRADED
        result.errors = validation_errors(exc, raw)
        logger.error(
            "存储的配置无效，进入 degraded 模式: %s",
            [".".join(map(str, e["loc"])) for e in result.errors],
        )
    return result


def resolve_config(
    *,
    decrypt: bool = True,
    environ: Mapping[str, str] | None = None,
) -> ResolvedConfig:
    """解析运行时配置（无副作用；config.db / master.key / 派生路径均取自 proxy_home()）.

    - config.db 不存在 → ``mode="defaults"``（仅包内默认）；
    - 否则默认 ⊕ 覆盖 ⊕ 密钥 → ProxyConfig；存储配置无效 → ``mode="degraded"``。
    """
    store = ConfigStore(config_db_path())
    if not store.exists():
        return resolve_from_snapshot(
            StoreSnapshot(), mode=MODE_DEFAULTS, decrypt=decrypt, environ=environ
        )
    return resolve_from_snapshot(
        store.snapshot(), mode=MODE_STORE, decrypt=decrypt, environ=environ
    )


__all__ = [
    "MODE_STORE",
    "MODE_DEFAULTS",
    "MODE_LEGACY",
    "MODE_DEGRADED",
    "ResolvedConfig",
    "resolve_config",
    "resolve_from_snapshot",
    "build_config_from",
    "inject_secrets",
    "section_container",
    "strip_secrets",
    "compute_secret_states",
    "validation_errors",
    "env_sources",
]
