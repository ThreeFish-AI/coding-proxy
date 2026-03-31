"""运行时 OAuth 重认证协调器 — 后台触发浏览器登录并热更新凭证."""

from __future__ import annotations

import asyncio
import enum
import logging
import time
from typing import Callable

from .providers.base import OAuthProvider
from .store import TokenStoreManager

logger = logging.getLogger(__name__)


class ReauthState(enum.Enum):
    """重认证状态."""

    IDLE = "idle"
    PENDING = "pending"
    COMPLETED = "completed"
    FAILED = "failed"


class RuntimeReauthCoordinator:
    """运行时 OAuth 重认证协调器.

    当 TokenManager 报告 needs_reauth=True 时，Router 调用
    ``request_reauth()`` 在后台触发浏览器登录流程。

    与熔断器的协同:
    - 重认证期间 TokenManager 持续抛 TokenAcquireError
    - Router 触发 failover → 熔断器 OPEN → 请求路由到下一层级
    - 重认证完成后 TokenManager 获得新凭证 → 熔断器恢复 → 后端可用
    """

    def __init__(
        self,
        token_store: TokenStoreManager,
        providers: dict[str, OAuthProvider],
        token_updaters: dict[str, Callable[[str], None]],
    ) -> None:
        """
        Args:
            token_store: Token 持久化管理器
            providers: provider_name → OAuthProvider 实例
            token_updaters: provider_name → 更新 TokenManager 凭证的回调
        """
        self._token_store = token_store
        self._providers = providers
        self._token_updaters = token_updaters
        self._states: dict[str, ReauthState] = {
            name: ReauthState.IDLE for name in providers
        }
        self._locks: dict[str, asyncio.Lock] = {
            name: asyncio.Lock() for name in providers
        }
        self._last_error: dict[str, str] = {}
        self._last_completed: dict[str, float] = {}

    async def request_reauth(self, provider_name: str) -> None:
        """请求对指定 provider 进行重认证（幂等，后台执行）.

        若已在进行中，直接返回不重复触发。
        """
        if provider_name not in self._providers:
            logger.warning("未知 provider: %s", provider_name)
            return

        if self._states.get(provider_name) == ReauthState.PENDING:
            return  # 已在进行中

        asyncio.create_task(self._do_reauth(provider_name))

    async def _do_reauth(self, provider_name: str) -> None:
        """执行重认证流程（带锁保护的幂等实现）."""
        lock = self._locks[provider_name]
        if lock.locked():
            return  # 另一个任务正在执行

        async with lock:
            self._states[provider_name] = ReauthState.PENDING
            logger.info("开始 %s 重认证...", provider_name)

            try:
                provider = self._providers[provider_name]
                tokens = await provider.login()
                self._token_store.set(provider_name, tokens)

                # 调用热更新回调
                updater = self._token_updaters.get(provider_name)
                if updater:
                    # GitHub → access_token, Google → refresh_token
                    if provider_name == "github":
                        updater(tokens.access_token)
                    elif provider_name == "google":
                        updater(tokens.refresh_token)

                self._states[provider_name] = ReauthState.COMPLETED
                self._last_completed[provider_name] = time.monotonic()
                self._last_error.pop(provider_name, None)
                logger.info("%s 重认证成功", provider_name)

            except Exception as exc:
                self._states[provider_name] = ReauthState.FAILED
                self._last_error[provider_name] = str(exc)
                logger.error("%s 重认证失败: %s", provider_name, exc)

    def get_status(self) -> dict[str, dict[str, str]]:
        """返回所有 provider 的重认证状态."""
        result = {}
        for name in self._providers:
            info: dict[str, str] = {"state": self._states[name].value}
            if name in self._last_error:
                info["error"] = self._last_error[name]
            if name in self._last_completed:
                info["completed_ago_seconds"] = str(
                    int(time.monotonic() - self._last_completed[name])
                )
            result[name] = info
        return result
