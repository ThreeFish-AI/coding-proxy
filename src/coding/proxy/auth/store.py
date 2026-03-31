"""Token 持久化存储 — ~/.coding-proxy/tokens.json."""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Any

from pydantic import BaseModel

logger = logging.getLogger(__name__)

_DEFAULT_STORE_PATH = Path("~/.coding-proxy/tokens.json")


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
        return self.expires_at > 0 and time.time() > self.expires_at - 60

    @property
    def has_credentials(self) -> bool:
        """是否有可用凭证（access_token 或 refresh_token）."""
        return bool(self.access_token or self.refresh_token)


class TokenStoreManager:
    """管理所有 Provider 的 Token 持久化."""

    def __init__(self, store_path: Path | None = None) -> None:
        self._path = (store_path or _DEFAULT_STORE_PATH).expanduser()
        self._data: dict[str, dict[str, Any]] = {}

    def load(self) -> None:
        """从磁盘加载 Token 存储."""
        if self._path.exists():
            try:
                with open(self._path) as f:
                    self._data = json.load(f)
                logger.debug("Token store loaded from %s", self._path)
            except (json.JSONDecodeError, OSError) as exc:
                logger.warning("Failed to load token store: %s", exc)
                self._data = {}
        else:
            self._data = {}

    def save(self) -> None:
        """持久化 Token 到磁盘."""
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with open(self._path, "w") as f:
            json.dump(self._data, f, indent=2, ensure_ascii=False)
        # 限制文件权限为仅 owner 可读写
        self._path.chmod(0o600)
        logger.debug("Token store saved to %s", self._path)

    def get(self, provider: str) -> ProviderTokens:
        """获取指定 Provider 的 Token."""
        raw = self._data.get(provider, {})
        return ProviderTokens(**raw) if raw else ProviderTokens()

    def set(self, provider: str, tokens: ProviderTokens) -> None:
        """设置指定 Provider 的 Token 并持久化."""
        self._data[provider] = tokens.model_dump()
        self.save()
        logger.info("Token updated for provider: %s", provider)

    def remove(self, provider: str) -> None:
        """移除指定 Provider 的 Token."""
        if provider in self._data:
            del self._data[provider]
            self.save()

    def list_providers(self) -> list[str]:
        """列出所有已存储 Token 的 Provider."""
        return list(self._data.keys())
