"""OAuth 认证配置模型."""

from __future__ import annotations

from pydantic import BaseModel


class AuthConfig(BaseModel):
    """OAuth 登录配置.

    .. note::
        各 Provider 的硬编码默认值（google.py / github.py）应与此配置保持同步。
        此配置作为运行时注入值的权威来源（Single Source of Truth）。
    """

    github_client_id: str = "Iv1.b507a08c87ecfe98"
    google_client_id: str = (
        "1071006060591-tmhssin2h21lcre235vtolojh4g403ep"
        ".apps.googleusercontent.com"
    )
    google_client_secret: str = "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"
    token_store_path: str = "~/.coding-proxy/tokens.json"

__all__ = ["AuthConfig"]
