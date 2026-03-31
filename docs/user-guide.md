# coding-proxy 用户操作指引

## 1. 简介

### 1.1 什么是 coding-proxy

coding-proxy 是一个面向 Claude Code 的多后端智能代理服务。它在 Claude Code 和 API 后端之间充当透明代理，具备以下核心能力：

- **自动故障转移**：Anthropic API 不可用时自动切换到智谱 GLM 后端，恢复后自动切回
- **模型名称映射**：自动将 Claude 模型名转换为对应的 GLM 模型名
- **Token 用量追踪**：记录每次请求的 Token 消耗、后端选择、响应时间等指标
- **熔断器保护**：智能检测后端健康状态，避免反复请求失败的后端

### 1.2 工作原理

```
Claude Code ──→ coding-proxy ──→ Anthropic API（主后端）
                     │
                     └──→ 智谱 GLM API（故障时自动切换）
```

正常情况下，coding-proxy 将请求透传到 Anthropic API。当检测到限流、配额耗尽或服务过载等错误时，自动切换到智谱 GLM 备用后端。后端恢复后，代理会自动尝试切回主后端。整个过程对用户透明，无需手动干预。

---

## 2. 快速开始

### 2.1 环境要求

- **Python** >= 3.13
- **UV** 包管理器（推荐）或 pip
- **智谱 API Key**：从 [open.bigmodel.cn](https://open.bigmodel.cn) 获取
- **Claude Code** 已安装并可用

### 2.2 安装

```bash
# 方式一：使用 UV（推荐）
uv sync

# 方式二：使用 pip
pip install -e .
```

安装完成后，`coding-proxy` 命令即可使用。

### 2.3 最小配置

```bash
# 复制配置模板到项目根目录
cp config.example.yaml config.yaml
```

设置智谱 API Key（二选一）：

**方式一：环境变量（推荐）**

```bash
export ZHIPU_API_KEY="your-api-key-here"
```

配置文件中使用 `${ZHIPU_API_KEY}` 引用，代理启动时自动替换。

**方式二：直接写入配置文件**

编辑 `config.yaml`，将 `fallback.api_key` 设为实际的 API Key：

```yaml
fallback:
  api_key: "your-api-key-here"
```

> **安全提示**：`config.yaml` 已在 `.gitignore` 中，不会被提交到版本库。推荐使用环境变量方式避免密钥泄露。

### 2.4 启动服务

```bash
# 使用默认配置启动
coding-proxy start

# 指定端口
coding-proxy start --port 8080

# 指定配置文件
coding-proxy start --config /path/to/config.yaml

# 自定义监听地址和端口
coding-proxy start --host 0.0.0.0 --port 8046
```

启动成功后会看到类似输出：

```bash
INFO:     Started server process [75773]
INFO:     Waiting for application startup.
INFO:     Application startup complete.
INFO:     Uvicorn running on http://127.0.0.1:8046 (Press CTRL+C to quit)
```

### 2.5 验证服务

```bash
# 健康检查
curl http://127.0.0.1:8046/health
# 期望返回: {"status":"ok"}

# 查看代理状态
coding-proxy status
```

### 2.6 配置 Claude Code

将 Claude Code 的 API 端点指向 coding-proxy：

```bash
export ANTHROPIC_BASE_URL=http://127.0.0.1:8046
```

设置后，Claude Code 发出的所有 API 请求将经过 coding-proxy 代理。

---

## 3. 配置详解

### 3.1 配置文件位置

配置文件按以下优先级加载（找到第一个即停止）：

1. `--config` 参数指定的路径（最高优先级）
2. `./config.yaml`（项目根目录）
3. `~/.coding-proxy/config.yaml`（用户主目录）
4. 内置默认值（无需配置文件也可启动）

### 3.2 完整配置示例

```yaml
# 服务器配置
server:
  host: "127.0.0.1"    # 监听地址
  port: 8046            # 监听端口

# 主后端 — Anthropic 官方 API
primary:
  enabled: true
  base_url: "https://api.anthropic.com"
  timeout_ms: 300000    # 5 分钟

# 备选后端 — 智谱 GLM（Anthropic 兼容接口）
fallback:
  enabled: true
  base_url: "https://open.bigmodel.cn/api/anthropic"
  api_key: "${ZHIPU_API_KEY}"   # 通过环境变量注入
  timeout_ms: 3000000   # 50 分钟

# 熔断器配置
circuit_breaker:
  failure_threshold: 3           # 连续 3 次失败后触发熔断
  recovery_timeout_seconds: 300  # 熔断后等待 5 分钟尝试恢复
  success_threshold: 2           # 连续 2 次成功后关闭熔断
  max_recovery_seconds: 3600     # 指数退避上限 1 小时

# 故障转移触发条件
failover:
  status_codes: [429, 403, 503, 500]
  error_types:
    - "rate_limit_error"
    - "overloaded_error"
    - "api_error"
  error_message_patterns:
    - "quota"
    - "limit exceeded"
    - "usage cap"
    - "capacity"

# 模型名称映射（Claude → GLM）
model_mapping:
  - pattern: "claude-sonnet-.*"
    target: "glm-5.1"
    is_regex: true
  - pattern: "claude-opus-.*"
    target: "glm-5.1"
    is_regex: true
  - pattern: "claude-haiku-.*"
    target: "glm-4.5-air"
    is_regex: true

# 用量数据库
database:
  path: "~/.coding-proxy/usage.db"

# 日志配置
logging:
  level: "INFO"
```

### 3.3 各配置节说明

#### server — 服务器

| 字段   | 类型   | 默认值        | 说明                                      |
| ------ | ------ | ------------- | ----------------------------------------- |
| `host` | string | `"127.0.0.1"` | 监听地址。设为 `"0.0.0.0"` 可接受外部连接 |
| `port` | int    | `8046`        | 监听端口                                  |

#### primary — 主后端（Anthropic）

| 字段         | 类型   | 默认值                        | 说明                            |
| ------------ | ------ | ----------------------------- | ------------------------------- |
| `enabled`    | bool   | `true`                        | 是否启用主后端                  |
| `base_url`   | string | `"https://api.anthropic.com"` | Anthropic API 地址              |
| `timeout_ms` | int    | `300000`                      | 请求超时，默认 5 分钟（300 秒） |

#### fallback — 备选后端（智谱）

| 字段         | 类型   | 默认值                                     | 说明                                 |
| ------------ | ------ | ------------------------------------------ | ------------------------------------ |
| `enabled`    | bool   | `true`                                     | 是否启用备选后端                     |
| `base_url`   | string | `"https://open.bigmodel.cn/api/anthropic"` | 智谱 Anthropic 兼容接口地址          |
| `api_key`    | string | `""`                                       | 智谱 API Key，支持 `${ENV_VAR}` 引用 |
| `timeout_ms` | int    | `3000000`                                  | 请求超时，默认 50 分钟               |

#### circuit_breaker — 熔断器

| 字段                       | 类型 | 默认值 | 说明                                       |
| -------------------------- | ---- | ------ | ------------------------------------------ |
| `failure_threshold`        | int  | `3`    | 连续失败多少次后触发熔断（切换到备选后端） |
| `recovery_timeout_seconds` | int  | `300`  | 熔断后等待多久尝试恢复（秒）               |
| `success_threshold`        | int  | `2`    | 恢复测试阶段需要连续成功多少次才关闭熔断   |
| `max_recovery_seconds`     | int  | `3600` | 指数退避的最大等待时间（秒）               |

**指数退避机制**：如果恢复测试失败，等待时间会翻倍（300s → 600s → 1200s → ...），直到达到 `max_recovery_seconds` 上限。

#### failover — 故障转移条件

| 字段                     | 类型      | 默认值                                                  | 说明                                         |
| ------------------------ | --------- | ------------------------------------------------------- | -------------------------------------------- |
| `status_codes`           | list[int] | `[429, 403, 503, 500]`                                  | 触发故障转移的 HTTP 状态码                   |
| `error_types`            | list[str] | `["rate_limit_error", "overloaded_error", "api_error"]` | 触发故障转移的 Anthropic 错误类型            |
| `error_message_patterns` | list[str] | `["quota", "limit exceeded", "usage cap", "capacity"]`  | 触发故障转移的错误消息关键词（不区分大小写） |

#### model_mapping — 模型映射规则

每条规则包含：

| 字段       | 类型   | 说明                             |
| ---------- | ------ | -------------------------------- |
| `pattern`  | string | 匹配模式                         |
| `target`   | string | 目标模型名称                     |
| `is_regex` | bool   | 是否为正则表达式（默认 `false`） |

**匹配优先级**：精确匹配 > 正则/通配符匹配 > 默认值 (`glm-5.1`)

#### database — 数据库

| 字段   | 类型   | 默认值                       | 说明                                 |
| ------ | ------ | ---------------------------- | ------------------------------------ |
| `path` | string | `"~/.coding-proxy/usage.db"` | SQLite 数据库文件路径，支持 `~` 展开 |

#### logging — 日志

| 字段    | 类型           | 默认值   | 说明                                       |
| ------- | -------------- | -------- | ------------------------------------------ |
| `level` | string         | `"INFO"` | 日志级别（DEBUG / INFO / WARNING / ERROR） |
| `file`  | string \| null | `null`   | 日志文件路径，`null` 表示输出到控制台      |

### 3.4 环境变量引用

配置文件中可使用 `${VARIABLE_NAME}` 语法引用环境变量：

```yaml
fallback:
  api_key: "${ZHIPU_API_KEY}"
```

启动时，`${ZHIPU_API_KEY}` 会被替换为环境变量 `ZHIPU_API_KEY` 的值。如果环境变量未设置，保留原始文本 `${ZHIPU_API_KEY}`。

---

## 4. CLI 命令参考

### 4.1 coding-proxy start

启动代理服务。

```bash
coding-proxy start [OPTIONS]
```

| 参数       | 缩写 | 说明                     |
| ---------- | ---- | ------------------------ |
| `--config` | `-c` | 配置文件路径             |
| `--port`   | `-p` | 监听端口（覆盖配置文件） |
| `--host`   | `-h` | 监听地址（覆盖配置文件） |

**示例**：

```bash
# 默认配置启动
coding-proxy start

# 自定义端口和配置
coding-proxy start -p 9000 -c ~/my-config.yaml
```

### 4.2 coding-proxy status

查看当前代理状态和熔断器信息。

```bash
coding-proxy status [OPTIONS]
```

| 参数     | 缩写 | 说明                      |
| -------- | ---- | ------------------------- |
| `--port` | `-p` | 代理服务端口（默认 8046） |

**输出示例**：

```
熔断器状态: closed
主后端: anthropic
备选后端: zhipu
连续失败次数: 0
恢复超时(s): 300
```

**熔断器状态说明**：

| 状态        | 含义                              |
| ----------- | --------------------------------- |
| `closed`    | 正常运行，使用主后端（Anthropic） |
| `open`      | 熔断中，使用备选后端（智谱）      |
| `half_open` | 恢复测试中，尝试使用主后端        |

### 4.3 coding-proxy usage

查看 Token 使用统计。

```bash
coding-proxy usage [OPTIONS]
```

| 参数        | 缩写 | 说明                                      |
| ----------- | ---- | ----------------------------------------- |
| `--days`    | `-d` | 统计天数（默认 7）                        |
| `--backend` | `-b` | 过滤指定后端（如 `anthropic` 或 `zhipu`） |
| `--db`      | —    | 数据库文件路径                            |

**示例**：

```bash
# 查看最近 7 天统计
coding-proxy usage

# 查看最近 30 天 Anthropic 后端统计
coding-proxy usage -d 30 -b anthropic
```

### 4.4 coding-proxy reset

手动重置熔断器为 CLOSED 状态（恢复使用主后端）。

```bash
coding-proxy reset [OPTIONS]
```

| 参数     | 缩写 | 说明                      |
| -------- | ---- | ------------------------- |
| `--port` | `-p` | 代理服务端口（默认 8046） |

**使用场景**：确认 Anthropic API 已恢复正常后，手动强制切回主后端，无需等待自动恢复超时。

### 4.5 HTTP API 端点

除 CLI 命令外，coding-proxy 还提供以下 HTTP 端点：

#### POST /v1/messages

代理 Anthropic Messages API，支持流式和非流式请求。

```bash
curl -X POST http://127.0.0.1:8046/v1/messages \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $ANTHROPIC_API_KEY" \
  -H "anthropic-version: 2023-06-01" \
  -d '{"model":"claude-sonnet-4-20250514","max_tokens":1024,"messages":[{"role":"user","content":"Hello"}]}'
```

#### GET /health

健康检查。

```bash
curl http://127.0.0.1:8046/health
# {"status":"ok"}
```

#### GET /api/status

查询熔断器状态和后端信息。

```bash
curl http://127.0.0.1:8046/api/status
# {"circuit_breaker":{"state":"closed","failure_count":0,...},"primary":"anthropic","fallback":"zhipu"}
```

#### POST /api/reset

手动重置熔断器。

```bash
curl -X POST http://127.0.0.1:8046/api/reset
# {"status":"ok","circuit_breaker":{"state":"closed",...}}
```

---

## 5. Claude Code 集成指南

### 5.1 配置 Claude Code 使用代理

启动 coding-proxy 后，设置环境变量让 Claude Code 通过代理发送请求：

```bash
export ANTHROPIC_BASE_URL=http://127.0.0.1:8046
```

Claude Code 使用的 OAuth token 会被代理透传到 Anthropic API，无需额外配置认证信息。

### 5.2 验证集成

1. 确保 coding-proxy 正在运行：`coding-proxy status`
2. 使用 Claude Code 发送一条消息
3. 查看 coding-proxy 的终端日志，确认请求经过代理
4. 使用 `coding-proxy usage` 查看是否有新的用量记录

### 5.3 日常使用流程

1. **启动代理**：`coding-proxy start`（可使用 `nohup` 或 `tmux` 后台运行）
2. **正常使用 Claude Code**：代理在后台透明工作
3. **定期查看用量**：`coding-proxy usage` 了解 Token 消耗和后端分布
4. **按需手动干预**：`coding-proxy reset` 在确认主后端恢复后强制切回

---

## 6. 常见使用场景

### 6.1 Anthropic API 限流自动切换

**现象**：Claude Code 响应变慢或提示 "rate limited"

**代理行为**：
1. 检测到 Anthropic 返回 `429 rate_limit_error`
2. 熔断器记录失败，达到阈值后切换到 OPEN 状态
3. 后续请求自动路由到智谱 GLM 后端
4. 等待恢复超时后自动尝试切回 Anthropic

**用户操作**：无需干预，代理自动处理。

### 6.2 配额耗尽后使用备用后端

**现象**：Anthropic 返回 `403` 错误，消息含 "usage cap" 或 "quota"

**代理行为**：
1. 识别错误消息中的关键词（"quota"、"usage cap"）
2. 触发故障转移到智谱后端
3. Claude Code 继续正常工作（使用 GLM 模型）

**用户操作**：无需干预。可通过 `coding-proxy usage` 查看哪些请求走了备用后端。

### 6.3 手动切换后端

**场景**：确认 Anthropic API 已恢复，希望立即切回而不等待自动恢复。

```bash
# 重置熔断器
coding-proxy reset

# 确认状态
coding-proxy status
# 熔断器状态: closed ← 已恢复使用主后端
```

### 6.4 仅使用智谱后端

如果希望跳过 Anthropic API，始终使用智谱后端，可将熔断器阈值设为极低值：

```yaml
circuit_breaker:
  failure_threshold: 1    # 首次失败即切换
```

或者将故障转移状态码设为空列表以完全禁用故障转移：

```yaml
failover:
  status_codes: []
```

---

## 7. 监控与运维

### 7.1 日志查看

代理服务日志默认输出到控制台，包含以下关键事件：

| 事件           | 日志级别     | 示例                                                      |
| -------------- | ------------ | --------------------------------------------------------- |
| 熔断器状态转换 | INFO/WARNING | `Circuit breaker: CLOSED → OPEN (3 consecutive failures)` |
| 故障转移触发   | WARNING      | `Primary error 429, failing over`                         |
| 恢复成功       | INFO         | `Circuit breaker: HALF_OPEN → CLOSED (recovered)`         |
| 连接错误       | WARNING      | `Primary connection error: ConnectTimeout`                |

可通过配置调整日志级别：

```yaml
logging:
  level: "DEBUG"    # 查看详细的模型映射和路由决策
  file: "/var/log/coding-proxy.log"  # 输出到文件
```

### 7.2 用量统计

```bash
# 查看最近 7 天统计
coding-proxy usage

# 查看最近 30 天，仅 Anthropic 后端
coding-proxy usage -d 30 -b anthropic

# 查看最近 30 天，仅智谱后端
coding-proxy usage -d 30 -b zhipu
```

统计字段说明：

| 字段         | 说明                         |
| ------------ | ---------------------------- |
| 日期         | 统计日期                     |
| 后端         | `anthropic` 或 `zhipu`       |
| 请求数       | 当日总请求数                 |
| 输入 Token   | 当日总输入 Token 数          |
| 输出 Token   | 当日总输出 Token 数          |
| 故障转移次数 | 当日经过故障转移的请求数     |
| 平均耗时     | 当日请求平均响应时间（毫秒） |

### 7.3 健康检查

**基础检查**：

```bash
curl http://127.0.0.1:8046/health
```

**详细状态**：

```bash
curl http://127.0.0.1:8046/api/status
```

返回示例：

```json
{
  "circuit_breaker": {
    "state": "closed",
    "failure_count": 0,
    "success_count": 0,
    "current_recovery_seconds": 300,
    "last_failure_time": null
  },
  "primary": "anthropic",
  "fallback": "zhipu"
}
```

### 7.4 数据库维护

用量数据库位于 `~/.coding-proxy/usage.db`（可通过配置修改）。

- 数据库采用 SQLite WAL 模式，支持读写并发
- 当前版本不自动清理历史数据
- 如需清理，可直接删除数据库文件（重启后自动重建）

---

## 8. 故障排查

### 8.1 代理服务无法启动

**端口占用**：

```bash
lsof -i :8046
# 如有进程占用，先停止或更换端口
coding-proxy start --port 8080
```

**配置文件语法错误**：

检查 YAML 格式是否正确（缩进、冒号后的空格等）。常见错误：

```yaml
# 错误：冒号后缺少空格
port:8046

# 正确
port: 8046
```

**Python 版本不满足**：

```bash
python --version
# 需要 Python >= 3.13
```

### 8.2 Claude Code 无法连接代理

1. 确认代理服务正在运行：

```bash
coding-proxy status
# 如果提示 "代理服务未运行"，先启动服务
```

2. 确认环境变量设置正确：

```bash
echo $ANTHROPIC_BASE_URL
# 应输出: http://127.0.0.1:8046
```

3. 确认代理端口与环境变量一致

### 8.3 频繁触发故障转移

如果发现频繁在主备后端之间切换：

1. 检查 Anthropic API 状态（是否正在经历服务波动）
2. 适当调高 `failure_threshold`（如从 3 改为 5）
3. 适当调高 `recovery_timeout_seconds`（给主后端更多恢复时间）
4. 查看日志确认触发原因（状态码、错误类型、错误消息）

### 8.4 智谱后端返回错误

1. **API Key 错误**：确认 `ZHIPU_API_KEY` 环境变量已正确设置
2. **模型不存在**：检查 `model_mapping` 规则，确认目标模型名称有效
3. **网络问题**：确认可以访问 `open.bigmodel.cn`

### 8.5 Token 用量不记录

1. 确认数据库路径目录可写：

```bash
ls -la ~/.coding-proxy/
```

2. 如果目录不存在，代理会在启动时自动创建
3. 流式请求的用量提取依赖 SSE 事件格式，如果后端返回非标准格式可能无法正确解析

---

## 9. 常见问题 (FAQ)

**Q: coding-proxy 支持哪些 Claude Code 版本？**

A: 支持所有使用 Anthropic Messages API (`/v1/messages`) 的 Claude Code 版本。

**Q: 代理会影响响应速度吗？**

A: 代理层自身开销极小，不影响实际使用体验。主要延迟来源于上游 API。

**Q: 智谱 GLM 的响应与 Anthropic Claude 完全一致吗？**

A: 智谱提供 Anthropic 兼容接口，响应格式一致，但底层模型不同，生成内容和能力可能存在差异。

**Q: 如何防止 API Key 泄露？**

A: 推荐通过环境变量注入（`${ZHIPU_API_KEY}`），`config.yaml` 已在 `.gitignore` 中，不会被提交到版本库。

**Q: 可以同时运行多个 coding-proxy 实例吗？**

A: 可以，使用不同端口即可。每个实例可使用独立的配置文件和数据库。

**Q: 熔断器的指数退避是什么意思？**

A: 每次恢复测试失败后，等待时间翻倍（300s → 600s → 1200s → 2400s → 3600s），上限为 `max_recovery_seconds`。这样可以避免对仍未恢复的后端进行频繁的无效重试。

**Q: 如何完全禁用故障转移？**

A: 将 `failover.status_codes` 设为空列表 `[]`，代理将不再自动切换后端。

**Q: 数据库文件会无限增长吗？**

A: 当前版本不自动清理历史数据。数据库文件大小取决于请求频率，日常使用增长较慢。如需清理，可删除 `usage.db` 文件，重启后自动重建。
