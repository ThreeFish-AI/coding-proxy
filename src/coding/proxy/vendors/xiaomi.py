"""小米 MiMo 供应商 — 原生 Anthropic 兼容端点薄透传代理.

端点 (https://token-plan-cn.xiaomimimo.com/anthropic) 已完整支持
Anthropic Messages API 协议，仅做模型名映射和认证头替换。
"""

from __future__ import annotations

from ..config.schema import FailoverConfig
from ..config.vendors import XiaomiConfig
from ..routing.model_mapper import ModelMapper
from .native_anthropic import NativeAnthropicVendor


class XiaomiVendor(NativeAnthropicVendor):
    """小米 MiMo 原生 Anthropic 兼容端点供应商（薄透传）."""

    _vendor_name = "xiaomi"
    _display_name = "Xiaomi"

    def __init__(
        self,
        config: XiaomiConfig,
        model_mapper: ModelMapper,
        failover_config: FailoverConfig | None = None,
    ) -> None:
        super().__init__(config, model_mapper, failover_config)
