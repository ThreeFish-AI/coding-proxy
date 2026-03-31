"""OAuth Provider 抽象基类."""

from __future__ import annotations

from abc import ABC, abstractmethod

from ..store import ProviderTokens


class OAuthProvider(ABC):
    """OAuth 登录提供者抽象基类."""

    @abstractmethod
    def get_name(self) -> str:
        """返回 Provider 唯一标识（用于 Token Store 的 key）."""

    @abstractmethod
    async def login(self) -> ProviderTokens:
        """执行 OAuth 登录流程，返回获取到的 Token."""

    @abstractmethod
    async def refresh(self, tokens: ProviderTokens) -> ProviderTokens:
        """使用 refresh_token 刷新 access_token."""

    @abstractmethod
    async def validate(self, tokens: ProviderTokens) -> bool:
        """验证当前 Token 是否仍然有效."""

    def needs_login(self, tokens: ProviderTokens) -> bool:
        """判断是否需要重新登录（默认：无凭证或已过期且无 refresh_token）."""
        if not tokens.has_credentials:
            return True
        if tokens.is_expired and not tokens.refresh_token:
            return True
        return False
