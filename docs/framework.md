# coding-proxy 架构设计与工程方案

## 1. 项目概述

### 1.1 项目动机与背景

Claude Code 作为日常 AI 编程助手，其底层依赖 Anthropic Messages API (`/v1/messages`)。在实际使用过程中，以下场景时有发生：

- **限流 (Rate Limiting)**：高频请求触发 `429 rate_limit_error`，导致短时间内无法继续使用
- **配额耗尽 (Usage Cap)**：月度/日度配额用尽后返回 `403` 错误，含 "usage cap" 等提示
- **服务过载 (Overloaded)**：Anthropic 服务端高峰期返回 `503 overloaded_error`
- **网络波动**：国际链路不稳定导致连接超时

与此同时，智谱 (Zhipu) 提供了与 Anthropic 兼容的 GLM API 接口（`/api/anthropic`），为构建备用通道提供了可能。

**coding-proxy 的核心诉求**：在主 API 不可用时自动、无缝地切换到备用后端，对 Claude Code 客户端完全透明，用户无需手动干预。

### 1.2 设计目标

| 目标 | 说明 |
|------|------|
| **透明代理** | 对 Claude Code 完全透明，客户端无需修改任何协议或配置（仅需指定代理地址） |
| **自动故障转移** | 检测到主后端异常时自动切换备用后端，并在主后端恢复后自动切回 |
| **可观测性** | Token 用量持久化追踪、后端状态实时查询、熔断器状态监控 |
| **可扩展性** | 易于添加新后端实现、新模型映射规则、新故障转移策略 |
| **轻量部署** | 单进程运行，仅依赖 SQLite（无外部数据库/消息队列），适合本地开发环境 |

---

## 2. 系统架构总览

### 2.1 架构分层图

```
┌─────────────────────────────────────────────────┐
│               Claude Code 客户端                 │
└────────────────────┬────────────────────────────┘
                     │ HTTP POST /v1/messages
┌────────────────────▼────────────────────────────┐
│            FastAPI Server (server/app.py)         │
│                                                   │
│  ┌─────────────────────────────────────────────┐ │
│  │        RequestRouter (routing/router.py)      │ │
│  │                                               │ │
│  │  ┌──────────────────┐ ┌───────────────────┐  │ │
│  │  │  CircuitBreaker   │ │   TokenLogger     │  │ │
│  │  │  (circuit_breaker │ │   (logging/db.py) │  │ │
│  │  │   .py)            │ │                   │  │ │
│  │  └──────────────────┘ └───────────────────┘  │ │
│  └────────┬──────────────────────┬──────────────┘ │
│           │ Primary              │ Fallback        │
│  ┌────────▼─────────┐  ┌────────▼──────────────┐ │
│  │ AnthropicBackend  │  │ ZhipuBackend          │ │
│  │ (backends/        │  │ (backends/zhipu.py)   │ │
│  │  anthropic.py)    │  │ + ModelMapper         │ │
│  └────────┬─────────┘  └────────┬──────────────┘ │
└───────────┼──────────────────────┼────────────────┘
            │                      │
   ┌────────▼─────────┐  ┌────────▼──────────────┐
   │  Anthropic API    │  │  智谱 GLM API          │
   │  (官方)           │  │  (Anthropic 兼容接口)  │
   └──────────────────┘  └───────────────────────┘
```

### 2.2 模块职责一览

| 模块 | 路径 | 职责 |
|------|------|------|
| **server** | `server/app.py` | 应用工厂、HTTP 端点定义、生命周期管理 |
| **backends** | `backends/` | 后端抽象基类与具体实现（Anthropic、Zhipu） |
| **routing** | `routing/` | 请求路由、熔断器状态机、模型名称映射 |
| **config** | `config/` | Pydantic 配置模型定义与 YAML 加载 |
| **logging** | `logging/` | Token 用量 SQLite 持久化与统计查询 |
| **cli** | `cli.py` | Typer 命令行入口（start、status、usage、reset） |

