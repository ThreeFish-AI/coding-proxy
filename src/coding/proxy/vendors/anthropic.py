"""Anthropic 官方供应商 — 透传 OAuth token."""

from __future__ import annotations

import copy
import logging
from typing import Any

from ..config.schema import AnthropicConfig, FailoverConfig
from .base import PROXY_SKIP_HEADERS, BaseVendor

logger = logging.getLogger(__name__)


def _strip_misplaced_tool_results(body: dict[str, Any]) -> int:
    """从非 user 角色的消息中剥离 tool_result blocks（纵深防御）.

    Anthropic API 严格要求 ``tool_result`` blocks 只能出现在 ``user`` messages 中。
    跨供应商迁移场景（如 Zhipu GLM → Anthropic），GLM-5 可能在 assistant 响应中
    同时包含 ``tool_use`` 和 ``tool_result`` 内容块，导致 Claude Code 将其存入
    conversation history 后，后续请求的 assistant message 中包含 ``tool_result``。

    源→目标转换通道（``convert/vendor_channels.py`` 中的 ``prepare_zhipu_to_anthropic``）
    会在路由阶段处理此场景，本函数提供纵深防御。

    Returns:
        被移除的 tool_result block 数量。
    """
    stripped = 0
    for message in body.get("messages", []):
        if not isinstance(message, dict):
            continue
        role = message.get("role")
        if role == "user":
            continue
        content = message.get("content")
        if not isinstance(content, list):
            continue
        original_len = len(content)
        new_content = [
            block
            for block in content
            if not (isinstance(block, dict) and block.get("type") == "tool_result")
        ]
        removed = original_len - len(new_content)
        if removed:
            if not new_content:
                # 剥离所有 tool_result 后 content 为空，插入占位 text block
                new_content = [{"type": "text", "text": ""}]
                logger.info(
                    "anthropic: inserted empty text placeholder after stripping "
                    "%d misplaced tool_result block(s) from %s message",
                    removed,
                    role,
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
        """深拷贝请求体、剥离错位的 tool_result blocks，过滤无关请求头.

        深拷贝确保 Anthropic 的请求体修改不会污染后续 tier 的输入。
        thinking block 剥离已提升至 executor 层条件执行（仅跨供应商场景）。
        剥离错位的 tool_result blocks 防止跨供应商 tool_result 放置位置不合规导致 400 错误。
        """
        body = copy.deepcopy(request_body)

        stripped_tool_results = _strip_misplaced_tool_results(body)
        if stripped_tool_results:
            logger.info(
                "anthropic: stripped %d misplaced tool_result block(s) from "
                "conversation history (defense-in-depth)",
                stripped_tool_results,
            )

        filtered = {
            k: v for k, v in headers.items() if k.lower() not in PROXY_SKIP_HEADERS
        }
        return body, filtered


# 向后兼容别名
AnthropicBackend = AnthropicVendor
