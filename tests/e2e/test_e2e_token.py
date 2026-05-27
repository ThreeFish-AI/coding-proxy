"""Level 1 E2E: Google OAuth2 Token 刷新 — 验证真实凭证链路."""

from __future__ import annotations

import pytest

from coding.proxy.vendors.antigravity import GoogleOAuthTokenManager
from coding.proxy.vendors.token_manager import TokenAcquireError, TokenErrorKind


@pytest.mark.e2e
@pytest.mark.asyncio
async def test_real_token_refresh(e2e_credentials: dict[str, str]) -> None:
    """真实 refresh_token 应返回有效的 access_token（ya29. 前缀）."""
    tm = GoogleOAuthTokenManager(
        e2e_credentials["client_id"],
        e2e_credentials["client_secret"],
        e2e_credentials["refresh_token"],
    )
    try:
        token = await tm.get_token()
        assert token, "access_token 为空"
        assert token.startswith("ya29."), f"access_token 前缀异常: {token[:10]}..."
        print(f"[E2E DIAG] access_token={token[:10]}... (len={len(token)})")
    finally:
        await tm.close()


@pytest.mark.e2e
@pytest.mark.asyncio
async def test_real_token_caching(e2e_credentials: dict[str, str]) -> None:
    """连续调用 get_token() 应返回缓存的同一 token."""
    tm = GoogleOAuthTokenManager(
        e2e_credentials["client_id"],
        e2e_credentials["client_secret"],
        e2e_credentials["refresh_token"],
    )
    try:
        token1 = await tm.get_token()
        token2 = await tm.get_token()
        assert token1 == token2, "缓存未生效，两次返回不同 token"
        assert tm._expires_at > 0, "expires_at 未被设置"
        print(f"[E2E DIAG] caching OK: expires_at={tm._expires_at}")
    finally:
        await tm.close()


@pytest.mark.e2e
@pytest.mark.asyncio
async def test_invalid_refresh_token_raises(e2e_credentials: dict[str, str]) -> None:
    """错误的 refresh_token 应抛出 TokenAcquireError(INVALID_CREDENTIALS)."""
    tm = GoogleOAuthTokenManager(
        e2e_credentials["client_id"],
        e2e_credentials["client_secret"],
        "1//invalid_token_for_e2e_test_00000000",
    )
    try:
        with pytest.raises(TokenAcquireError) as exc_info:
            await tm.get_token()
        assert exc_info.value.kind == TokenErrorKind.INVALID_CREDENTIALS, (
            f"预期 INVALID_CREDENTIALS，实际: {exc_info.value.kind}"
        )
        assert exc_info.value.needs_reauth is True
        print(f"[E2E DIAG] invalid_grant 正确捕获: {exc_info.value}")
    finally:
        await tm.close()


@pytest.mark.e2e
@pytest.mark.asyncio
async def test_token_invalidation_triggers_refresh(
    e2e_credentials: dict[str, str],
) -> None:
    """invalidate() 后重新获取应成功."""
    tm = GoogleOAuthTokenManager(
        e2e_credentials["client_id"],
        e2e_credentials["client_secret"],
        e2e_credentials["refresh_token"],
    )
    try:
        token1 = await tm.get_token()
        assert token1, "首次获取失败"

        tm.invalidate()
        assert tm._expires_at == 0.0, "invalidate 后 expires_at 应为 0"

        token2 = await tm.get_token()
        assert token2, "invalidate 后重新获取失败"
        print(
            f"[E2E DIAG] invalidation OK: token1={token1[:10]}... token2={token2[:10]}..."
        )
    finally:
        await tm.close()
