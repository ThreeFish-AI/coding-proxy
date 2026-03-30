"""Anthropic 官方后端 — 透传 OAuth token."""

from __future__ import annotations

from typing import Any

from ..config.schema import AnthropicConfig, FailoverConfig
from .base import BaseBackend

_SKIP_HEADERS = {"host", "content-length", "transfer-encoding", "connection"}


class AnthropicBackend(BaseBackend):
    """Anthropic 官方 API 后端.

    透传 Claude Code 发来的 OAuth token 和请求体到 Anthropic API.
    """

    def __init__(self, config: AnthropicConfig, failover_config: FailoverConfig) -> None:
        super().__init__(config.base_url, config.timeout_ms)
        self._failover_config = failover_config

    def get_name(self) -> str:
        return "anthropic"

    def _prepare_request(
        self,
        request_body: dict[str, Any],
        headers: dict[str, str],
    ) -> tuple[dict[str, Any], dict[str, str]]:
        """透传请求体，过滤无关请求头."""
        filtered = {k: v for k, v in headers.items() if k.lower() not in _SKIP_HEADERS}
        return request_body, filtered

    def should_trigger_failover(self, status_code: int, body: dict[str, Any] | None) -> bool:
        """判断是否应触发故障转移."""
        if status_code not in self._failover_config.status_codes:
            return False

        if body and "error" in body:
            error = body["error"]
            error_type = error.get("type", "")
            error_message = error.get("message", "").lower()

            if error_type in self._failover_config.error_types:
                return True

            for pattern in self._failover_config.error_message_patterns:
                if pattern.lower() in error_message:
                    return True

        # 对于 429 和 503，即使无法解析 body 也触发故障转移
        return status_code in (429, 503)
