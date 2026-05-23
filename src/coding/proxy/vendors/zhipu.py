"""智谱 GLM 供应商 — 原生 Anthropic 兼容端点透传代理.

官方端点 (https://open.bigmodel.cn/api/anthropic) 已完整支持
Anthropic Messages API 协议，本模块仅做三项最小适配：
  1. 模型名映射（Claude -> GLM）
  2. 认证头替换（x-api-key）
  3. 请求参数清洗（剥离 GLM 不支持的 Anthropic 扩展字段）
"""

from __future__ import annotations

import logging
from typing import Any

from ..config.schema import FailoverConfig, ZhipuConfig
from ..routing.model_mapper import ModelMapper
from .native_anthropic import NativeAnthropicVendor

logger = logging.getLogger(__name__)


class ZhipuVendor(NativeAnthropicVendor):
    """智谱 GLM 原生 Anthropic 兼容端点供应商.

    通过官方 /api/anthropic 端点转发请求，
    替换模型名和认证头，并剥离 GLM 不支持的 Anthropic 扩展参数：
    - cache_control 字段（GLM 不支持 Anthropic prompt caching）
    - thinking / extended_thinking / reasoning_effort 顶层参数
    """

    _vendor_name = "zhipu"
    _display_name = "Zhipu"

    def __init__(
        self,
        config: ZhipuConfig,
        model_mapper: ModelMapper,
        failover_config: FailoverConfig | None = None,
    ) -> None:
        super().__init__(config, model_mapper, failover_config)

    async def _prepare_request(
        self,
        request_body: dict[str, Any],
        headers: dict[str, str],
    ) -> tuple[dict[str, Any], dict[str, str]]:
        """深拷贝 + 模型映射 + 认证头替换 + GLM 兼容性清洗.

        在父类 deep copy + model mapping + header 替换的基础上，
        增加剥离 GLM 不支持的 Anthropic 扩展参数。
        """
        body, new_headers = await super()._prepare_request(request_body, headers)

        from ..convert.vendor_channels import normalize_for_zhipu

        _, adaptations = normalize_for_zhipu(body)
        if adaptations:
            logger.debug(
                "zhipu: applied first-tier normalization: %s",
                ", ".join(adaptations),
            )

        return body, new_headers


# 向后兼容别名
ZhipuBackend = ZhipuVendor
