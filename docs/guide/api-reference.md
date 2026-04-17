# HTTP API 端点

<details>
<summary><strong>📑 目录（点击展开）</strong></summary>

- [1. HEAD / 和 GET /](#1-head--和-get-)
- [2. POST /v1/messages](#2-post-v1messages)
  - [2.1 请求格式](#21-请求格式)
  - [2.2 非流式响应](#22-非流式响应)
  - [2.3 流式响应（SSE）](#23-流式响应sse)
  - [2.4 错误响应](#24-错误响应)
  - [2.5 请求规范化行为](#25-请求规范化行为)
- [3. POST /v1/messages/count_tokens](#3-post-v1messagescount_tokens)
- [4. GET /health](#4-get-health)
- [5. GET /api/status](#5-get-apistatus)
- [6. POST /api/reset](#6-post-apireset)
- [7. GET /api/copilot/diagnostics](#7-get-apicopilotdiagnostics)
- [8. GET /api/copilot/models](#8-get-apicopilotmodels)
- [9. GET /api/reauth/status](#9-get-apireauthstatus)
- [10. POST /api/reauth/{provider}](#10-post-apireauthprovider)
- [11. Dashboard 端点](#11-dashboard-端点)

</details>

## 1. HEAD / 和 GET /

根路径连通性探针。Claude Code 在建立连接前发送 `HEAD /` 作为健康检查，代理返回 HTTP 200 空响应。

```bash
curl -I http://127.0.0.1:8046/
# HTTP/1.1 200 OK
```

## 2. POST /v1/messages

代理 Anthropic Messages API，支持流式（SSE）与非流式请求。请求经过规范化处理后，路由至配置的 vendor tier 链；若当前 tier 不可用或返回可恢复错误，自动故障转移至下一 tier。

### 2.1 请求格式

**请求头**

| 请求头 | 必填 | 说明 |
|--------|------|------|
| `Content-Type` | ✓ | 固定为 `application/json` |
| `Authorization` | ✗ | 格式 `Bearer <token>`；对 Anthropic vendor 透传，对其他 vendor 由代理内部凭证管理 |
| `anthropic-version` | ✗ | Anthropic API 版本，建议传 `2023-06-01`；透传至上游 |
| `anthropic-beta` | ✗ | Beta 功能标识，透传至上游 |

> `hop-by-hop` 头（如 `Connection`、`Transfer-Encoding`）会在转发前自动过滤。

**请求体参数**

| 字段 | 类型 | 必填 | 约束 | 说明 |
|------|------|------|------|------|
| `model` | string | ✓ | 非空 | 目标模型标识。经 [`model_mapping`](./vendors.md#5-model_mapping--模型映射规则) 规则映射后路由至实际 vendor 模型 |
| `messages` | array | ✓ | 至少 1 条；`user`/`assistant` 交替；末尾必须为 `user` | 对话历史 |
| `max_tokens` | integer | ✗ | > 0 | 最大输出 token 数 |
| `stream` | boolean | ✗ | 默认 `false` | 是否以 SSE 流式返回 |
| `temperature` | number | ✗ | `[0, 2]` | 采样温度 |
| `top_p` | number | ✗ | `(0, 1]` | Top-p 采样 |
| `top_k` | integer | ✗ | ≥ 1 | Top-k 采样 |
| `stop_sequences` | array[string] | ✗ | | 提前停止的字符串序列 |
| `system` | string \| array | ✗ | | 系统提示词 |
| `tools` | array | ✗ | | 工具定义 |
| `tool_choice` | object | ✗ | | 工具选择策略 |
| `thinking` | object | ✗ | 需 `budget_tokens`；部分 vendor 不支持 | Extended Thinking 配置 |
| `metadata` | object | ✗ | | 用户元数据，透传至上游 |

**消息 content block 类型**

| 类型 | 适用角色 | 必填字段 | 说明 |
|------|---------|---------|------|
| `text` | `user`/`assistant` | `text` | 纯文本 |
| `image` | `user` | `source` | 图片；部分 vendor 不支持 |
| `tool_use` | `assistant` | `id`（`toolu_` 前缀）、`name`、`input` | 模型发起工具调用 |
| `tool_result` | `user` | `tool_use_id`、`content` | 工具调用结果 |
| `thinking` | `assistant` | `thinking`、`signature` | Extended Thinking；跨 vendor 时自动剥离 |

### 2.2 非流式响应

**成功响应（HTTP 200）**

```json
{
  "id": "msg_01XFDUDYJgAACzvnptvVoYEL",
  "type": "message",
  "role": "assistant",
  "content": [
    { "type": "text", "text": "你好！我是 Claude。" }
  ],
  "model": "claude-sonnet-4-6",
  "stop_reason": "end_turn",
  "stop_sequence": null,
  "usage": {
    "input_tokens": 14,
    "output_tokens": 32,
    "cache_creation_input_tokens": 0,
    "cache_read_input_tokens": 0
  }
}
```

**`stop_reason` 枚举**

| 值 | 含义 |
|----|------|
| `end_turn` | 模型自然输出完毕 |
| `tool_use` | 模型发起工具调用 |
| `stop_sequence` | 触发了停止序列 |
| `max_tokens` | 达到 `max_tokens` 上限 |

### 2.3 流式响应（SSE）

设置 `"stream": true`，响应以 `text/event-stream` 格式逐块下发。

**SSE 事件类型**

| 事件类型 | 说明 |
|---------|------|
| `message_start` | 消息开始，包含初始元数据 |
| `content_block_start` | 新的 content block 开始 |
| `content_block_delta` | 增量数据（`text_delta` 或 `input_json_delta`） |
| `content_block_stop` | 当前 content block 结束 |
| `message_delta` | 消息级增量（`stop_reason` + `usage`） |
| `message_stop` | 消息结束 |
| `ping` | 心跳 |
| `error` | 流式错误 |

> 流式模式下，一旦 SSE 流开始发送，代理不再进行 tier 级别的故障转移。若中途出现错误，以 `event: error` 事件通知客户端。

### 2.4 错误响应

**错误结构**

```json
{
  "error": {
    "type": "invalid_request_error",
    "message": "详细错误描述",
    "details": ["原因1", "原因2"]
  }
}
```

**HTTP 状态码对照**

| HTTP 状态码 | `error.type` | 触发场景 | 可重试 |
|------------|-------------|---------|--------|
| `400` | `invalid_request_error` | 请求格式不合规、无兼容 vendor | ✗ |
| `401` | `authentication_error` | 无有效认证凭证 | ✗ |
| `403` | `permission_error` | 权限不足 | ✗ |
| `429` | `rate_limit_error` | 所有 vendor 均触发速率限制 | ✓ |
| `500` | `api_error` | 代理内部异常 | ✓ |
| `502` | `api_error` | 所有 vendor 均不可达 | ✓ |
| `503` | `authentication_error` | Token 获取失败 | ✓ |
| `501` | `not_implemented` | 请求的端点无可用 vendor 处理 | ✗ |

### 2.5 请求规范化行为

代理在转发前自动进行规范化，对调用方透明。

**自动修复（静默处理）**

| 问题 | 处理方式 |
|------|---------|
| `tool_use_id` 格式不符（非 `toolu_` 前缀） | 自动重写为合规格式 |
| `tool_result` 出现在 `assistant` 消息中 | 剥离该 block（首次触发 WARNING 日志） |
| `tool_use` 缺少合法 ID | 自动生成新 ID |

**致命验证错误（返回 HTTP 400）**

| 场景 | 错误示例 |
|------|---------|
| `tool_use` block 缺少 `id` 字段 | `"tool_use block is missing 'id' field"` |
| `tool_result` block 缺少 `tool_use_id` 字段 | `"tool_result block is missing 'tool_use_id' field"` |
| 消息角色不交替 | `"messages must alternate between user and assistant"` |
| `messages` 末尾不是 `user` 消息 | `"last message must be from user"` |

**Thinking Block 跨 Vendor 处理**：请求路由至非 Anthropic vendor 时，assistant 历史消息中的 `thinking` block 会被自动剥离（包含仅 Anthropic 可验证的 `signature`）。不影响当前轮次的 `thinking` 功能配置。

## 3. POST /v1/messages/count_tokens

Token 计数 API 透传。支持所有提供 Anthropic 兼容端点的供应商。

```bash
curl -X POST http://127.0.0.1:8046/v1/messages/count_tokens \
  -H "Content-Type: application/json" \
  -H "anthropic-version: 2023-06-01" \
  -d '{"model":"claude-sonnet-4-6","messages":[{"role":"user","content":"Hello"}]}'
```

| 响应码 | 说明 |
|--------|------|
| `200` | 成功，返回 token 计数结果 |
| `501` | 无可用 vendor 处理此端点 |
| `502` | 上游 API 不可达 |

## 4. GET /health

健康检查。

```bash
curl http://127.0.0.1:8046/health
# {"status":"ok"}
```

## 5. GET /api/status

查询所有层级的熔断器、配额守卫、周级配额守卫、Rate Limit 及诊断信息。

```bash
curl http://127.0.0.1:8046/api/status
```

**返回示例**：

```json
{
  "tiers": [
    {
      "name": "zhipu",
      "circuit_breaker": {
        "state": "closed",
        "failure_count": 0,
        "current_recovery_seconds": 30
      },
      "quota_guard": {
        "state": "within_quota",
        "window_usage_tokens": 452000000,
        "budget_tokens": 1000000000,
        "usage_percent": 45.2
      },
      "rate_limit": {
        "is_rate_limited": false,
        "remaining_seconds": 0
      }
    },
    {
      "name": "anthropic",
      "circuit_breaker": {
        "state": "closed",
        "failure_count": 0
      }
    }
  ]
}
```

## 6. POST /api/reset

重置所有层级的弹性设施。支持可选 JSON body 进行运行时 tier 重排序。

```bash
# 仅重置（向后兼容）
curl -X POST http://127.0.0.1:8046/api/reset

# 重置 + 提升 anthropic 到最高优先级
curl -X POST http://127.0.0.1:8046/api/reset \
  -H "Content-Type: application/json" \
  -d '{"vendors": ["anthropic"]}'

# 重置 + 替换整条链路顺序
curl -X POST http://127.0.0.1:8046/api/reset \
  -H "Content-Type: application/json" \
  -d '{"vendors": ["zhipu", "anthropic", "copilot"]}'
```

**重排序语义**：
- 单个 vendor：提升到最高优先级，其余保持相对顺序
- 多个 vendor：替换整个 N-tier 链路顺序

**重置范围**：circuit_breaker（→ CLOSED）、quota_guard（→ WITHIN_QUOTA）、weekly_quota_guard（→ WITHIN_QUOTA）、rate_limit deadline（→ 清除）。

## 7. GET /api/copilot/diagnostics

返回 Copilot 认证与交换链路的脱敏诊断信息。

```bash
curl http://127.0.0.1:8046/api/copilot/diagnostics
```

若 Copilot 供应商未启用，返回 404。

## 8. GET /api/copilot/models

按需探测当前 Copilot 会话可见的模型列表。

```bash
curl http://127.0.0.1:8046/api/copilot/models
```

需要有效的 Copilot 凭证；凭证无效时返回 503。

## 9. GET /api/reauth/status

查询运行时重认证状态。

```bash
curl http://127.0.0.1:8046/api/reauth/status
```

## 10. POST /api/reauth/{provider}

手动触发指定 provider 的运行时重认证。

```bash
curl -X POST http://127.0.0.1:8046/api/reauth/github
# HTTP/1.1 202 Accepted
# {"status":"reauth requested"}
```

| 参数 | 说明 |
|------|------|
| `{provider}` | provider 名称：`github` / `google` |

返回 202 表示重认证请求已接收，用户需在浏览器中完成授权。

## 11. Dashboard 端点

为 [Dashboard 看板](./dashboard.md)提供数据的 API 端点。

### 11.1 GET /dashboard

返回 Dashboard HTML 页面。

```bash
curl http://127.0.0.1:8046/dashboard
```

### 11.2 GET /api/dashboard/summary

返回 Dashboard 汇总数据（今日 + 所选区间）。

```bash
curl "http://127.0.0.1:8046/api/dashboard/summary?days=7"
```

| 参数 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `days` | int | `7` | 统计天数（1~90） |

**返回结构**：`version`（版本号）、`today`（今日 KPI）、`range`（区间 KPI）、`failover_stats`（故障转移统计）。

### 11.3 GET /api/dashboard/timeline

返回按天分组的时序数据（用于图表绘制）。

```bash
curl "http://127.0.0.1:8046/api/dashboard/timeline?days=30"
```

| 参数 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `days` | int | `7` | 统计天数（1~90） |

**返回结构**：`period`（`"day"`）、`count`（天数）、`rows`（按天分组的用量数组）。
