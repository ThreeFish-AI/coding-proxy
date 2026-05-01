"""Session Policy 配置模型 — 为特定 Session 定制路由行为."""

from __future__ import annotations

from pydantic import BaseModel, Field


class SessionPolicyMatch(BaseModel):
    """策略匹配条件 — 满足任一条件即匹配（OR 语义）."""

    session_keys: list[str] = Field(
        default_factory=list,
        description="精确匹配的 session key 列表",
    )
    client_category: str | None = Field(
        default=None,
        description=(
            "按客户端类别匹配（'cc' 或 'api'）。"
            "⚠️ 预留字段，当前路由执行链路未传入 client_category，"
            "配置此条件不会生效。后续版本将支持。"
        ),
    )


class SessionQuotaConfig(BaseModel):
    """Per-session 资源配额（架构预留）."""

    token_budget: int = Field(
        default=0,
        description="时间窗口内的 token 预算上限",
    )
    window_hours: float = Field(
        default=24.0,
        description="滚动时间窗口（小时）",
    )


class SessionPolicy(BaseModel):
    """单条 Session 路由策略."""

    name: str = Field(description="策略名称（用于日志与排障）")
    match: SessionPolicyMatch = Field(description="匹配条件")
    tiers: list[str] = Field(
        default_factory=list,
        description="覆盖全局 tier 顺序的供应商优先级列表",
    )
    quota: SessionQuotaConfig | None = Field(
        default=None,
        description="Per-session 资源配额（预留）",
    )


class SessionPoliciesConfig(BaseModel):
    """顶层 Session 策略配置容器."""

    policies: list[SessionPolicy] = Field(
        default_factory=list,
        description="Session 路由策略列表，按定义顺序求值，首次匹配生效",
    )