### 2.3 技术选型

| 技术 | 选型理由 |
|------|---------|
| **Python 3.13+** | 原生 async/await 成熟、类型提示完善、生态丰富 |
| **FastAPI** | 原生异步、`StreamingResponse` 支持 SSE、自动 OpenAPI 文档 |
| **httpx** | 同时支持同步/异步、流式请求、完整的 HTTP 客户端功能 |
| **Pydantic v2** | 配置校验与类型安全、性能显著优于 v1 |
| **aiosqlite** | 异步 SQLite 访问、WAL 模式支持并发读写 |
| **Typer + Rich** | 现代化 CLI 体验、类型安全的参数声明、美观的终端输出 |
| **UV** | 极速包管理器、lockfile 确保可复现构建 |

---

## 3. 设计模式详解

### 3.1 Template Method（模板方法模式）

> **经典出处**：GoF《Design Patterns: Elements of Reusable Object-Oriented Software》— 定义算法骨架，将某些步骤延迟到子类实现。

**应用位置**：`backends/base.py` — `BaseBackend` 抽象基类

**设计要点**：

`BaseBackend` 定义了请求处理的算法骨架，将差异化的逻辑延迟到子类：

```
BaseBackend（模板）
├── send_message()          ← 固定流程：prepare → send → parse response
├── send_message_stream()   ← 固定流程：prepare → stream → yield chunks
├── _get_client()           ← 公共逻辑：惰性初始化 httpx.AsyncClient
├── close()                 ← 公共逻辑：关闭 HTTP 客户端
│
├── _prepare_request()      ← 【抽象】子类实现请求转换
├── get_name()              ← 【抽象】子类返回后端标识
└── should_trigger_failover() ← 【抽象】子类定义故障转移条件
```

两个具体子类的差异化实现：

| 方法 | AnthropicBackend | ZhipuBackend |
|------|-----------------|--------------|
| `_prepare_request()` | 过滤 hop-by-hop 头，透传 OAuth token | 映射模型名称，替换为 API Key 认证 |
| `should_trigger_failover()` | 检查 status_codes + error_types + message_patterns | 始终返回 `False`（最终后端） |

### 3.2 Circuit Breaker（熔断器模式）

> **经典出处**：Martin Fowler "CircuitBreaker" (2014)；Michael Nygard《Release It! Design and Deploy Production-Ready Software》第 5 章 — 通过快速失败防止级联故障。

**应用位置**：`routing/circuit_breaker.py` — `CircuitBreaker` 类

**状态机**：

```
                  连续 N 次失败
 ┌────────┐ ──────────────────→ ┌────────┐
 │ CLOSED │                     │  OPEN  │ ◄──┐
 │ (正常)  │                     │ (熔断)  │    │
 └────────┘                     └───┬────┘    │
      ▲                             │          │
      │ 连续 M 次成功      超时恢复  │          │ 失败（退避×2）
      │                             ▼          │
      │                       ┌──────────┐     │
      └───────────────────────│HALF_OPEN │─────┘
                              │ (试探)    │
                              └──────────┘
```

**状态转换条件**：

| 转换 | 条件 | 默认值 |
|------|------|--------|
| CLOSED → OPEN | 连续失败次数 ≥ `failure_threshold` | 3 次 |
| OPEN → HALF_OPEN | 距上次失败 ≥ `recovery_timeout_seconds` | 300 秒 |
| HALF_OPEN → CLOSED | 连续成功次数 ≥ `success_threshold` | 2 次 |
| HALF_OPEN → OPEN | 任意一次失败 | — |

**指数退避 (Exponential Backoff)**：每次从 HALF_OPEN 回退到 OPEN 时，恢复等待时间翻倍（`recovery_timeout *= 2`），上限为 `max_recovery_seconds`（默认 3600 秒）。避免对仍未恢复的后端频繁重试。

