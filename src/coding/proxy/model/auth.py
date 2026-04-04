"""认证凭证数据模型.

从 :mod:`coding.proxy.auth.store` 正交提取 ``ProviderTokens`` Pydantic model。
``TokenStoreManager`` 持久化管理器保留在原模块。
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel


class ProviderTokens(BaseModel):
    """单个 Provider 的 Token 凭证."""

    access_token: str = ""
    refresh_token: str = ""
    expires_at: float = 0.0  # Unix timestamp
    scope: str = ""
    token_type: str = "bearer"
    extra: dict[str, Any] = {}

    @property
    def is_expired(self) -> bool:
        """检查 access_token 是否已过期（含 60 秒余量）."""
        return self.expires_at > 0 and __import__("time").time() > self.expires_at - 60

    @property
    def has_credentials(self) -> bool:
        """是否有可用凭证（access_token 或 refresh_token）."""
        return bool(self.access_token or self.refresh_token)
