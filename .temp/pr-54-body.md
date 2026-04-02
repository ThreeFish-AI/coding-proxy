## 变更概述

本次改动围绕 `zhipu` 后端在 `https://open.bigmodel.cn/api/anthropic` 兼容端点下的 Claude Code 协议适配展开，目标是在不切换 endpoint、不影响现有故障转移与路由架构的前提下，降低 `Task`、`Bash`、`Grep`、`Glob`、`Edit`、`Write`、`Read`、`TodoWrite` 等本地工具链路因参数形状不兼容导致的异常、重试和降级。

## 做了哪些改动

### 1. 增强 zhipu 请求补偿逻辑
- 将 `ZhipuBackend` 从简单透传调整为“请求补偿 + 诊断输出”的兼容适配器。
- 默认模型映射改为：
  - `claude-opus-*` -> `glm-5v-turbo`
  - `claude-sonnet-*` -> `glm-5v-turbo`
  - `claude-haiku-*` -> `glm-4.5-air`
- 对 `thinking` / `extended_thinking` 做收敛处理，保留启用语义并剥离不安全的预算类字段。
- 对 `tool_choice` 增加投影逻辑，支持：
  - 指定工具时收窄工具集
  - `none` 时移除工具声明
  - `any` 时保留工具调用意图并记录诊断
- 保留 `anthropic-beta`、`x-request-id` 等关键请求头，避免 Claude Code Beta 工具能力在代理层被意外丢失。
- 将 `metadata.user_id` 投影到兼容端点可接受字段，并记录 request adaptations。

### 2. 增加会话态与诊断信息
- 在 compat session 中记录最近一轮工具调用映射与工具选择模式，便于后续轮次工具结果关联和问题定位。
- 在 diagnostics 中补充：
  - `request_adaptations`
  - `tool_choice_projection`
  - `requested_model`
  - `resolved_model`
  - `tool_names`

### 3. 补充 Claude Code 工具回归测试
- 新增/增强 zhipu 后端测试，覆盖：
  - 默认模型家族映射
  - `thinking` 字段投影
  - `anthropic-beta` 请求头透传
  - `tool_choice` 的 `tool` / `none` / `any` 行为
  - compat session 状态记录
- 针对 Claude Code 常见本地工具补充参数形状保真测试：
  - `Task`
  - `Bash`
  - `Grep`
  - `Glob`
  - `Edit`
  - `Write`
  - `Read`
  - `TodoWrite`

## 为什么要这样改

任务背景是：在 Claude Code 使用 `glm-5v-turbo` 和 `glm-4.5-air` 代替 Claude 原生模型时，zhipu 的 Anthropic 兼容端点在部分工具调用链路上出现参数不适配，导致本地工具异常、重复重试或降级执行，直接影响 Claude Code 的“满血能力”。

这次改动的目标不是更换底层接入方式，而是在保留现有兼容端点权限边界的前提下，通过代理层补偿把请求形状修正到兼容端点更稳定可承接的状态，同时把关键信息写入诊断和会话态，降低问题定位成本，并用回归测试锁住 Claude Code 工具协议的关键行为。

## 关键实现细节

- 未切换 zhipu endpoint，仍然使用 `https://open.bigmodel.cn/api/anthropic`
- 未改动整体路由、熔断、配额与其他后端实现
- zhipu 能力画像从“原生支持”调整为更准确的“通过兼容补偿承接”
- 通过默认模型映射修正，明确让 `glm-5v-turbo` 承接 Opus/Sonnet 家族，让 `glm-4.5-air` 承接 Haiku 家族
- 通过工具约束与测试增强，减少 Claude Code 本地工具调用在兼容端点下的协议漂移风险

## 验证结果

已执行：
```bash
uv run pytest -q
```

结果：
```text
339 passed in 4.63s
```
