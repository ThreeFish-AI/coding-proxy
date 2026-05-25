"""供应商专属配置模型."""

from __future__ import annotations

from pydantic import BaseModel, Field


class ZhipuConcurrencyConfig(BaseModel):
    """Zhipu 每模型并发限制配置."""

    default: int = Field(default=3, ge=1, le=20, description="全局默认并行度")
    models: dict[str, int] = Field(
        default_factory=dict,
        description="按映射后模型名自定义并行度（覆盖 default）",
    )

    def get_limit(self, model: str) -> int:
        """获取指定模型的并行度限制."""
        return self.models.get(model, self.default)


class AnthropicConfig(BaseModel):
    enabled: bool = True
    base_url: str = "https://api.anthropic.com"
    timeout_ms: int = 300000


class CopilotConfig(BaseModel):
    """GitHub Copilot 供应商配置."""

    enabled: bool = False
    github_token: str = ""
    account_type: str = "individual"
    token_url: str = "https://api.github.com/copilot_internal/v2/token"
    base_url: str = ""
    models_cache_ttl_seconds: int = 300
    timeout_ms: int = 300000


class AntigravityConfig(BaseModel):
    """Google Antigravity Claude 供应商配置."""

    enabled: bool = False
    client_id: str = ""
    client_secret: str = ""
    refresh_token: str = ""
    base_url: str = "https://generativelanguage.googleapis.com/v1beta"
    model_endpoint: str = "models/claude-sonnet-4-20250514"
    timeout_ms: int = 300000
    safety_settings: dict[str, str] | None = None
    project_id: str = ""  # GCP Project ID（v1internal 协议必填）


class ZhipuConfig(BaseModel):
    """智谱 GLM 供应商配置（原生 Anthropic 兼容端点）.

    官方端点已完整支持 Anthropic Messages API 协议，
    无需工具截断、thinking 剥离等适配逻辑.
    """

    enabled: bool = True
    base_url: str = "https://open.bigmodel.cn/api/anthropic"
    api_key: str = ""
    timeout_ms: int = 3000000
    concurrency: ZhipuConcurrencyConfig = Field(default_factory=ZhipuConcurrencyConfig)


class MinimaxConfig(BaseModel):
    """MiniMax 供应商配置（原生 Anthropic 兼容端点）."""

    enabled: bool = True
    base_url: str = "https://api.minimaxi.com/anthropic"
    api_key: str = ""
    timeout_ms: int = 3000000


class KimiConfig(BaseModel):
    """Kimi 供应商配置（原生 Anthropic 兼容端点）."""

    enabled: bool = True
    base_url: str = "https://api.kimi.com/coding/"
    api_key: str = ""
    timeout_ms: int = 3000000


class DoubaoConfig(BaseModel):
    """豆包 Doubao 供应商配置（原生 Anthropic 兼容端点）."""

    enabled: bool = True
    base_url: str = "https://ark.cn-beijing.volces.com/api/coding"
    api_key: str = ""
    timeout_ms: int = 3000000


class XiaomiConfig(BaseModel):
    """小米 MiMo 供应商配置（原生 Anthropic 兼容端点）."""

    enabled: bool = True
    base_url: str = "https://token-plan-cn.xiaomimimo.com/anthropic"
    api_key: str = ""
    timeout_ms: int = 3000000


class AlibabaConfig(BaseModel):
    """阿里 Qwen 供应商配置（原生 Anthropic 兼容端点）."""

    enabled: bool = True
    base_url: str = "https://coding-intl.dashscope.aliyuncs.com/apps/anthropic"
    api_key: str = ""
    timeout_ms: int = 3000000


__all__ = [
    "AnthropicConfig",
    "CopilotConfig",
    "AntigravityConfig",
    "ZhipuConfig",
    "ZhipuConcurrencyConfig",
    "MinimaxConfig",
    "KimiConfig",
    "DoubaoConfig",
    "XiaomiConfig",
    "AlibabaConfig",
]
