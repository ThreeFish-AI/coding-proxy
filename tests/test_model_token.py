"""Token 模型 (coding.proxy.model.token) 单元测试.

覆盖 TokenErrorKind 枚举、TokenAcquireError 异常、TokenManagerDiagnostics 数据类。
"""

from coding.proxy.model.token import (
    TokenAcquireError,
    TokenErrorKind,
    TokenManagerDiagnostics,
)


# ---------------------------------------------------------------------------
# TokenErrorKind
# ---------------------------------------------------------------------------

class TestTokenErrorKind:
    """枚举值完整性验证."""


def test_enum_members():
    """枚举应包含全部四个预定义成员."""
    assert set(TokenErrorKind) == {
        TokenErrorKind.TEMPORARY,
        TokenErrorKind.INVALID_CREDENTIALS,
        TokenErrorKind.PERMISSION_UPGRADE_REQUIRED,
        TokenErrorKind.INSUFFICIENT_SCOPE,
    }


def test_enum_values():
    """各枚举成员的 value 应与声明一致."""
    assert TokenErrorKind.TEMPORARY.value == "temporary"
    assert TokenErrorKind.INVALID_CREDENTIALS.value == "invalid_credentials"
    assert TokenErrorKind.PERMISSION_UPGRADE_REQUIRED.value == "permission_upgrade_required"
    assert TokenErrorKind.INSUFFICIENT_SCOPE.value == "insufficient_scope"


def test_enum_from_string():
    """应能通过字符串值反向查找枚举成员."""
    assert TokenErrorKind("temporary") is TokenErrorKind.TEMPORARY
    assert TokenErrorKind("invalid_credentials") is TokenErrorKind.INVALID_CREDENTIALS


# ---------------------------------------------------------------------------
# TokenAcquireError
# ---------------------------------------------------------------------------

class TestTokenAcquireError:
    """异常构造与属性验证."""


def test_default_construction():
    """默认 needs_reauth=False, kind=TEMPORARY."""
    err = TokenAcquireError("network timeout")
    assert str(err) == "network timeout"
    assert err.needs_reauth is False
    assert err.kind is TokenErrorKind.TEMPORARY


def test_needs_reauth_true():
    """显式传入 needs_reauth=True 应正确赋值."""
    err = TokenAcquireError("token revoked", needs_reauth=True)
    assert err.needs_reauth is True
    assert err.kind is TokenErrorKind.TEMPORARY  # kind 不受 needs_reauth 影响


def test_with_kind_classmethod():
    """with_kind 应同时设置 message / kind / needs_reauth."""
    err = TokenAcquireError.with_kind(
        "scope too narrow",
        kind=TokenErrorKind.INSUFFICIENT_SCOPE,
        needs_reauth=True,
    )
    assert str(err) == "scope too narrow"
    assert err.kind is TokenErrorKind.INSUFFICIENT_SCOPE
    assert err.needs_reauth is True


def test_with_kind_default_needs_reauth():
    """with_kind 未传 needs_reauth 时默认为 False."""
    err = TokenAcquireError.with_kind(
        "bad creds",
        kind=TokenErrorKind.INVALID_CREDENTIALS,
    )
    assert err.needs_reauth is False
    assert err.kind is TokenErrorKind.INVALID_CREDENTIALS


def test_is_exception_subclass():
    """TokenAcquireError 必须是 Exception 的子类（可 raise / except）."""
    assert issubclass(TokenAcquireError, Exception)

    with __import__("pytest").raises(TokenAcquireError, match="boom"):
        raise TokenAcquireError("boom")


# ---------------------------------------------------------------------------
# TokenManagerDiagnostics
# ---------------------------------------------------------------------------

class TestTokenManagerDiagnostics:
    """诊断数据类验证."""


def test_default_values():
    """所有字段应取声明的默认值."""
    diag = TokenManagerDiagnostics()
    assert diag.last_error == ""
    assert diag.needs_reauth is False
    assert diag.error_kind == ""
    assert diag.updated_at == 0.0


def test_custom_values():
    """自定义字段值应正确赋值."""
    diag = TokenManagerDiagnostics(
        last_error="rate limited",
        needs_reauth=True,
        error_kind="insufficient_scope",
        updated_at=1712345678.123,
    )
    assert diag.last_error == "rate limited"
    assert diag.needs_reauth is True
    assert diag.error_kind == "insufficient_scope"
    assert diag.updated_at == 1712345678.123


def test_to_dict_with_error():
    """有错误信息时 to_dict 返回完整字典，updated_at 保留三位小数."""
    diag = TokenManagerDiagnostics(
        last_error="forbidden",
        needs_reauth=True,
        error_kind="permission_upgrade_required",
        updated_at=1712345678.123456,
    )
    result = diag.to_dict()
    assert result == {
        "last_error": "forbidden",
        "needs_reauth": True,
        "error_kind": "permission_upgrade_required",
        "updated_at_unix": 1712345678.123,
    }


def test_to_dict_no_error():
    """无错误信息时 to_dict 返回空字典."""
    diag = TokenManagerDiagnostics()
    assert diag.to_dict() == {}


def test_to_dict_empty_last_error():
    """last_error 为空字符串时视为无错误，返回 {}."""
    diag = TokenManagerDiagnostics(last_error="", error_kind="temp")
    assert diag.to_dict() == {}
