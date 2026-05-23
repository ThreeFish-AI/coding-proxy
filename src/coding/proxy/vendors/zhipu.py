"""智谱 GLM 供应商 — 原生 Anthropic 兼容端点薄透传代理.

官方端点 (https://open.bigmodel.cn/api/anthropic) 已完整支持
Anthropic Messages API 协议，本模块仅做两项最小适配：
  1. 模型名映射（Claude -> GLM）
  2. 认证头替换（x-api-key）

注意：实测验证 GLM 的 Anthropic 兼容端点对以下参数的处理方式：
- thinking 参数：原生支持（GLM 有自己的 thinking 机制）
- cache_control 字段：静默忽略（GLM 使用隐式自动缓存）
- reasoning_effort 参数：静默忽略
以上参数均不会导致 400 错误，因此不需要在 _prepare_request 中剥离。
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
