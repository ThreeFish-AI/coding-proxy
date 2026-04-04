"""ProviderTokens (coding.proxy.model.auth) 单元测试."""

import time

from coding.proxy.model.auth import ProviderTokens


class TestProviderTokensDefault:
    """默认构造行为验证."""


def test_default_construction():
    """所有字段应取声明中的默认值."""
    tokens = ProviderTokens()
    assert tokens.access_token == ""
    assert tokens.refresh_token == ""
    assert tokens.expires_at == 0.0
    assert tokens.scope == ""
    assert tokens.token_type == "bearer"
    assert tokens.extra == {}


def test_custom_field_values():
    """自定义字段值应正确赋值."""
    tokens = ProviderTokens(
        access_token="at-abc123",
        refresh_token="rt-xyz789",
        expires_at=9999999999.0,
        scope="read write",
        token_type="mac",
        extra={"key": "value"},
    )
    assert tokens.access_token == "at-abc123"
    assert tokens.refresh_token == "rt-xyz789"
    assert tokens.expires_at == 9999999999.0
    assert tokens.scope == "read write"
    assert tokens.token_type == "mac"
    assert tokens.extra == {"key": "value"}


def test_is_expired_not_expired_future():
    """expires_at 在未来（含 60s 余量）时 is_expired 应为 False."""
    future_ts = time.time() + 3600  # 1 小时后
    tokens = ProviderTokens(expires_at=future_ts)
    assert tokens.is_expired is False


def test_is_expired_expired_past():
    """expires_at 已过期（含 60s 余量）时 is_expired 应为 True."""
    past_ts = time.time() - 120  # 2 分钟前
    tokens = ProviderTokens(expires_at=past_ts)
    assert tokens.is_expired is True


def test_is_expired_zero_expires_at():
    """expires_at=0 表示未设置，is_expired 应为 False."""
    tokens = ProviderTokens(expires_at=0.0)
    assert tokens.is_expired is False


def test_has_credentials_with_access_token():
    """access_token 非空时 has_credentials 应为 True."""
    tokens = ProviderTokens(access_token="at-foo")
    assert tokens.has_credentials is True


def test_has_credentials_with_refresh_token_only():
    """仅 refresh_token 非空时 has_credentials 也应为 True."""
    tokens = ProviderTokens(refresh_token="rt-bar")
    assert tokens.has_credentials is True


def test_has_credentials_both_empty():
    """access_token 与 refresh_token 均为空时 has_credentials 应为 False."""
    tokens = ProviderTokens()
    assert tokens.has_credentials is False


def test_model_dump_round_trip():
    """model_dump 序列化后应能通过 model_validate 还原等价实例."""
    original = ProviderTokens(
        access_token="at-roundtrip",
        refresh_token="rt-roundtrip",
        expires_at=1234567890.0,
        scope="test-scope",
        token_type="bearer",
        extra={"custom_field": 42},
    )
    data = original.model_dump()
    restored = ProviderTokens.model_validate(data)

    assert restored.access_token == original.access_token
    assert restored.refresh_token == original.refresh_token
    assert restored.expires_at == original.expires_at
    assert restored.scope == original.scope
    assert restored.token_type == original.token_type
    assert restored.extra == original.extra
