# CLI 命令参考

<details>
<summary><strong>📑 目录（点击展开）</strong></summary>

- [1. coding-proxy start](#1-coding-proxy-start)
- [2. coding-proxy status](#2-coding-proxy-status)
- [3. coding-proxy usage](#3-coding-proxy-usage)
- [4. coding-proxy reset](#4-coding-proxy-reset)
- [5. coding-proxy auth login](#5-coding-proxy-auth-login)
- [6. coding-proxy auth status](#6-coding-proxy-auth-status)
- [7. coding-proxy auth reauth](#7-coding-proxy-auth-reauth)
- [8. coding-proxy auth logout](#8-coding-proxy-auth-logout)

</details>

## 1. coding-proxy start

启动代理服务。

```bash
coding-proxy start [OPTIONS]
```

| 参数 | 缩写 | 说明 |
|------|------|------|
| `--config` | `-c` | 配置文件路径 |
| `--port` | `-p` | 监听端口（覆盖配置文件） |
| `--host` | `-h` | 监听地址（覆盖配置文件） |

**示例**：

```bash
# 默认配置启动
coding-proxy start

# 自定义端口和配置
coding-proxy start -p 9000 -c ~/my-config.yaml

# 自定义监听地址和端口
coding-proxy start --host 0.0.0.0 --port 8046
```

> 若启用了 Copilot 或 Antigravity 供应商但未配置凭证，启动时会自动触发 OAuth 浏览器登录流程。

## 2. coding-proxy status

查看当前代理状态和各供应商层级信息。

```bash
coding-proxy status [OPTIONS]
```

| 参数 | 缩写 | 说明 |
|------|------|------|
| `--port` | `-p` | 代理服务端口（默认 8046） |

**输出示例**：

```
zhipu
  熔断器: closed  失败=0
  配额: within_quota  45.2% (452000000/1000000000)

anthropic
  熔断器: closed  失败=0
```

每个 tier 独立展示名称、熔断器状态和配额守卫状态。

**熔断器状态说明**：

| 状态 | 含义 |
|------|------|
| `closed` | 正常运行 |
| `open` | 熔断中，跳过该层降级到下一层 |
| `half_open` | 恢复测试中 |

## 3. coding-proxy usage

查看 Token 使用统计与费用估算。

```bash
coding-proxy usage [OPTIONS]
```

| 参数 | 缩写 | 说明 |
|------|------|------|
| `--days` | `-d` | 统计天数（默认 7） |
| `--week` | `-w` | 最近第 N 周统计（按周聚合，默认 1） |
| `--month` | `-m` | 最近第 N 月统计（按月聚合，默认 1） |
| `--total` | `-t` | 统计全部历史记录（按供应商+模型聚合） |
| `--vendor` | `-v` | 过滤供应商（支持逗号分隔多选，如 `anthropic,zhipu`） |
| `--model` | — | 过滤实际服务模型（支持逗号分隔多选，如 `glm-5v-turbo,glm-5.1`） |
| `--db` | — | 数据库文件路径 |

> **时间维度互斥**，优先级：`-t > -m > -w > -d`。

**示例**：

```bash
# 查看最近 7 天统计（默认）
coding-proxy usage

# 本周统计（第 1 周）
coding-proxy usage -w 1

# 本月统计（第 1 月）
coding-proxy usage -m 1

# 全部历史，按供应商+模型聚合
coding-proxy usage -t

# 最近 30 天，仅 Anthropic 和智谱供应商
coding-proxy usage -d 30 -v anthropic,zhipu

# 按实际服务模型过滤
coding-proxy usage --model glm-5v-turbo,claude-sonnet-4-6
```

**输出字段说明**：

| 字段 | 说明 |
|------|------|
| 日期 | 统计日期 |
| 供应商 | 处理请求的供应商名称 |
| 请求模型 | 客户端请求的原始模型名称 |
| 实际模型 | 供应商实际使用的模型名称（经映射后） |
| 请求数 | 总请求数 |
| 输入/输出/缓存创建/缓存读取 Token | 各维度 Token 消耗 |
| 总 Token | 所有维度之和 |
| Cost | 基于定价配置计算的费用；未配置定价时显示 `-` |
| 平均耗时(ms) | 平均响应时间 |

## 4. coding-proxy reset

手动重置所有层级的熔断器和配额守卫，恢复使用最高优先级供应商。支持运行时重排序 N-tier 链路。

```bash
coding-proxy reset [OPTIONS]
```

| 参数 | 缩写 | 说明 |
|------|------|------|
| `--port` | `-p` | 代理服务端口（默认 8046） |
| `--vendor` | `-v` | 提升/重排序 vendor 优先级（单个或逗号分隔多个） |

**重排序语义**：
- 单个 vendor：提升该 vendor 到最高优先级，其余保持相对顺序
  ```bash
  # 提升 anthropic 到最高优先级
  coding-proxy reset -v anthropic
  ```
- 多个 vendor：替换整个 N-tier 链路顺序
  ```bash
  # 替换整条链路
  coding-proxy reset -v anthropic,zhipu,copilot
  ```

**重置范围**：所有层级的熔断器状态（→ CLOSED）、配额守卫状态（→ WITHIN_QUOTA）、周级配额守卫状态（→ WITHIN_QUOTA）、Rate Limit 截止时间（→ 清除）。

## 5. coding-proxy auth login

执行 OAuth 浏览器登录，获取供应商访问凭证。

```bash
coding-proxy auth login [OPTIONS]
```

| 参数 | 缩写 | 说明 |
|------|------|------|
| `--provider` | `-p` | 指定 provider（`github` / `google`）；省略则依次登录两者 |

**示例**：

```bash
# 仅登录 GitHub（Copilot）
coding-proxy auth login -p github

# 仅登录 Google（Antigravity）
coding-proxy auth login -p google

# 依次登录两者
coding-proxy auth login
```

## 6. coding-proxy auth status

查看已登录的 OAuth 凭证状态。

```bash
coding-proxy auth status
```

**输出示例**：

```
github: 有效  有 refresh_token
google: 已过期  无 refresh_token
```

## 7. coding-proxy auth reauth

触发运行中代理的 OAuth 重认证。

```bash
coding-proxy auth reauth PROVIDER [OPTIONS]
```

| 参数 | 缩写 | 说明 |
|------|------|------|
| `PROVIDER` | — | provider 名称（必填）：`github` / `google` |
| `--port` | `-p` | 代理服务端口（默认 8046） |

**示例**：

```bash
# 触发 GitHub 重认证
coding-proxy auth reauth github

# 指定端口
coding-proxy auth reauth google -p 9090
```

> 此命令通过 [`POST /api/reauth/{provider}`](./api-reference.md#510-post-apireauthprovider) 向运行中的代理发送重认证请求，无需重启服务。

## 8. coding-proxy auth logout

清除已存储的 OAuth 凭证。

```bash
coding-proxy auth logout [OPTIONS]
```

| 参数 | 缩写 | 说明 |
|------|------|------|
| `--provider` | `-p` | 指定 provider；省略则登出所有 provider |
