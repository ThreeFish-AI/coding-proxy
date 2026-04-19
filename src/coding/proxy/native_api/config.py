"""Native API 透传配置模型.

三家 provider 的 base_url / enabled / timeout 配置；默认 ``enabled=True``
（开箱即用：客户端只需将 SDK base_url 改为 ``http://127.0.0.1:3392/api/{provider}``
即可直接触达官方上游；proxy 不保管凭据，认证头由客户端自带）。

.. note::
    - ``base_url`` 为**纯域名前缀**（不含 ``/v1``），客户端 SDK 的
      ``base_url=http://proxy/api/openai/v1`` 将 ``rest`` 路径与上游 base 直拼；
    - ``authorization`` / ``x-api-key`` 等认证头由客户端自带，proxy 不保管凭据；
    - 上游 base_url 覆写优先级：**env var > YAML 显式字段 > Pydantic 内置默认**。
      env var 命名由 :data:`_ENV_BASE_URL_MAP` 规定（``NATIVE_{PROVIDER}_BASE_URL``）。
"""

from __future__ import annotations

import os

from pydantic import BaseModel, Field, model_validator

# env var 命名约定：NATIVE_{PROVIDER}_BASE_URL
# 与已有 ANTHROPIC_BASE_URL（client→proxy 方向）通过 NATIVE_ 前缀彻底切分语义。
_ENV_BASE_URL_MAP: dict[str, str] = {
    "openai": "NATIVE_OPENAI_BASE_URL",
    "gemini": "NATIVE_GEMINI_BASE_URL",
    "anthropic": "NATIVE_ANTHROPIC_BASE_URL",
}


class NativeProviderConfig(BaseModel):
    """单个原生 API provider 的透传配置."""

    enabled: bool = Field(
        default=True,
        description="是否启用该 provider 的原生透传端点。默认启用（开箱即用）。",
    )
    base_url: str = Field(
        default="",
        description=(
            "上游 API base_url（纯域名前缀，不含 /v1 等版本段）。留空时使用内置默认值。"
        ),
    )
    timeout_ms: int = Field(
        default=300_000,
        ge=1_000,
        description="单次请求超时（毫秒）。LLM 大模型建议 ≥ 120s。",
    )
    connect_timeout_ms: int = Field(
        default=15_000,
        ge=500,
        description="连接建立超时（毫秒）。",
    )

    model_config = {"extra": "allow"}


class NativeApiConfig(BaseModel):
    """Native API 透传顶层配置 — 三家 provider 各一份子配置."""

    openai: NativeProviderConfig = Field(
        default_factory=lambda: NativeProviderConfig(
            base_url="https://api.openai.com",
        ),
        description="OpenAI 原生 API 透传配置（含 chat / responses / embeddings / audio / image / moderations 等）。",
    )
    gemini: NativeProviderConfig = Field(
        default_factory=lambda: NativeProviderConfig(
            base_url="https://generativelanguage.googleapis.com",
        ),
        description="Google Gemini 原生 API 透传配置（含 generateContent / streamGenerateContent / embedContent / cachedContents 等）。",
    )
    anthropic: NativeProviderConfig = Field(
        default_factory=lambda: NativeProviderConfig(
            base_url="https://api.anthropic.com",
        ),
        description="Anthropic 原生 API 透传配置（含 messages / count_tokens / batches 等）。",
    )

    model_config = {"extra": "allow"}

    @model_validator(mode="after")
    def _apply_env_overrides(self) -> NativeApiConfig:
        """按 ``NATIVE_{PROVIDER}_BASE_URL`` 环境变量覆写上游 base_url.

        优先级：env var（运行时） > YAML 显式字段（部署时） > Pydantic 内置默认（兜底）。
        空串或纯空白视作未设置，保留上一层值，避免"未设置 env → 空串覆盖内置默认"陷阱。
        """
        for provider, env_name in _ENV_BASE_URL_MAP.items():
            override = os.environ.get(env_name, "").strip()
            if override:
                getattr(self, provider).base_url = override
        return self

    def get(self, provider: str) -> NativeProviderConfig | None:
        """按 provider 名称获取子配置（大小写不敏感）."""
        key = provider.lower()
        if key == "openai":
            return self.openai
        if key == "gemini":
            return self.gemini
        if key == "anthropic":
            return self.anthropic
        return None

    def is_enabled(self, provider: str) -> bool:
        cfg = self.get(provider)
        return bool(cfg and cfg.enabled)


__all__ = ["NativeApiConfig", "NativeProviderConfig"]
