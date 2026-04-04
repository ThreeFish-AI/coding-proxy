"""Anthropic 官方供应商 — 透传 OAuth token."""

from __future__ import annotations

from typing import Any

from ..config.schema import AnthropicConfig, FailoverConfig
from .base import PROXY_SKIP_HEADERS, BaseVendor


class AnthropicVendor(BaseVendor):
    """Anthropic 官方 API 供应商.

    透传 Claude Code 发来的 OAuth token 和请求体到 Anthropic API.
    """

    def __init__(self, config: AnthropicConfig, failover_config: FailoverConfig) -> None:
        super().__init__(config.base_url, config.timeout_ms, failover_config)

    def get_name(self) -> str:
        return "anthropic"

    async def check_health(self) -> bool:
        """Anthropic 健康检查 — 透明代理被动策略.

        Anthropic 供应商作为透明代理，不管理凭证（auth 来自客户端请求头），
        无法独立发起 API 探测。健康状态通过 VendorTier 的 Rate Limit
        Deadline 门控判定：仅在服务端声明的 rate limit 重置时间到期后，
        才允许使用下一个真实客户端请求作为探针。
        """
        return True

    async def _prepare_request(
        self,
        request_body: dict[str, Any],
        headers: dict[str, str],
    ) -> tuple[dict[str, Any], dict[str, str]]:
        """透传请求体，过滤无关请求头."""
        filtered = {k: v for k, v in headers.items() if k.lower() not in PROXY_SKIP_HEADERS}
        return request_body, filtered


# 向后兼容别名
AnthropicBackend = AnthropicVendor
