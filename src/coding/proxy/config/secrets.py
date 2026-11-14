"""密钥：字段路径（单一事实源）、Fernet 主密钥与三态解析.

路径格式 ``vendors.<type>.<field>`` / ``auth.google_client_secret``；legacy flat 与
alias section（``primary`` / ``anthropic`` / ``copilot`` / ``antigravity`` /
``fallback`` / ``zhipu``）中的全部 vendor 密钥字段同样剥离为 ``<section>.<field>``
（旧 validator 以 ``{"vendor": t, **section}`` 迁移，任意密钥字段都会流入 vendor），
保证 config.db 中任何 section 文档都不含明文密钥。

主密钥：env ``CODING_PROXY_MASTER_KEY``（urlsafe base64 Fernet key）优先，否则
``$CODING_PROXY_HOME/master.key``（0600）。``meta.key_check`` 存在时 key 不匹配直接
报错，绝不重新生成（防 CLI 与服务进程 env 不一致导致互相写坏）。
"""

from __future__ import annotations

import hashlib
import os
from collections.abc import Mapping
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken

from .home import create_private_file, ensure_home, master_key_path
from .normalize import VENDOR_SECRET_FIELD_NAMES, whole_env_var
from .store import SecretRecord, StoreTxn

ENV_MASTER_KEY = "CODING_PROXY_MASTER_KEY"
_KEY_CHECK_PLAINTEXT = b"coding-proxy:key-check:v1"
HINT_MIN_LENGTH = 16

SECRET_FIELDS_BY_VENDOR: dict[str, tuple[str, ...]] = {
    "copilot": ("github_token",),
    "antigravity": ("client_id", "client_secret", "refresh_token"),
    "zhipu": ("api_key",),
    "minimax": ("api_key",),
    "alibaba": ("api_key",),
    "xiaomi": ("api_key",),
    "kimi": ("api_key",),
    "doubao": ("api_key",),
}
AUTH_SECRET_PATH = "auth.google_client_secret"
# 承载 vendor 字段的 legacy flat / alias section（隐藏，仅 import / export / CLI 往返）
LEGACY_VENDOR_SECTIONS: tuple[str, ...] = (
    "primary",
    "anthropic",
    "copilot",
    "antigravity",
    "fallback",
    "zhipu",
)
LEGACY_SECRET_FIELDS: dict[str, tuple[str, ...]] = {
    section: VENDOR_SECRET_FIELD_NAMES for section in LEGACY_VENDOR_SECTIONS
}
# vendors 之外的全部密钥字段（section → 字段），剥离 / 注入 / 导出共用
SECTION_SECRET_FIELDS: dict[str, tuple[str, ...]] = {
    "auth": ("google_client_secret",),
    **LEGACY_SECRET_FIELDS,
}


def vendor_secret_path(vendor_type: str, field_name: str) -> str:
    return f"vendors.{vendor_type}.{field_name}"


def applicable_secret_paths() -> list[str]:
    """UI / HTTP 可操作的密钥路径."""
    paths = [
        vendor_secret_path(t, f)
        for t, fields in SECRET_FIELDS_BY_VENDOR.items()
        for f in fields
    ]
    return [*paths, AUTH_SECRET_PATH]


def legacy_secret_paths() -> list[str]:
    return [f"{s}.{f}" for s, fields in LEGACY_SECRET_FIELDS.items() for f in fields]


def strip_section_secrets(section: str, value: object) -> object:
    """返回去除该 section 密钥字段后的值（非 dict 原样返回）."""
    fields = SECTION_SECRET_FIELDS.get(section)
    if not fields or not isinstance(value, dict):
        return value
    return {k: v for k, v in value.items() if k not in fields}


def is_vendor_secret_path(path: str) -> bool:
    parts = path.split(".")
    return (
        len(parts) == 3
        and parts[0] == "vendors"
        and parts[2] in VENDOR_SECRET_FIELD_NAMES
    )


def secret_hint(plaintext: str) -> str:
    """明文长度 ≥ 16 时返回末 4 位，否则为空."""
    return plaintext[-4:] if len(plaintext) >= HINT_MIN_LENGTH else ""


def default_env_var(path: str, defaults_raw: Mapping) -> str | None:
    """包内默认值中该密钥路径的环境变量名（如 ``ZHIPU_API_KEY``）."""
    parts = path.split(".")
    if len(parts) == 3 and parts[0] == "vendors":
        for vendor in defaults_raw.get("vendors") or []:
            if isinstance(vendor, dict) and vendor.get("vendor") == parts[1]:
                return whole_env_var(vendor.get(parts[2]))
        return None
    if len(parts) == 2:
        section = defaults_raw.get(parts[0])
        if isinstance(section, dict):
            return whole_env_var(section.get(parts[1]))
    return None


# ── 主密钥 ──────────────────────────────────────────────────────


class MasterKeyError(RuntimeError):
    """主密钥不可用（缺失 / 格式错误 / 不匹配）；消息不含密钥内容."""


class MasterKeyMismatch(MasterKeyError):
    """主密钥与 config.db 的 key_check 不匹配."""


def _key_id(key: bytes) -> str:
    return hashlib.sha256(key).hexdigest()[:8]