**线程安全**：所有状态变更通过 `threading.Lock` 保护，确保并发请求下状态一致。

### 3.3 Strategy（策略模式）

> **经典出处**：GoF《Design Patterns》— 定义一系列算法，将它们封装起来并使它们可互换。

**应用位置**：`routing/model_mapper.py` — `ModelMapper` 类

**设计要点**：

ModelMapper 采用三级匹配策略链，按优先级依次尝试：

```
输入模型名称
    │
    ▼
 1. 精确匹配（Exact Match）
    │ pattern == model（无通配符、非正则）
    ├─ 命中 → 返回 target
    │
    ▼
 2. 模式匹配（Regex / Glob Match）
    │ is_regex=true → re.fullmatch()
    │ 含 * → fnmatch.fnmatch()
    ├─ 命中 → 返回 target
    │
    ▼
 3. 默认值（Default Fallback）
    └─ 返回 "glm-5.1"
```

**默认映射规则**：

| 模式 | 目标 | 类型 |
|------|------|------|
| `claude-sonnet-.*` | `glm-5.1` | 正则 |
| `claude-opus-.*` | `glm-5.1` | 正则 |
| `claude-haiku-.*` | `glm-4.5-air` | 正则 |
| `claude-.*` | `glm-5.1` | 正则（兜底） |

正则表达式在 `__init__` 时预编译（`re.compile()`），`map()` 调用时直接使用编译后的对象，避免重复编译开销。

### 3.4 Factory Method（工厂方法模式）

> **经典出处**：GoF《Design Patterns》— 定义创建对象的接口，由子类决定实例化哪个类。

**应用位置**：`server/app.py` — `create_app()` 函数

**组装顺序**：

```
create_app(config)
    │
    ├─ 1. TokenLogger(config.db_path)
    ├─ 2. ModelMapper(config.model_mapping)
    ├─ 3. AnthropicBackend(config.primary, config.failover)
    ├─ 4. ZhipuBackend(config.fallback, config.failover, mapper)
    ├─ 5. CircuitBreaker(config.circuit_breaker.*)
    ├─ 6. RequestRouter(primary, fallback, cb, token_logger)
    │
    └─ 7. FastAPI(lifespan=lifespan)
         ├─ app.state.router = router
         ├─ app.state.token_logger = token_logger
         └─ app.state.config = config
```

通过 `lifespan` 异步上下文管理器管理应用生命周期：
- **启动**：初始化 TokenLogger（创建数据库表）
- **关闭**：关闭 RequestRouter（释放 HTTP 客户端）和 TokenLogger（关闭数据库连接）

### 3.5 Proxy（代理模式）

> **经典出处**：GoF《Design Patterns》— 为其他对象提供一种代理以控制对这个对象的访问。

**应用位置**：整体架构

coding-proxy 本身即是一个代理服务：

- 对外暴露与 Anthropic Messages API 完全兼容的 `POST /v1/messages` 接口
- Claude Code 客户端只需将 `ANTHROPIC_BASE_URL` 指向代理地址
- 代理在幕后完成后端选择、故障转移、模型映射、用量记录等增值逻辑
- 支持流式（SSE `text/event-stream`）和非流式（JSON）两种响应模式

---

## 4. 请求生命周期

### 4.1 完整请求流程

```
Client POST /v1/messages
        │
        ▼
 app.messages() 解析 body + headers
        │
        ├─ stream=true ──→ route_stream()
        └─ stream=false ─→ route_message()
                │
                ▼
     CircuitBreaker.can_execute()?
                │
        ┌───────┴───────┐
        │ Yes           │ No (OPEN)
        ▼               │
   Primary Backend      │
        │               │
   ┌────┴────┐          │
   │ 成功     │ 失败     │
   │         │          │
   │  record  │ should   │
   │ success  │ failover?│
   │         │          │
   │    ┌────┴───┐      │
   │    │Yes    │No     │
   │    │       │       │
   │    │record │return │
   │    │failure│error  │
   │    │       │       │
   │    ▼       ▼       │
   │  ┌─────────────┐   │
   │  │             │◄──┘
   │  │  Fallback   │
   │  │  Backend    │
   │  │             │
   │  └──────┬──────┘
   │         │
   ▼         ▼
 TokenLogger.log()
        │
        ▼
   Response → Client
```

