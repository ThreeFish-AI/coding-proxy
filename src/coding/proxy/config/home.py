"""运行时 home 目录（``$CODING_PROXY_HOME``，默认 ``~/.coding-proxy``）与私有文件工具.

所有落盘路径（config.db / master.key / admin_token / usage.db / compat.db /
tokens.json / 旧 config.yaml）统一由 :func:`proxy_home` 推导，代码中不再写死
``~/.coding-proxy``。``CODING_PROXY_HOME`` 同时充当多实例 profile 与测试隔离手段。
"""

from __future__ import annotations

import os
import re
import secrets
from pathlib import Path

ENV_HOME = "CODING_PROXY_HOME"
_DEFAULT_HOME = "~/.coding-proxy"


def proxy_home() -> Path:
    """返回 home 目录（纯函数，不创建目录）."""
    raw = os.environ.get(ENV_HOME, "").strip()
    if raw:
        return Path(raw).expanduser().resolve()
    return Path(_DEFAULT_HOME).expanduser()


def ensure_home() -> Path:
    """创建 home 目录（0700）；目录属主为当前用户时收紧权限至 0700."""
    home = proxy_home()
    try:
        home.mkdir(mode=0o700, parents=True, exist_ok=True)
    except OSError as exc:
        from .store import ConfigStoreError

        raise ConfigStoreError(
            f"无法创建配置目录 {home}（{exc.strerror or exc}）。"
            "请检查 CODING_PROXY_HOME 指向的路径及其父目录权限"
        ) from None
    getuid = getattr(os, "getuid", None)  # Windows 无 getuid
    try:
        if getuid is not None and home.stat().st_uid == getuid():
            home.chmod(0o700)
    except OSError:
        pass
    return home


def config_db_path() -> Path:
    return proxy_home() / "config.db"


def master_key_path() -> Path:
    return proxy_home() / "master.key"


def admin_token_path() -> Path:
    return proxy_home() / "admin_token"


def default_usage_db_path() -> Path:
    return proxy_home() / "usage.db"


def default_compat_db_path() -> Path:
    return proxy_home() / "compat.db"


def default_token_store_path() -> Path:
    return proxy_home() / "tokens.json"


def legacy_home_yaml_path() -> Path:
    """旧版 home 下的 config.yaml（仅作为一次性导入源与只读兼容读取）."""
    return proxy_home() / "config.yaml"


# ── Pydantic default_factory（字符串形态，保持与旧字面量展开后一致） ──


def default_usage_db_str() -> str:
    return str(default_usage_db_path())


def default_compat_db_str() -> str:
    return str(default_compat_db_path())


def default_token_store_str() -> str:
    return str(default_token_store_path())


# ── 私有文件 ────────────────────────────────────────────────────


def create_private_file(path: Path, data: bytes) -> None:
    """以 O_EXCL + 0600 创建并写入文件；文件已存在时抛 FileExistsError."""
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        raise
    except OSError as exc:
        from .store import StoreAccessError

        raise StoreAccessError(path, f"创建文件失败: {exc.strerror or exc}") from None
    try:
        with os.fdopen(fd, "wb") as fh:
            fd = -1
            fh.write(data)
    finally:
        if fd >= 0:
            os.close(fd)


def write_private_atomic(path: Path, data: bytes) -> None:
    """原子写入私有文件：同目录 0600 临时文件 + ``os.replace``."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{secrets.token_hex(6)}.tmp")
    create_private_file(tmp, data)
    try:
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


ADMIN_TOKEN_MIN_LENGTH = 32
_TOKEN_RE = re.compile(rf"[A-Za-z0-9_-]{{{ADMIN_TOKEN_MIN_LENGTH},}}")


def is_valid_admin_token(token: str) -> bool:
    """token 至少 32 个 urlsafe 字符；空串 / 过短一律无效（调用方不得视为匹配）."""
    return bool(_TOKEN_RE.fullmatch(token))


def _read_admin_token(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8").strip()
    except (FileNotFoundError, UnicodeDecodeError):
        return ""


def load_or_create_admin_token() -> str:
    """读取 admin token；缺失或无效（空 / 过短 / 非 urlsafe）时原子生成新 token.

    新 token（32 字节 urlsafe）先写入同目录 0600 临时文件：目标不存在时 ``os.link``
    （不存在才创建，并发首启只落地一个完整 token），已存在但无效时 ``os.replace``；
    读取方只会看到完整文件。
    """
    path = admin_token_path()
    token = _read_admin_token(path)
    if is_valid_admin_token(token):
        return token
    ensure_home()
    tmp = path.with_name(f".{path.name}.{secrets.token_hex(6)}.tmp")
    create_private_file(tmp, secrets.token_urlsafe(32).encode())
    try:
        try:
            os.link(tmp, path)
        except FileExistsError:
            if not is_valid_admin_token(_read_admin_token(path)):
                os.replace(tmp, path)
        except OSError:  # 文件系统不支持硬链接
            os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)
    return _read_admin_token(path)


__all__ = [
    "ENV_HOME",
    "proxy_home",
    "ensure_home",
    "config_db_path",
    "master_key_path",
    "admin_token_path",
    "default_usage_db_path",
    "default_compat_db_path",
    "default_token_store_path",
    "legacy_home_yaml_path",
    "default_usage_db_str",
    "default_compat_db_str",
    "default_token_store_str",
    "create_private_file",
    "write_private_atomic",
    "ADMIN_TOKEN_MIN_LENGTH",
    "is_valid_admin_token",
    "load_or_create_admin_token",
]