class SecretCipher:
    """Fernet 加解密；按需加载主密钥."""

    def __init__(
        self, home: Path | None = None, *, environ: Mapping[str, str] | None = None
    ) -> None:
        self._home = home
        self._environ = os.environ if environ is None else environ
        self._fernet: Fernet | None = None
        self.key_id: str | None = None

    @property
    def key_file(self) -> Path:
        return self._home / "master.key" if self._home else master_key_path()

    @property
    def key_source(self) -> str:
        if self._environ.get(ENV_MASTER_KEY, "").strip():
            return f"环境变量 {ENV_MASTER_KEY}"
        return f"文件 {self.key_file}"

    def _read_key(self, *, create: bool) -> bytes | None:
        env_key = self._environ.get(ENV_MASTER_KEY, "").strip()
        if env_key:
            return env_key.encode()
        path = self.key_file
        if path.is_file():
            return path.read_bytes().strip()
        if not create:
            return None
        if self._home is None:
            ensure_home()
        else:
            self._home.mkdir(mode=0o700, parents=True, exist_ok=True)
        key = Fernet.generate_key()
        try:
            create_private_file(path, key + b"\n")
        except FileExistsError:
            return path.read_bytes().strip()
        return key

    def _build(self, key: bytes) -> Fernet:
        try:
            return Fernet(key)
        except (ValueError, TypeError):
            raise MasterKeyError(
                f"主密钥格式无效（来源：{self.key_source}），需为 urlsafe base64 Fernet key"
            ) from None

    def open(self, meta: Mapping[str, str]) -> None:
        """按已有 key_check 加载主密钥；不创建、不重新生成."""
        if self._fernet is not None:
            return
        expected = meta.get("key_id", "?")
        key = self._read_key(create=False)
        if key is None:
            raise MasterKeyMismatch(
                f"缺少主密钥（期望 key_id={expected}，来源：{self.key_source}）。"
                f"请恢复 master.key 或设置 {ENV_MASTER_KEY}"
            )
        fernet = self._build(key)
        check = meta.get("key_check")
        if check:
            try:
                fernet.decrypt(check.encode())
            except InvalidToken:
                raise MasterKeyMismatch(
                    f"主密钥与 config.db 不匹配（来源：{self.key_source}，"
                    f"期望 key_id={expected}，当前 key_id={_key_id(key)}）。"
                    "不会重新生成主密钥，请恢复原密钥"
                ) from None
        self._fernet = fernet
        self.key_id = _key_id(key)

    def ensure_for_write(self, txn: StoreTxn) -> None:
        """写事务内准备主密钥：已有 key_check 则校验，否则加载 / 创建并登记."""
        meta = txn.snapshot.meta
        if meta.get("key_check"):
            self.open(meta)
            return
        key = self._read_key(create=True)
        assert key is not None
        fernet = self._build(key)
        txn.set_meta("key_check", fernet.encrypt(_KEY_CHECK_PLAINTEXT).decode())
        txn.set_meta("key_id", _key_id(key))
        self._fernet = fernet
        self.key_id = _key_id(key)

    def encrypt(self, plaintext: str) -> bytes:
        if self._fernet is None:
            raise MasterKeyError("主密钥尚未加载")
        return self._fernet.encrypt(plaintext.encode("utf-8"))

    def decrypt(self, token: bytes) -> str:
        if self._fernet is None:
            raise MasterKeyError("主密钥尚未加载")
        try:
            return self._fernet.decrypt(token).decode("utf-8")
        except InvalidToken:
            raise MasterKeyMismatch(
                f"密文无法用当前主密钥解密（来源：{self.key_source}）"
            ) from None

    def make_record(self, plaintext: str) -> SecretRecord:
        return SecretRecord(
            kind="stored",
            ciphertext=self.encrypt(plaintext),
            hint=secret_hint(plaintext),
        )


# ── 解析 ────────────────────────────────────────────────────────


def resolve_record(
    record: SecretRecord,
    *,
    cipher: SecretCipher | None,
    environ: Mapping[str, str],
    decrypt: bool,
    plaintext: str | None = None,
) -> tuple[str, bool]:
    """解析单条记录 → ``(value, resolved)``；``decrypt=False`` 时 stored 记为未解析."""
    if record.kind == "env":
        value = environ.get(record.env_var or "", "")
        return value, bool(value)
    if record.kind == "empty":
        return "", False
    if plaintext is not None:
        return plaintext, bool(plaintext)
    if not decrypt or cipher is None or record.ciphertext is None:
        return "", False
    value = cipher.decrypt(record.ciphertext)
    return value, bool(value)


__all__ = [
    "ENV_MASTER_KEY",
    "SECRET_FIELDS_BY_VENDOR",
    "AUTH_SECRET_PATH",
    "LEGACY_VENDOR_SECTIONS",
    "LEGACY_SECRET_FIELDS",
    "SECTION_SECRET_FIELDS",
    "SecretRecord",
    "SecretCipher",
    "MasterKeyError",
    "MasterKeyMismatch",
    "vendor_secret_path",
    "applicable_secret_paths",
    "legacy_secret_paths",
    "strip_section_secrets",
    "is_vendor_secret_path",
    "secret_hint",
    "default_env_var",
    "resolve_record",
]