### 4.2 流式请求处理

流式请求使用 `StreamingResponse` + 异步生成器 `_stream_proxy()`：

1. `RequestRouter.route_stream()` 返回 `AsyncIterator[tuple[bytes, str]]`
2. 每个 SSE chunk 通过 `_parse_usage_from_chunk()` 提取 Token 用量：
   - `message_start` 事件：提取 `input_tokens`、`cache_creation_input_tokens`、`cache_read_input_tokens`、`request_id`
   - `message_delta` 事件：提取 `output_tokens`
3. chunk 原样透传给客户端
4. 流结束后记录完整用量到 TokenLogger

**故障转移时**：清空已收集的 usage 数据（`usage.clear()`），从 Fallback Backend 重新开始流式传输。

### 4.3 非流式请求处理

非流式请求直接调用 `send_message()` 获取完整响应：

1. 主后端返回成功（`status_code < 400`）→ 记录 success → 返回
2. 主后端返回错误 → 检查 `should_trigger_failover()`：
   - 是 → `record_failure()` → 尝试 Fallback
   - 否 → 直接返回错误响应
3. 捕获 `httpx.TimeoutException` / `httpx.ConnectError` → 触发故障转移

### 4.4 故障转移判定逻辑

故障转移的判定在 `AnthropicBackend.should_trigger_failover()` 中实现，依据三层条件（可通过配置文件自定义）：

| 层级 | 条件 | 默认值 |
|------|------|--------|
| HTTP 状态码 | `status_code in failover.status_codes` | `[429, 403, 503, 500]` |
| 错误类型 | `error.type in failover.error_types` | `["rate_limit_error", "overloaded_error", "api_error"]` |
| 错误消息 | `pattern in error.message`（不区分大小写） | `["quota", "limit exceeded", "usage cap", "capacity"]` |

**特殊规则**：对于 429 和 503 状态码，即使无法解析响应体（body），也会强制触发故障转移。

**ZhipuBackend** 作为最终备选后端，`should_trigger_failover()` 始终返回 `False`。

---

## 5. 模块详细设计

### 5.1 backends/ — 后端模块

**数据结构**：

```python
@dataclass
class UsageInfo:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_creation_tokens: int = 0
    cache_read_tokens: int = 0
    request_id: str = ""

@dataclass
class BackendResponse:
    status_code: int = 200
    usage: UsageInfo = field(default_factory=UsageInfo)
    is_streaming: bool = False
    raw_body: bytes = b"{}"
    error_type: str | None = None
    error_message: str | None = None
```

**AnthropicBackend**：
- 过滤 hop-by-hop 头（`host`、`content-length`、`transfer-encoding`、`connection`）
- 透传客户端的 OAuth token（`authorization` 头）
- 不修改请求体

**ZhipuBackend**：
- 浅拷贝请求体（`{**request_body}`），避免修改原始数据
- 调用 `ModelMapper.map()` 映射模型名称
- 替换认证头为 `x-api-key`
- 保持 `anthropic-version` 头兼容

**HTTP 客户端管理**：
- 惰性初始化 `httpx.AsyncClient`（首次调用时创建）
- 自动检测并重建已关闭的客户端（`is_closed` 检查）
- `close()` 方法释放连接资源

### 5.2 routing/ — 路由模块

