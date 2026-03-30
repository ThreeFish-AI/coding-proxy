"""Anthropic 官方后端 — 透传 OAuth token."""

from __future__ import annotations

from typing import Any

from ..config.schema import AnthropicConfig, FailoverConfig
from .base import PROXY_SKIP_HEADERS, BaseBackend


class AnthropicBackend(BaseBackend):
    """Anthropic 官方 API 后端.

    透传 Claude Code 发来的 OAuth token 和请求体到 Anthropic API.
    """

    def __init__(self, config: AnthropicConfig, failover_config: FailoverConfig) -> None:
        super().__init__(config.base_url, config.timeout_ms, failover_config)

    def get_name(self) -> str:
        return "anthropic"

    def _prepare_request(
        self,
        request_body: dict[str, Any],
        headers: dict[str, str],
    ) -> tuple[dict[str, Any], dict[str, str]]:
        """透传请求体，过滤无关请求头."""
        filtered = {k: v for k, v in headers.items() if k.lower() not in PROXY_SKIP_HEADERS}
        return request_body, filtered
