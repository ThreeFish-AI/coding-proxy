"""豆包 Doubao 供应商 — 原生 Anthropic 兼容端点薄透传代理.

端点 (https://ark.cn-beijing.volces.com/api/coding) 已完整支持
Anthropic Messages API 协议，仅做模型名映射和认证头替换。
"""

from __future__ import annotations

from ..config.schema import FailoverConfig
from ..config.vendors import DoubaoConfig
from ..routing.model_mapper import ModelMapper
from .native_anthropic import NativeAnthropicVendor


class DoubaoVendor(NativeAnthropicVendor):
    """豆包 Doubao 原生 Anthropic 兼容端点供应商（薄透传）."""

    _vendor_name = "doubao"
    _display_name = "Doubao"

    def __init__(
        self,
        config: DoubaoConfig,
        model_mapper: ModelMapper,
        failover_config: FailoverConfig | None = None,
    ) -> None:
        super().__init__(config, model_mapper, failover_config)
