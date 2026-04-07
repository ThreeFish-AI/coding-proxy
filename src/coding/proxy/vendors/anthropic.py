"""Anthropic 官方供应商 — 透传 OAuth token."""

from __future__ import annotations

import copy
import logging
from typing import Any

from ..config.schema import AnthropicConfig, FailoverConfig
from .base import PROXY_SKIP_HEADERS, BaseVendor

logger = logging.getLogger(__name__)

# 需要从 assistant messages 中剥离的 thinking block 类型
_THINKING_BLOCK_TYPES = {"thinking", "redacted_thinking"}


def _strip_thinking_blocks(body: dict[str, Any]) -> int:
    """从 assistant messages 中移除 thinking / redacted_thinking blocks.

    Anthropic API 要求 thinking blocks 的 ``signature`` 必须是其签发的有效签名。
    跨供应商迁移（如 Zhipu → Anthropic）后，conversation history 中可能包含
    非 Anthropic 签发的 signature，导致 400 ``invalid_request_error``。
    根据 Anthropic 官方文档，thinking blocks 可以被安全省略，不影响模型行为。

    Returns:
        被移除的 thinking block 数量。
    """
    stripped = 0
    for message in body.get("messages", []):
        if not isinstance(message, dict) or message.get("role") != "assistant":
            continue
        content = message.get("content")
        if not isinstance(content, list):
            continue
        original_len = len(content)
        new_content = [
            block
            for block in content
            if not (
                isinstance(block, dict) and block.get("type") in _THINKING_BLOCK_TYPES
            )
        ]
        removed = original_len - len(new_content)
        if removed and not new_content:
            logger.warning(
                "anthropic: assistant message content became empty after "
                "stripping %d thinking block(s); this may cause API errors",
                removed,
            )
        message["content"] = new_content
        stripped += removed
    return stripped


class AnthropicVendor(BaseVendor):
    """Anthropic 官方 API 供应商.

    透传 Claude Code 发来的 OAuth token 和请求体到 Anthropic API.
    """

    def __init__(
        self, config: AnthropicConfig, failover_config: FailoverConfig
    ) -> None:
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
        """深拷贝请求体、剥离历史 thinking blocks，过滤无关请求头.

        深拷贝确保 Anthropic 的请求体修改不会污染后续 tier 的输入。
        剥离 thinking blocks 防止跨供应商 signature 不兼容导致 400 错误。
        """
        body = copy.deepcopy(request_body)
        stripped = _strip_thinking_blocks(body)
        if stripped:
            logger.debug(
                "anthropic: stripped %d thinking block(s) from conversation history",
                stripped,
            )

        filtered = {
            k: v for k, v in headers.items() if k.lower() not in PROXY_SKIP_HEADERS
        }
        return body, filtered


# 向后兼容别名
AnthropicBackend = AnthropicVendor
