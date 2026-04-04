"""Token 管理相关类型 — 枚举、异常与诊断数据类.

从 :mod:`coding.proxy.backends.token_manager` 正交提取纯声明式类型定义。
``BaseTokenManager`` 抽象基类保留在原模块。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class TokenErrorKind(Enum):
    """Token 获取失败分类."""

    TEMPORARY = "temporary"
    INVALID_CREDENTIALS = "invalid_credentials"
    PERMISSION_UPGRADE_REQUIRED = "permission_upgrade_required"
    INSUFFICIENT_SCOPE = "insufficient_scope"


class TokenAcquireError(Exception):
    """Token 获取失败.

    needs_reauth=True 表示长期凭证已失效，需要重新执行浏览器 OAuth 登录。
    needs_reauth=False 表示临时性故障（网络超时等），可自动恢复。
    """

    def __init__(self, message: str, *, needs_reauth: bool = False) -> None:
        super().__init__(message)
        self.needs_reauth = needs_reauth
        self.kind = TokenErrorKind.TEMPORARY

    @classmethod
    def with_kind(
        cls,
        message: str,
        *,
        kind: TokenErrorKind,
        needs_reauth: bool = False,
    ) -> "TokenAcquireError":
        err = cls(message, needs_reauth=needs_reauth)
        err.kind = kind
        return err


@dataclass
class TokenManagerDiagnostics:
    """TokenManager 最近一次失败诊断信息."""

    last_error: str = ""
    needs_reauth: bool = False
    error_kind: str = ""
    updated_at: float = 0.0

    def to_dict(self) -> dict[str, str | bool]:
        if not self.last_error:
            return {}
        return {
            "last_error": self.last_error,
            "needs_reauth": self.needs_reauth,
            "error_kind": self.error_kind,
            "updated_at_unix": round(self.updated_at, 3),
        }
