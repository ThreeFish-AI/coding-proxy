# 监控·运维·排查

<details>
<summary><strong>📑 目录（点击展开）</strong></summary>

- [1. 日志查看](#1-日志查看)
- [2. 用量统计](#2-用量统计)
- [3. 健康检查](#3-健康检查)
- [4. Dashboard 监控](#4-dashboard-监控)
- [5. 数据库维护](#5-数据库维护)
- [6. 性能调优参考](#6-性能调优参考)
- [7. 常见使用场景](#7-常见使用场景)
- [8. 故障排查](#8-故障排查)

</details>

## 1. 日志查看

代理日志默认输出到控制台，包含以下关键事件：

| 事件            | 日志级别     | 示例                                                      |
| --------------- | ------------ | --------------------------------------------------------- |
| 熔断器状态转换  | INFO/WARNING | `Circuit breaker: CLOSED → OPEN (3 consecutive failures)` |
| 故障转移触发    | WARNING      | `Primary error 429, failing over`                         |
| 恢复成功        | INFO         | `Circuit breaker: HALF_OPEN → CLOSED (recovered)`         |
| Rate Limit 生效 | INFO         | `Tier zhipu: rate limit deadline active, 30.0s remaining` |
| 自动登录        | INFO         | `Copilot 层缺少有效凭证，启动 GitHub OAuth 登录...`       |

配置调整：

```yaml
logging:
  level: "DEBUG"    # 查看详细的模型映射和路由决策
  file: "coding-proxy.log"  # 输出到文件
  max_bytes: 5242880        # 单文件 5 MB，触发轮转
  backup_count: 5           # 保留 5 个 gzip 压缩备份
```

## 2. 用量统计

```bash
# 最近 7 天（默认）
coding-proxy usage

# 本周统计（第 1 周）
coding-proxy usage -w 1

# 本月统计（第 1 月）
coding-proxy usage -m 1

# 全部历史
coding-proxy usage -t

# 过滤供应商（逗号分隔多选）
coding-proxy usage -v anthropic,zhipu

# 过滤模型
coding-proxy usage --model glm-5v-turbo

# 指定数据库
coding-proxy usage --db /path/to/usage.db
```

> 完整 CLI 选项参见 [CLI 参考 — usage](./cli-reference.md#3-coding-proxy-usage)。

## 3. 健康检查

```bash
# 基础检查
curl http://127.0.0.1:3392/health

# 详细状态（所有层级的熔断器、配额守卫、Rate Limit、诊断信息）
curl http://127.0.0.1:3392/api/status
```

> 端点详情参见 [API 参考](./api-reference.md)。

## 4. Dashboard 监控

浏览器访问 `http://127.0.0.1:3392/dashboard` 查看 Web 可视化看板。详见 [Dashboard 文档](./dashboard.md)。

## 5. 数据库维护

用量数据库位于 `~/.coding-proxy/usage.db`（可通过配置修改）。

- 采用 SQLite WAL 模式，支持读写并发
- 当前版本不自动清理历史数据
- 如需清理，可直接删除数据库文件（重启后自动重建）

## 6. 性能调优参考

| 参数                         | 默认值 | 稳定优先   | 敏感快速 | 说明                                     |
| ---------------------------- | ------ | ---------- | -------- | ---------------------------------------- |
| `timeout_ms`                 | 300000 | `300000`   | `120000` | 长对话保持 5 分钟；短查询可缩短至 2 分钟 |
| `failure_threshold`          | 3      | `5`        | `2`      | 网络稳定环境降低以更快触发降级           |
| `recovery_timeout_seconds`   | 300    | `600`      | `120`    | 给供应商更多恢复时间 vs 更快尝试恢复     |
| `token_budget`               | 按计划 | 按计划设定 | —        | 设为订阅额度的 95%~99%                   |
| `window_hours` (quota_guard) | 5.0    | `8.0`      | `3.0`    | 长窗口更平滑，短窗口更灵敏               |
| `max_retries`                | 2      | `3`        | `1`      | 网络不稳定时增加重试                     |

> 完整弹性参数表参见 [配置字段参考 — 弹性字段](../arch/config-reference.md#5-vendorconfig-弹性字段)。

## 7. 常见使用场景

### 7.1 上游 API 限流自动降级

**现象**：Claude Code 响应变慢或提示 "rate limited"

**代理行为**：检测到上游返回 `429` → 解析 `retry-after` → 设置 Rate Limit 截止时间 → 熔断器记录失败 → 自动降级到下一供应商 → 恢复后自动切回。

**用户操作**：无需干预。通过 [`GET /api/status`](./api-reference.md#5-get-apistatus) 中的 `rate_limit` 字段查看限速状态。

### 7.2 配额耗尽后自动降级

**现象**：上游供应商返回 `403` 错误，消息含 "quota"、"usage cap"、"limit exceeded"、"capacity" 等关键词

**代理行为**：识别关键词 → 配额守卫标记 QUOTA_EXCEEDED → 后续请求自动路由到下一层级。

**用户操作**：无需干预。通过 `coding-proxy usage` 查看各供应商请求分布。

### 7.3 手动恢复使用主供应商

```bash
coding-proxy reset

# 同时提升指定供应商优先级
coding-proxy reset -v anthropic
```

### 7.4 禁用特定供应商

在 `vendors` 列表中设置 `enabled: false`，或通过 [`tiers`](./vendors.md#4-tiers--降级链路优先级) 从降级链中排除。

### 7.5 运行时 OAuth 重认证

```bash
# CLI
coding-proxy auth reauth github

# API
curl -X POST http://127.0.0.1:3392/api/reauth/github
```

重认证请求发出后（HTTP 202），在浏览器中完成授权即可，无需重启。

## 8. 故障排查

### 8.1 代理服务无法启动

**端口占用**：

```bash
lsof -i :3392
coding-proxy start --port 8080
```

**配置文件语法错误**：检查 YAML 缩进、冒号后空格。

**Python 版本**：`python --version`（需要 >= 3.12）

### 8.2 Claude Code 无法连接代理

1. 确认代理正在运行：`coding-proxy status`
2. 确认环境变量：`echo $ANTHROPIC_BASE_URL`（应为 `http://127.0.0.1:3392`）
3. 确认端口一致

### 8.3 频繁触发故障转移

1. 检查上游 API 状态
2. 查看 `GET /api/status` 中的 `rate_limit` 信息
3. 适当调高 `failure_threshold`（如 3 → 5）
4. 适当调高 `recovery_timeout_seconds`

### 8.4 供应商返回错误（通用）

适用于所有原生 Anthropic 兼容供应商（zhipu、minimax、alibaba、xiaomi、kimi、doubao）：

1. **API Key 错误**：确认对应环境变量已正确设置
2. **模型不存在**：检查 [`model_mapping`](./vendors.md#5-model_mapping--模型映射规则) 中的 `target` 是否有效
3. **网络问题**：确认可以访问对应供应商的 `base_url`

### 8.5 Copilot 认证问题

**现象**：GitHub Device Flow 显示 "Congratulations, you're all set!" 但请求仍失败。

```bash
# 查看诊断信息
curl http://127.0.0.1:3392/api/copilot/diagnostics

# 探查可用模型
curl http://127.0.0.1:3392/api/copilot/models

# 重认证
coding-proxy auth reauth github
```

### 8.6 Token 用量不记录

1. 确认数据库目录可写：`ls -la ~/.coding-proxy/`
2. 目录不存在时代理会自动创建
3. 流式请求的用量提取依赖 SSE 事件格式，非标准格式可能无法正确解析

### 8.7 count_tokens 请求失败

**返回 501**：无可用 vendor 处理此端点。确认至少有一个 Anthropic 兼容供应商已启用。

**返回 502**：上游 API 不可达。此端点直接透传，不经过故障转移链。
