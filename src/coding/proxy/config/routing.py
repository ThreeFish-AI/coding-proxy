"""路由层配置模型（后端类型、Tier、模型映射、定价）."""

from __future__ import annotations

import logging
import re
from typing import Annotated, Any, Literal

from pydantic import BaseModel, BeforeValidator, Field, PrivateAttr, model_validator

from .resiliency import CircuitBreakerConfig, QuotaGuardConfig, RetryConfig

# ── 价格字段解析（$ / ¥ 前缀支持） ──────────────────────────

_PRICE_RE = re.compile(r"^([$\u00a5])\s*(.+)$")


def _price_to_float(v: Any) -> float:
    """Pydantic BeforeValidator: 将 $/¥ 前缀字符串 / 纯数字统一转为 float."""
    if isinstance(v, (int, float)):
        return float(v)
    if isinstance(v, str):
        m = _PRICE_RE.match(v.strip())
        if m:
            return float(m.group(2))
        return float(v)
    return float(v)


def _detect_currency(v: Any) -> str | None:
    """从原始值中检测币种前缀. 返回 ``'USD'``/``'CNY'``/``None``."""
    if isinstance(v, str):
        m = _PRICE_RE.match(v.strip())
        if m:
            return "USD" if m.group(1) == "$" else "CNY"
    return None


PriceField = Annotated[float, BeforeValidator(_price_to_float)]

logger = logging.getLogger(__name__)

# ── 后端专属字段分组映射 ──────────────────────────────────────
# 每个 backend 类型对应其专属字段集合，用于 TierConfig 的语义标注与校验

_COPILOT_FIELDS: frozenset[str] = frozenset({
    "github_token", "account_type", "token_url", "models_cache_ttl_seconds",
})
_ANTIGRAVITY_FIELDS: frozenset[str] = frozenset({
    "client_id", "client_secret", "refresh_token", "model_endpoint",
})
_ZHIPU_FIELDS: frozenset[str] = frozenset({"api_key",})

_BACKEND_EXCLUSIVE_FIELDS: dict[str, frozenset[str]] = {
    "copilot": _COPILOT_FIELDS,
    "antigravity": _ANTIGRAVITY_FIELDS,
    "zhipu": _ZHIPU_FIELDS,
}

BackendType = Literal["anthropic", "copilot", "antigravity", "zhipu"]


class ModelMappingRule(BaseModel):
    pattern: str
    target: str
    is_regex: bool = False
    backends: list[str] = Field(default_factory=list)


class ModelPricingEntry(BaseModel):
    """单个模型的定价配置（支持 $ / ¥ 前缀指定币种）.

    用法示例::

        input_cost_per_mtok: $3.0       # USD
        output_cost_per_mtok: \\u00a53.2  # CNY (¥)
        cache_read_cost_per_mtok: 0.5   # 无前缀，默认 USD

    向后兼容：不带前缀的纯数字默认视为 USD。
    """

    model_config = {"extra": "allow"}

    backend: str                            # 后端名称（对应 usage 表"后端"列）
    model: str                              # 实际模型名（对应 usage 表"实际模型"列）
    input_cost_per_mtok: PriceField = 0.0   # 输入 Token 单价
    output_cost_per_mtok: PriceField = 0.0  # 输出 Token 单价
    cache_write_cost_per_mtok: PriceField = 0.0  # 缓存创建 Token 单价
    cache_read_cost_per_mtok: PriceField = 0.0   # 缓存读取 Token 单价

    # ── 内部状态：币种信息（不参与序列化） ───────────────────
    _currency: str = PrivateAttr(default="USD")

    # ── 币种一致性校验与提取 ──────────────────────────────

    @model_validator(mode="before")
    @classmethod
    def _check_currency_consistency(cls, data: Any) -> Any:
        """校验同一 entry 内所有非零价格的币种一致性，并提取币种."""
        if not isinstance(data, dict):
            return data

        price_field_names = [
            "input_cost_per_mtok", "output_cost_per_mtok",
            "cache_write_cost_per_mtok", "cache_read_cost_per_mtok",
        ]
        currencies: set[str] = set()

        for name in price_field_names:
            raw = data.get(name)
            if raw is None or raw == 0 or raw == 0.0:
                continue
            detected = _detect_currency(raw)
            if detected:
                currencies.add(detected)

        if len(currencies) > 1:
            backend = data.get("backend", "?")
            model = data.get("model", "?")
            raise ValueError(
                f"PricingEntry(backend={backend!r}, model={model!r}): "
                f"检测到混合币种 {sorted(currencies)}，"
                "同一模型的所有单价必须使用相同币种 ($ 或 ¥)"
            )

        # 将检测到的币种暂存到临时键（mode=after 中消费后清理）
        if currencies:
            data["__detected_currency__"] = currencies.pop()

        return data

    @model_validator(mode="after")
    def _capture_currency(self) -> "ModelPricingEntry":
        """将 mode=before 检测到的币种转移到 PrivateAttr，清理临时键."""
        detected = getattr(self, "__detected_currency__", None)
        if detected:
            self._currency = detected
            # 从 __pydantic_extra__ 中移除临时键，避免序列化泄露
            if hasattr(self, "__pydantic_extra__") and "__detected_currency__" in self.__pydantic_extra__:
                del self.__pydantic_extra__["__detected_currency__"]
            else:
                # 回退：直接删除实例属性
                try:
                    object.__delattr__(self, "__detected_currency__")
                except AttributeError:
                    pass
        return self

    @model_validator(mode="after")
    def _validate_non_negative(self) -> "ModelPricingEntry":
        """校验所有价格字段非负."""
        for name in (
            "input_cost_per_mtok", "output_cost_per_mtok",
            "cache_write_cost_per_mtok", "cache_read_cost_per_mtok",
        ):
            val = getattr(self, name)
            if val < 0:
                raise ValueError(
                    f"PricingEntry(backend={self.backend!r}, model={self.model!r}): "
                    f"{name} 不能为负数（当前值: {val}）"
                )
        return self

    @property
    def currency(self) -> str:
        """本条目的币种代码 (``'USD'`` / ``'CNY'``).

        从 PrivateAttr 读取，若无显式币种前缀则默认 ``'USD'``。
        """
        return self._currency