**RequestRouter**：
- 持有四个依赖：`primary`、`fallback`、`circuit_breaker`、`token_logger`
- `route_message()` → 非流式路由，返回 `BackendResponse`
- `route_stream()` → 流式路由，yield `(chunk, backend_name)`
- `_record_usage()` → 统一记录用量到 TokenLogger

**CircuitBreaker 参数**：

| 参数 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `failure_threshold` | int | 3 | 触发 OPEN 的连续失败次数 |
| `recovery_timeout_seconds` | int | 300 | OPEN → HALF_OPEN 等待秒数 |
| `success_threshold` | int | 2 | HALF_OPEN → CLOSED 所需连续成功次数 |
| `max_recovery_seconds` | int | 3600 | 指数退避上限秒数 |

### 5.3 config/ — 配置模块

**配置搜索优先级**：

```
CLI --config 参数（显式指定）
    ↓ 未指定时
./config.yaml（项目根目录）
    ↓ 不存在时
~/.coding-proxy/config.yaml（用户目录）
    ↓ 不存在时
Pydantic 默认值
```

**环境变量展开**：
- 语法：`${VARIABLE_NAME}`
- 实现：正则 `\$\{([^}]+)\}` 匹配，递归处理 dict/list/str
- 未定义的变量保留原始 `${VAR}` 文本
- 适用场景：API Key 等敏感信息不宜写入配置文件

### 5.4 logging/ — 日志模块

**usage_log 表结构**：

| 列名 | 类型 | 说明 |
|------|------|------|
| `id` | INTEGER PK | 自增主键 |
| `ts` | TEXT | 时间戳（ISO 8601 格式，UTC） |
| `backend` | TEXT | 后端标识（`"anthropic"` / `"zhipu"`） |
| `model_requested` | TEXT | 客户端请求的模型名称 |
| `model_served` | TEXT | 实际使用的模型名称 |
| `input_tokens` | INTEGER | 输入 Token 数 |
| `output_tokens` | INTEGER | 输出 Token 数 |
| `cache_creation_tokens` | INTEGER | 缓存创建 Token 数 |
| `cache_read_tokens` | INTEGER | 缓存读取 Token 数 |
| `duration_ms` | INTEGER | 请求耗时（毫秒） |
| `success` | BOOLEAN | 是否成功 |
| `failover` | BOOLEAN | 是否经过故障转移 |
| `request_id` | TEXT | Anthropic 请求 ID |

**索引**：`idx_usage_ts`（时间戳）、`idx_usage_backend`（后端名）

**SQLite 优化**：WAL (Write-Ahead Logging) 模式，支持读写并发而不互相阻塞。

### 5.5 server/ — 服务模块

**API 端点**：

| 端点 | 方法 | 说明 |
|------|------|------|
| `/v1/messages` | POST | 代理 Anthropic Messages API（流式 + 非流式） |
| `/health` | GET | 健康检查，返回 `{"status": "ok"}` |
| `/api/status` | GET | 熔断器状态、主/备后端信息 |
| `/api/reset` | POST | 手动重置熔断器为 CLOSED |

---

## 6. 配置系统设计

### 6.1 完整配置字段参考

**server — 服务器配置**

| 字段 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `host` | str | `"127.0.0.1"` | 监听地址 |
| `port` | int | `8046` | 监听端口 |

**primary — 主后端（Anthropic）配置**

| 字段 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `enabled` | bool | `true` | 是否启用 |
| `base_url` | str | `"https://api.anthropic.com"` | API 基础地址 |
| `timeout_ms` | int | `300000` | 请求超时（毫秒），默认 5 分钟 |

**fallback — 备选后端（智谱）配置**

| 字段 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `enabled` | bool | `true` | 是否启用 |
| `base_url` | str | `"https://open.bigmodel.cn/api/anthropic"` | 智谱 Anthropic 兼容接口地址 |
| `api_key` | str | `""` | 智谱 API Key，支持 `${ENV_VAR}` 引用 |
| `timeout_ms` | int | `3000000` | 请求超时（毫秒），默认 50 分钟 |

