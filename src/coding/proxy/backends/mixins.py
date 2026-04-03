"""后端 Mixin — 消除 Token 后端间的重复模式."""

from __future__ import annotations

import logging
from typing import Any

from .token_manager import BaseTokenManager

logger = logging.getLogger(__name__)


class TokenBackendMixin:
    """提供基于 TokenManager 的后端通用能力.

    使用方式::
        class MyBackend(TokenBackendMixin, BaseBackend):
            def __init__(self, ...):
                TokenBackendMixin.__init__(self, token_manager)
                BaseBackend.__init__(self, ...)

    提供:
    - _on_error_status: 401/403 时自动 invalidate token
    - check_health: 基于 token 可获取性的健康检查
    - 标准诊断字段追踪（_last_requested_model / _last_resolved_model /
      _last_model_resolution_reason / _last_request_adaptations）
    """

    _token_manager: BaseTokenManager

    # 诊断追踪字段
    _last_requested_model: str = ""
    _last_resolved_model: str = ""
    _last_model_resolution_reason: str = ""
    _last_request_adaptations: list[str] = []  # type: ignore[assignment]

    def __init__(self, token_manager: BaseTokenManager) -> None:
        self._token_manager = token_manager

    def _on_error_status(self, status_code: int) -> None:
        """401/403 时标记 token 失效以触发被动刷新."""
        if status_code in (401, 403):
            self._token_manager.invalidate()

    async def check_health(self) -> bool:
        """基于 token 可用性的健康检查."""
        try:
            token = await self._token_manager.get_token()
            return bool(token)
        except Exception:
            logger.warning(
                "%s health check failed: token refresh error",
                getattr(self, "get_name", lambda: "unknown")(),
            )
            return False

    def _get_token_diagnostics(self) -> dict[str, Any]:
        """收集 token 相关诊断信息."""
        diagnostics: dict[str, Any] = {}
        tm_diag = self._token_manager.get_diagnostics()
        if tm_diag:
            diagnostics["token_manager"] = tm_diag
        if self._last_request_adaptations:
            diagnostics["request_adaptations"] = self._last_request_adaptations
        if self._last_requested_model:
            diagnostics["requested_model"] = self._last_requested_model
        if self._last_resolved_model:
            diagnostics["resolved_model"] = self._last_resolved_model
        if self._last_model_resolution_reason:
            diagnostics["model_resolution_reason"] = self._last_model_resolution_reason
        return diagnostics