class TierConfig(BaseModel):
    """单个 Tier 的统一配置（支持所有后端类型）.

    列表顺序即优先级：index 越小优先级越高。
    无 circuit_breaker 的 Tier 为终端层（不触发故障转移）。

    各后端类型的专属字段已通过 ``Field(description=...)`` 标注适用范围，
    非当前 backend 类型的专属字段在验证阶段会发出 warning 日志。
    """

    backend: BackendType

    # ── 通用字段（所有后端共用） ──────────────────────────────
    enabled: bool = True
    base_url: str = Field(
        default="",
        description="后端 API 基础 URL；留空时使用各后端默认值",
    )
    timeout_ms: int = Field(
        default=300000,
        description="请求超时时间（毫秒），适用于所有后端",
    )

    # ── Copilot 专属字段 ─────────────────────────────────────────────
    github_token: str = Field(
        default="",
        description="[copilot] GitHub Personal Access Token 或 OAuth Token",
    )
    account_type: str = Field(
        default="individual",
        description="[copilot] Copilot 账户类型：individual / business / enterprise",
    )
    token_url: str = Field(
        default="https://api.github.com/copilot_internal/v2/token",
        description="[copilot] Copilot Token 交换端点 URL",
    )
    models_cache_ttl_seconds: int = Field(
        default=300,
        description="[copilot] 模型列表缓存 TTL（秒）",
    )

    # ── Antigravity 专属字段 ────────────────────────────────────────
    client_id: str = Field(
        default="",
        description="[antigravity] Google OAuth2 Client ID",
    )
    client_secret: str = Field(
        default="",
        description="[antigravity] Google OAuth2 Client Secret",
    )
    refresh_token: str = Field(
        default="",
        description="[antigravity] Google OAuth2 Refresh Token",
    )
    model_endpoint: str = Field(
        default="models/claude-sonnet-4-20250514",
        description="[antigravity] Antigravity 模型端点路径",
    )

    # ── Zhipu 专属字段 ────────────────────────────────────────────
    api_key: str = Field(
        default="",
        description="[zhipu] 智谱 GLM API Key",
    )

    # ── 弹性配置 ──────────────────────────────────────────────
    circuit_breaker: CircuitBreakerConfig | None = Field(
        default=None,
        description="熔断器配置；None 表示终端层（不触发故障转移）",
    )
    retry: RetryConfig = Field(default_factory=RetryConfig)
    quota_guard: QuotaGuardConfig = Field(default_factory=QuotaGuardConfig)
    weekly_quota_guard: QuotaGuardConfig = Field(default_factory=QuotaGuardConfig)

    @model_validator(mode="after")
    def _warn_irrelevant_fields(self) -> "TierConfig":
        """对非当前 backend 类型的非空专属字段发出 warning."""
        exclusive = _BACKEND_EXCLUSIVE_FIELDS.get(self.backend)
        if not exclusive:
            return self
        for backend_type, fields in _BACKEND_EXCLUSIVE_FIELDS.items():
            if backend_type == self.backend:
                continue
            for field_name in fields:
                value = getattr(self, field_name, None)
                if value and value != getattr(TierConfig.model_fields[field_name], "default", None):
                    logger.warning(
                        "TierConfig(backend=%s): 字段 %s 属于 %s 后端，当前值将被忽略",
                        self.backend, field_name, backend_type,
                    )
        return self

__all__ = [
    "BackendType", "TierConfig", "ModelMappingRule", "ModelPricingEntry",
    "_COPILOT_FIELDS", "_ANTIGRAVITY_FIELDS", "_ZHIPU_FIELDS",
    "_BACKEND_EXCLUSIVE_FIELDS",
]
