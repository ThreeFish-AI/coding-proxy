"""智谱 GLM 供应商 — 原生 Anthropic 兼容端点薄透传代理.

官方端点 (https://open.bigmodel.cn/api/anthropic) 已完整支持
Anthropic Messages API 协议，本模块仅做两项最小适配：
  1. 模型名映射（Claude -> GLM）
  2. 认证头替换（x-api-key）
"""

from __future__ import annotations

from ..config.schema import FailoverConfig, ZhipuConfig
from ..routing.model_mapper import ModelMapper
from .native_anthropic import NativeAnthropicVendor


class ZhipuVendor(NativeAnthropicVendor):
    """智谱 GLM 原生 Anthropic 兼容端点供应商（薄透传）.

    通过官方 /api/anthropic 端点转发请求，
    仅替换模型名和认证头，其余原样透传。
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


# 向后兼容别名
ZhipuBackend = ZhipuVendor
