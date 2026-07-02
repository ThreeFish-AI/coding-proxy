"""Session Policy 配置模型 — 为特定 Session 定制路由行为."""

from __future__ import annotations

from pydantic import BaseModel, Field, field_validator


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


class TitleVendorBinding(BaseModel):
    """标题前缀 → 供应商自动绑定规则."""

    prefix: str = Field(
        min_length=1,
        description=(
            "标题前缀匹配模式（大小写敏感的 startswith 匹配）。"
            "禁止空字符串——空前缀会匹配所有标题,导致全量误绑定。"
        ),
    )
    vendor: str = Field(
        min_length=1,
        description="匹配后绑定的目标供应商名称",
    )


class SessionPoliciesConfig(BaseModel):
    """顶层 Session 策略配置容器."""

    policies: list[SessionPolicy] = Field(
        default_factory=list,
        description="Session 路由策略列表，按定义顺序求值，首次匹配生效",
    )
    title_vendor_bindings: list[TitleVendorBinding] = Field(
        default_factory=list,
        description=(
            "标题前缀 → 供应商自动绑定规则。"
            "当 Session 标题以指定前缀开头时，自动绑定到对应供应商。"
            "匹配规则按列表顺序求值，首次匹配生效。"
        ),
    )
    title_exempt_prefixes: list[str] = Field(
        default_factory=list,
        description=(
            "Session 标题豁免前缀名单。标题提取的每一层（user TEXT 噪声剥离、"
            "TOOL_RESULT 摘要、IMAGE 计数、元数据兜底）产出的候选，若以任一前缀"
            "开头，则视为豁免、继续向下一层回退；全部层级均被豁免时返回空串，"
            "调用方据此跳过写库，session 标题保持空待后续真实输入回填。"
            "既用于过滤注入式 Prompt（如示例语言指令），也可豁免无信息量的合成"
            "兜底标题（如 [Session]、[Tool call]、[Tool output]）。"
            "大小写敏感的 startswith 匹配，与 title_vendor_bindings 语义一致。"
            "加载时会自动 strip + 去空 + 去重；空字符串前缀会被丢弃"
            "（防空串 startswith 恒真导致全量误豁免）。"
        ),
    )

    @field_validator("title_exempt_prefixes", mode="after")
    @classmethod
    def _normalize_exempt_prefixes(cls, value: list[str]) -> list[str]:
        """归一化豁免前缀：strip + 去空 + 去重保序.

        空字符串前缀必须丢弃——``"".startswith`` 恒真会豁免一切用户输入，
        导致 Level 1 标题提取永久失效。
        """
        seen: set[str] = set()
        result: list[str] = []
        for item in value:
            if not isinstance(item, str):
                continue
            cleaned = item.strip()
            if not cleaned or cleaned in seen:
                continue
            seen.add(cleaned)
            result.append(cleaned)
        return result
