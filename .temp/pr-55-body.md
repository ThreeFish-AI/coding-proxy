## 变更摘要

本 PR 聚焦于 `coding-proxy` 中 Zhipu Claude 兼容链路的确定性兼容问题收敛，目标是在继续使用：

- `glm-5v-turbo` 对应 Claude 4.6 Opus / Sonnet
- `glm-4.5-air` 对应 Claude 4.5 Haiku

的前提下，减少因鉴权、传输层异常和超大工具集带来的失败重试、语义漂移和非预期降级。

## 做了什么

### 1. 收敛 Zhipu 认证失败语义

- 在 Zhipu backend 中补齐 `401` 错误归一化逻辑
- 将“令牌已过期或验证不正确”等兼容端点认证失败统一映射为 `authentication_error`
- 在缺少 `api_key` 时增加 fail-fast 行为，避免请求继续落入不明确异常路径

### 2. 增强工具投递兼容性

- 为 `glm-5v-turbo` / `glm-4.5-air` 增加自适应工具投递策略
- 在 `glm-4.5-air` 下优先保留 Claude Code 核心工具，限制过大的工具集合
- 为 MCP / 浏览器类工具增加可配置裁剪能力，降低兼容端点对超大 tool schema 的失败概率

### 3. 统一收口传输层读失败

- 将 `httpx.ReadError` 纳入 router / server 的统一异常面
- 流式请求遇到读失败时返回 Anthropic 兼容 SSE error
- 非流式请求遇到读失败时返回稳定的 `502 api_error`
- 避免异常继续冒泡为 FastAPI / ASGI 层 `500 Internal Server Error`

### 4. 补充回归测试

新增测试覆盖以下场景：

- Zhipu `401` 认证失败归一化
- `ReadError` 在路由链中的 failover 行为
- 流式请求返回 SSE error
- 非流式请求返回 `502`
- `glm-4.5-air` 下的大工具集压缩与核心工具保留策略

## 为什么这样改

当前任务背景是：在通过 Zhipu Claude 兼容端点承接 Claude Code 时，`glm-5v-turbo` / `glm-4.5-air` 链路存在明显的不稳定因素，包括：

- 认证失败被反复触发，导致无效重试
- 上游读失败直接打成框架级 500
- 超大工具集增加兼容端点请求风险
- 错误语义不稳定，影响 Claude Code 的恢复与降级策略

这些问题会直接造成任务执行失败、时间浪费，以及能力体验打折扣。  
因此本 PR 先优先修复已经由日志明确证实的确定性问题，为后续继续逼近“满血能力”目标建立更稳的兼容基础。

## 实现细节

- 在 `src/coding/proxy/backends/zhipu.py` 中新增：
  - `401` 认证错误归一化
  - 缺失 `api_key` 的 fail-fast
  - 按模型的工具投递分层与裁剪
- 在 `src/coding/proxy/routing/router.py` 与 `src/coding/proxy/server/app.py` 中：
  - 将 `httpx.ReadError` 纳入统一异常处理
  - 保证流式 / 非流式路径的错误语义一致
- 在测试中补齐：
  - backend 级行为验证
  - router failover 验证
  - app 路由层 `502` / SSE error 验证

## 影响范围

本次改动不改变外部 API 的调用方式，也不改变既有模型映射目标；  
主要是增强 Zhipu Claude 兼容链路的稳定性、可解释性与失败收敛能力，尽量避免引入新的行为回归。
