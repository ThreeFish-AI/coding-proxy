"""Antigravity Gateway 配置模型.

Antigravity IDE（默认 Gemini agent）经未文档化环境变量 ``CLOUD_CODE_URL``
将其模型流指向本地代理：

.. code-block:: bash

    export CLOUD_CODE_URL="http://127.0.0.1:3392/api/antigravity"

代理将 ``/api/antigravity/**`` 收到的请求字节级透传至 Google Cloud Code
内部端点（``v1internal`` 协议）。默认上游为当前版本的
``daily-cloudcode-pa.googleapis.com``；该域名曾在版本间漂移
（``cloudcode-pa`` → ``daily-cloudcode-pa``），故提供 env / YAML 覆写。
认证头（``Authorization: Bearer``）由 IDE 自带，proxy 不保管凭据。
"""

from __future__ import annotations

import os

from pydantic import BaseModel, Field, model_validator

# 上游 base_url 覆写 env（与 native_api 的 NATIVE_{PROVIDER}_BASE_URL 命名并列）
_BASE_URL_ENV = "ANTIGRAVITY_GATEWAY_BASE_URL"


class AntigravityGatewayConfig(BaseModel):
    """Antigravity 客户端透传通道配置."""

    enabled: bool = Field(
        default=True,
        description="是否启用 /api/antigravity/** 透传端点。默认启用（开箱即用）。",
    )
    base_url: str = Field(
        default="https://daily-cloudcode-pa.googleapis.com",
        description=(
            "Antigravity 后端（Cloud Code v1internal）base_url。"
            "该域名随 Antigravity 版本漂移过（cloudcode-pa → daily-cloudcode-pa），"
            "若 IDE 报 404/400 可切换回 https://cloudcode-pa.googleapis.com。"
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

    @model_validator(mode="after")
    def _apply_env_overrides(self) -> AntigravityGatewayConfig:
        """按 ``ANTIGRAVITY_GATEWAY_BASE_URL`` 覆写上游 base_url.

        优先级：env var > YAML 显式字段 > Pydantic 内置默认。
        空串或纯空白视作未设置。
        """
        override = os.environ.get(_BASE_URL_ENV, "").strip()
        if override:
            self.base_url = override
        return self


__all__ = ["AntigravityGatewayConfig"]
