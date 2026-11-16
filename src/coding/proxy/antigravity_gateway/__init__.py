"""Antigravity Gateway 子包 — Antigravity IDE 客户端透传通道.

将本地 Antigravity IDE（默认 Gemini agent）的模型流经
``CLOUD_CODE_URL=http://127.0.0.1:3392/api/antigravity`` 接入本代理，
字节级透传至 Google Cloud Code ``v1internal`` 后端，旁路解析信封完成
用量统计（``client_category='antigravity'``）与会话维度提取。

与 :mod:`..native_api`（API-key 客户端的 OpenAI/Gemini/Anthropic 原生
透传）为并列通道：协议（v1internal 私有信封）、身份（客户端已知为
Antigravity）、上游（Antigravity 服务端）均不同，公共机制经
:mod:`..passthrough_kernel` 共享。

核心模块：

- :mod:`.config`    — ``AntigravityGatewayConfig`` 配置模型
- :mod:`.operation` — v1internal 冒号方法路径 → 规范操作名
- :mod:`.envelope`  — v1internal 信封旁路解析（请求/响应/流式累加器）
- :mod:`.handler`   — ``AntigravityGatewayHandler`` 透传核心
- :mod:`.routes`    — ``register_antigravity_gateway_routes`` FastAPI 集成
"""

from __future__ import annotations

from .config import AntigravityGatewayConfig

__all__ = ["AntigravityGatewayConfig"]