**circuit_breaker — 熔断器配置**

| 字段 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `failure_threshold` | int | `3` | 触发熔断的连续失败次数 |
| `recovery_timeout_seconds` | int | `300` | 熔断后恢复等待时间（秒） |
| `success_threshold` | int | `2` | 半开状态恢复所需连续成功次数 |
| `max_recovery_seconds` | int | `3600` | 指数退避最大恢复时间（秒） |

**failover — 故障转移触发条件**

| 字段 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `status_codes` | list[int] | `[429, 403, 503, 500]` | 触发转移的 HTTP 状态码 |
| `error_types` | list[str] | `["rate_limit_error", "overloaded_error", "api_error"]` | 触发转移的错误类型 |
| `error_message_patterns` | list[str] | `["quota", "limit exceeded", "usage cap", "capacity"]` | 触发转移的消息关键词 |

**model_mapping — 模型名称映射规则**

| 字段 | 类型 | 说明 |
|------|------|------|
| `pattern` | str | 匹配模式（支持精确匹配、glob 通配符、正则表达式） |
| `target` | str | 目标模型名称 |
| `is_regex` | bool | 是否为正则表达式（默认 `false`） |

**database — 数据库配置**

| 字段 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `path` | str | `"~/.coding-proxy/usage.db"` | SQLite 数据库文件路径 |

**logging — 日志配置**

| 字段 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `level` | str | `"INFO"` | 日志级别 |
| `file` | str \| null | `null` | 日志文件路径（null 输出到控制台） |

---

## 7. 可扩展性设计

### 7.1 添加新后端

1. 在 `backends/` 下创建新模块，继承 `BaseBackend`
2. 实现三个抽象方法：
   - `get_name()` — 返回后端标识字符串
   - `_prepare_request()` — 转换请求体和请求头
   - `should_trigger_failover()` — 定义何时触发故障转移
3. 在 `config/schema.py` 中添加对应的配置模型
4. 在 `server/app.py` 的 `create_app()` 中实例化并注册

### 7.2 添加新映射规则

在配置文件的 `model_mapping` 节添加规则即可，无需修改代码：

```yaml
model_mapping:
  - pattern: "claude-sonnet-4-6"   # 精确匹配
    target: "glm-5.1"
  - pattern: "claude-haiku-.*"      # 正则匹配
    target: "glm-4.5-air"
    is_regex: true
  - pattern: "claude-*"             # Glob 通配符
    target: "glm-5.1"
```

匹配优先级确保精确匹配不会被通配符覆盖。

### 7.3 自定义故障转移策略

通过配置文件调整 `failover` 节的三个字段即可自定义触发条件：

- 增减 `status_codes` 控制哪些 HTTP 状态码触发切换
- 增减 `error_types` 控制哪些 Anthropic 错误类型触发切换
- 增减 `error_message_patterns` 控制哪些错误消息关键词触发切换

如需更复杂的策略，可重写子类的 `should_trigger_failover()` 方法。

---

## 8. 测试策略

### 8.1 单元测试覆盖

| 测试文件 | 覆盖范围 |
|---------|---------|
| `test_circuit_breaker.py` | 状态转换（CLOSED→OPEN→HALF_OPEN→CLOSED）、恢复超时、指数退避、手动重置 |
| `test_model_mapper.py` | 精确匹配、正则匹配、Glob 匹配、默认回退、空规则集 |
| `test_backends.py` | 请求头过滤、模型映射、故障转移判断、数据类默认值 |
| `test_config_loader.py` | 配置文件搜索优先级、环境变量展开、缺失文件处理 |

### 8.2 测试工具

- **pytest** (>=9.0) — 测试框架
- **pytest-asyncio** (>=1.3) — 异步测试支持
- **monkeypatch** — 环境变量和工作目录隔离
- **tmp_path** — 临时文件测试
