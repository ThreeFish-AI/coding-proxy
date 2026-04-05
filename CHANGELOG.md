# Changelog

本文件基于 [Keep a Changelog](https://keepachangelog.com/zh-CN/) 规范维护，版本号遵循 [语义化版本](https://semver.org/lang/zh-CN/)。

## [Unreleased]

## [0.1.0] — 2026-04-05

### ✨ 亮点特性

> **coding-proxy MVP 版本发布！** 一行环境变量，让 Claude Code 拥有永不宕机的多后端智能代理——主服务故障时毫秒级自动切换至备用通道，编码心流零中断。

| 特性 | 价值 |
|:-----|:-----|
| **N 层链式故障转移** | Claude → Copilot → Antigravity → GLM 全链路自动降级 |
| **智能熔断器** | 状态机防护雪崩，指数退避自动恢复 |
| **双窗口配额守卫** | 5h 滑动窗口 + 周配额双重护盾，超额前主动预警 |
| **零侵入透明代理** | 改一行 `ANTHROPIC_BASE_URL` 即接入，代码零改动 |
| **协议万能转换** | Anthropic ↔ OpenAI/Gemini 双向无缝翻译 |
| **模型名自动映射** | 正则匹配 + 自定义规则，`claude-*` → `glm-*` 一键切 |
| **Token 用量看板** | SQLite 本地存储，CLI 多维统计（按天/供应商/模型） |
| **轻量独立部署** | FastAPI 异步架构，零 Redis/MQ 重依赖，开箱即用 |
| **OAuth2 内置集成** | GitHub Device Flow / Google OAuth 开箱即用，令牌自动轮转 |
| **SSE 流式全链路** | 流式请求完整透传，跨协议 SSE 转换零感知 |

### 🔧 更多特性

- 指数退避重试机制，可配置最大重试次数与退避策略
- 速率限制头解析与智能等待，精确计算恢复时间
- Copilot 421 Misdirection 自动重试
- 可配置供应商优先级（tiers），灵活编排降级链路
- 模型定价表支持，按供应商/模型细粒度成本追踪
- 兼容性会话状态管理（CompatSessionStore）
- 错误事件流式传播，流式场景异常不丢失
- 优雅停机与资源清理，进程退出无残留
- YAML 灵活配置 + 环境变量安全注入敏感值
- Vendor 能力声明与自动兼容性降级决策

[0.1.0]: https://github.com/ThreeFish-AI/coding-proxy/releases/tag/v0.1.0
