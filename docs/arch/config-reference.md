# 配置字段参考

> **路径约定**：本文档中模块路径均相对于 `src/coding/proxy/`。
>
> **定位**：本文档是所有配置参数的**规范来源（Single Source of Truth）**。设计模式章节和模块详情中引用参数默认值时，统一链接至此。

[TOC]

---

## 1. 配置模块结构

配置模型已从单体 `schema.py` 正交拆分为 5 个子模块：

| 子模块          | 文件                                                                    | 核心类型                                                                                                                                               |
| --------------- | ----------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------ |
| **server**      | [`config/server.py`](../../src/coding/proxy/config/server.py)           | `ServerConfig`, `DatabaseConfig`, `LoggingConfig`                                                                                                      |
| **vendors**     | [`config/vendors.py`](../../src/coding/proxy/config/vendors.py)         | `AnthropicConfig`, `CopilotConfig`, `AntigravityConfig`, `ZhipuConfig`, `MinimaxConfig`, `KimiConfig`, `DoubaoConfig`, `XiaomiConfig`, `AlibabaConfig` |
| **resiliency**  | [`config/resiliency.py`](../../src/coding/proxy/config/resiliency.py)   | `CircuitBreakerConfig`, `RetryConfig`, `FailoverConfig`, `QuotaGuardConfig`                                                                            |
| **routing**     | [`config/routing.py`](../../src/coding/proxy/config/routing.py)         | `VendorType`, `VendorConfig`, `ModelMappingRule`, `ModelPricingEntry`                                                                                  |
| **auth_schema** | [`config/auth_schema.py`](../../src/coding/proxy/config/auth_schema.py) | `AuthConfig`                                                                                                                                           |

`config/schema.py` 作为聚合入口点 re-export 所有符号，并保留 `ProxyConfig` 顶层模型及旧格式迁移逻辑。

运行时配置的存储与解析由 `config/` 下的新增模块承担：

| 模块              | 文件                                                                      | 职责                                                                            |
| ----------------- | ------------------------------------------------------------------------- | ------------------------------------------------------------------------------- |
| **home**          | [`config/home.py`](../../src/coding/proxy/config/home.py)                 | `proxy_home()`（env `CODING_PROXY_HOME` → 默认 `~/.coding-proxy`）与派生路径    |
| **defaults**      | [`config/defaults.py`](../../src/coding/proxy/config/defaults.py)         | 随包 `config.default.yaml` 基线的解析（默认值单一事实源）                       |
| **store**         | [`config/store.py`](../../src/coding/proxy/config/store.py)               | `config.db`（sqlite3）读写、per-section revision 乐观并发、损坏/占用错误分类    |
| **secrets**       | [`config/secrets.py`](../../src/coding/proxy/config/secrets.py)           | 密钥三态记录（stored 密文 / env 变量 / empty）、Fernet 加解密、主密钥管理       |
| **resolver**      | [`config/resolver.py`](../../src/coding/proxy/config/resolver.py)         | `resolve_config()`：默认 ⊕ 覆盖 ⊕ 密钥注入 → `$tmpl` 展开 → `ProxyConfig`       |
| **importer**      | [`config/importer.py`](../../src/coding/proxy/config/importer.py)         | 旧 YAML 一次性导入（等价校验）、`boot()` 首启流程、YAML 一致性检查             |
| **service**       | [`config/service.py`](../../src/coding/proxy/config/service.py)           | `ConfigService`：Dashboard / CLI 共用的读写与校验服务                          |
| **ui_registry**   | [`config/ui_registry.py`](../../src/coding/proxy/config/ui_registry.py)   | Dashboard Config 页字段元数据（label / widget / 中文说明）                      |
| **loader**        | [`config/loader.py`](../../src/coding/proxy/config/loader.py)             | 旧 YAML 搜索链读取，保留为导入参考实现与等价基准（运行时不再使用）             |

---

## 2. 配置解析与存储

运行时配置源为 **`$CODING_PROXY_HOME/config.db`**（默认 `~/.coding-proxy/config.db`，sqlite3、0600）；`config.default.yaml` 退化为随包发布的默认值基线，用户不再编辑任何 YAML。

![配置解析与存储](../diagrams/config-resolution-storage/config-resolution-storage.svg)

上图（配置源 → 合并 → 密钥注入 → `$tmpl` 展开 → `ProxyConfig` 校验 → 运行时热切换，含三条写入路径与导入门）为双主题 SVG，深浅模式自适应；交互版与图源见 [`docs/diagrams/config-resolution-storage/`](../diagrams/config-resolution-storage/)。

**写入路径**（仅三条，均经 `ConfigService` 校验后持久化，保存即在进程内热切换生效）：

- Dashboard → Config 页（`PUT /api/config`，需 `X-Coding-Proxy-Admin` 头）
- CLI：`coding-proxy config set / reset / set-secret`（含 bootstrap 字段）
- `coding-proxy config import`（旧 YAML 一次性导入，见 §2.2）

**存储模型**：`config_section` 行存在 = 该 section 被覆盖，删除行 = 恢复默认；`secret` 表按路径独立存放密钥（Fernet 加密，主密钥为 `$CODING_PROXY_HOME/master.key` 或 env `CODING_PROXY_MASTER_KEY`），Reset section 不会丢失密钥；per-section revision 支持乐观并发（冲突返回 409）。

**环境变量展开**：语法 `${VARIABLE_NAME}`，递归处理 dict/list/str，未定义变量展开为空串。模板仅随包默认值与旧 YAML 导入进入存储；Dashboard / CLI 写入的字符串一律按字面量保存，不会再被展开。

### 2.1 密钥（只写不读）

密钥字段（各供应商 `api_key` / `github_token` / `client_id` / `client_secret` / `refresh_token`、`auth.google_client_secret`）在存储中脱离 section 文档：GET 只返回 `{kind, env_var?, resolved, hint, default_env?, recorded}`（`kind` 含 `stored` / `env` / `empty` / `default` 四态；`recorded` 标识是否存在密钥记录，前端据此预测「目标地址变更 → 清空」；长度 ≥ 16 才展示尾 4 位；完整载荷见 [API 参考 §11.1](../guide/api-reference.md#111-get-apiconfig)）；写入用 op 表达（`set` / `clear` / `default_env` / `env`），值从不回显。vendor 的 `base_url` / `token_url` 变化时该 vendor 的已存密钥自动清空（防密钥发往新目标），同一请求显式 `set` 可保留；新目标即包内默认端点（`reset` / 改回默认）时保留密钥。

### 2.2 旧 YAML 一次性导入（boot）

- 首次 `start`（或 `config import`）时，源为 `start -c FILE` 指定的路径或 `~/.coding-proxy/config.yaml`（存在时）；`./config.yaml` **不会**被自动导入。
- 导入前跑旧 loader 做等价校验：`resolve_config() == legacy load_config(source)`；不通过则不建库，本次以旧 YAML 只读运行（Config 页显示失败字段路径并禁写）。
- 源文件不改名、不改写；marker 记录 sha256。拒启（strict）覆盖：`start -c` 显式指定的文件（始终 strict，与导入来源无关）；该文件即导入源（marker.source 指向它）；或存储无导入源（`imported_from` 为 None，含首启）时旧版会读取的 `./config.yaml` / `~/.coding-proxy/config.yaml`；其余情形（如经 `config import /tmp/custom.yaml` 导入后，home 下遗留的旧 YAML 被修改）只提示「不一致，确认无用后请删除」，不拒启。
- 旧 `config/loader.py` 搜索链（`./config.yaml` → `~/.coding-proxy/config.yaml` → Pydantic 默认）保留为导入参考实现与测试基准，运行时不再读取。

---

## 3. 服务器配置

### 3.1 ServerConfig

| 字段   | 类型 | 默认值        | 说明     |
| ------ | ---- | ------------- | -------- |
| `host` | str  | `"127.0.0.1"` | 监听地址 |
| `port` | int  | `3392`        | 监听端口 |
| `allowed_hosts` | list[str] | `[]` | 管理面（Dashboard / 非透传 `/api/*`）Host 白名单的额外条目；`127.0.0.1` / `localhost` / `::1` 恒允许，绑定 IP 的本机访问亦放行。bootstrap 字段，经 `coding-proxy config set` 修改后需重启 |

### 3.2 DatabaseConfig

| 字段                       | 类型 | 默认值                        | 说明                                                |
| -------------------------- | ---- | ----------------------------- | --------------------------------------------------- |
| `path`                     | str  | `"~/.coding-proxy/usage.db"`  | SQLite 数据库文件路径（派生自 `$CODING_PROXY_HOME`） |
| `compat_state_path`        | str  | `"~/.coding-proxy/compat.db"` | 兼容性会话存储路径（同上派生）                      |
| `compat_state_ttl_seconds` | int  | `86400`                       | 兼容性会话 TTL（秒）                                |

### 3.3 LoggingConfig

| 字段           | 类型        | 默认值    | 说明                                     |
| -------------- | ----------- | --------- | ---------------------------------------- |
| `level`        | str         | `"INFO"`  | 控制台日志级别                           |
| `file`         | str \| null | `null`    | 文件日志路径；`null` 时 CLI 启动回落 `.logs/coding-proxy.log`，空串 `""` 禁用文件日志（仅控制台） |
| `max_bytes`    | int         | `5242880` | 单个日志文件最大字节数（5 MB），触发轮转 |
| `backup_count` | int         | `5`       | 保留的已压缩备份文件数                   |

### 3.4 AuthConfig

| 字段                | 类型             | 默认值                             | 说明                                                       |
| ------------------- | ---------------- | ---------------------------------- | ---------------------------------------------------------- |
| `token_store_path`  | str              | `~/.coding-proxy/tokens.json`（派生自 `$CODING_PROXY_HOME`） | Token Store 文件路径 |
| `github_client_id`  | str              | 公开 ID                            | GitHub OAuth Client ID（隐藏字段，仅导入/导出往返）         |
| `google_client_id`  | str              | 内置公开凭据                       | Google OAuth Client ID（开箱即用，可覆盖）                 |
| `google_client_secret` | 密钥            | 内置公开凭据（开箱即用）           | 密钥字段，经 `config set-secret` / Dashboard 写入          |

---

## 4. VendorConfig 通用字段

| 字段         | 类型 | 默认值   | 说明                                                                                                                 |
| ------------ | ---- | -------- | -------------------------------------------------------------------------------------------------------------------- |
| `vendor`     | enum | --       | 供应商类型：`anthropic` / `copilot` / `antigravity` / `zhipu` / `minimax` / `kimi` / `doubao` / `xiaomi` / `alibaba` |
| `enabled`    | bool | `true`   | 是否启用                                                                                                             |
| `base_url`   | str  | `""`     | API 基础 URL（留空使用各供应商默认值）                                                                               |
| `timeout_ms` | int  | `300000` | 请求超时（毫秒）                                                                                                     |

---

## 5. VendorConfig 弹性字段

| 字段                 | 类型           | 默认值               | 说明                                        |
| -------------------- | -------------- | -------------------- | ------------------------------------------- |
| `circuit_breaker`    | config \| None | `None`               | 熔断器配置（None = 不启用熔断，兜底由 tiers 中的位置决定） |
| `retry`              | config         | `RetryConfig()`      | 重试策略配置                                |
| `quota_guard`        | config         | `QuotaGuardConfig()` | 日度配额守卫配置                            |
| `weekly_quota_guard` | config         | `QuotaGuardConfig()` | 周度配额守卫配置                            |
| `concurrency`        | config \| None | `None`               | `[zhipu]` 每模型并发限制（详见 5.5）         |

<a id="elastic-params"></a>

### 5.1 CircuitBreakerConfig — 熔断器参数

> **设计语义**：参见 [设计模式 -- Circuit Breaker](./design-patterns.md#circuit-breaker)

| 字段                       | 类型 | 默认值 | 说明                              |
| -------------------------- | ---- | ------ | --------------------------------- |
| `failure_threshold`        | int  | `3`    | 触发 OPEN 的连续失败次数          |
| `recovery_timeout_seconds` | int  | `300`  | OPEN → HALF_OPEN 等待秒数         |
| `success_threshold`        | int  | `2`    | HALF_OPEN → CLOSED 所需连续成功数 |
| `max_recovery_seconds`     | int  | `3600` | 指数退避最大恢复时间（秒）        |

### 5.2 QuotaGuardConfig — 配额守卫参数

> **设计语义**：参见 [设计模式 -- QuotaGuard State Machine](./design-patterns.md#quota-guard)

| 字段                     | 类型  | 默认值  | 说明                                 |
| ------------------------ | ----- | ------- | ------------------------------------ |
| `enabled`                | bool  | `false` | 是否启用配额守卫                     |
| `token_budget`           | int   | `0`     | 滑动窗口内的 Token 预算上限          |
| `window_hours`           | float | `5.0`   | 滑动窗口大小（小时）                 |
| `threshold_percent`      | float | `99.0`  | 触发 QUOTA_EXCEEDED 的用量百分比阈值 |
| `probe_interval_seconds` | int   | `300`   | QUOTA_EXCEEDED 状态下探测间隔（秒）  |

### 5.3 RetryConfig — 重试策略参数

> **设计语义**：参见 [设计模式 -- Retry with Full Jitter](./design-patterns.md#retry)

| 字段                 | 类型  | 默认值 | 说明             |
| -------------------- | ----- | ------ | ---------------- |
| `max_retries`        | int   | `2`    | 最大重试次数     |
| `initial_delay_ms`   | int   | `500`  | 初始延迟（毫秒） |
| `max_delay_ms`       | int   | `5000` | 最大延迟（毫秒） |
| `backoff_multiplier` | float | `2.0`  | 退避倍数         |
| `jitter`             | bool  | `true` | 是否启用抖动     |

### 5.4 FailoverConfig — 故障转移参数

> **设计语义**：参见 [请求生命周期 -- 故障转移判定](../framework.md#fault-overhead)

| 字段                     | 类型      | 默认值                                                                             |
| ------------------------ | --------- | ---------------------------------------------------------------------------------- |
| `status_codes`           | list[int] | `[429, 403, 503, 500, 529]`                                                        |
| `error_types`            | list[str] | `["rate_limit_error", "overloaded_error", "api_error"]`                            |
| `error_message_patterns` | list[str] | `["quota", "limit exceeded", "usage cap", "capacity", "internal network failure"]` |

### 5.5 ZhipuConcurrencyConfig — Zhipu 每模型并发参数

仅对 `vendor: zhipu` 生效，基于 `asyncio.Semaphore` 实现 FIFO 公平排队。

| 字段      | 类型           | 默认值 | 说明                                                                             |
| --------- | -------------- | ------ | -------------------------------------------------------------------------------- |
| `default` | int            | `3`    | 全局默认并行度（适用于所有未在 `models` 中显式覆盖的模型）；取值范围 `[1, 20]`   |
| `models`  | map[str → int] | `{}`   | 按映射后模型名（如 `glm-5v-turbo` / `glm-5.1` / `glm-4.5-air`）自定义并行度上限 |

配置示例（Dashboard → Config 或 `config set vendors.zhipu.concurrency '{...}'`）：

```yaml
- vendor: zhipu
  concurrency:
    default: 3
    models:
      glm-5v-turbo: 5
      glm-5.1: 2
```

行为语义：

- 信号量按**映射后模型名**键控，与上游真实承载模型对齐；流式与非流式请求共用同一槽位。
- 槽位满时新请求按 FIFO 顺序排队，直到任一在途请求释放槽位才被唤醒。
- 429 重试期间持续占用槽位（重试视为同一请求的延续）。
- 顶层 `concurrency` 字段缺省为 `None` → 转发至 `ZhipuConfig` 时回退默认值 `default=3`；如需完全关闭限流，可在 `ZhipuConfig` 构造层显式置 `null`（一般无需操作）。

---

## 6. 供应商专属字段

### 6.1 Copilot 专属字段

| 字段                       | 类型 | 默认值          | 说明                                               |
| -------------------------- | ---- | --------------- | -------------------------------------------------- |
| `github_token`             | str  | `""`            | GitHub OAuth token / PAT（支持 `${ENV_VAR}`）      |
| `account_type`             | str  | `"individual"`  | 账号类型：`individual` / `business` / `enterprise` |
| `token_url`                | str  | `"https://..."` | Token 交换端点                                     |
| `models_cache_ttl_seconds` | int  | `300`           | 模型列表缓存 TTL                                   |

### 6.2 Antigravity 专属字段

| 字段             | 类型 | 默认值                              | 说明                        |
| ---------------- | ---- | ----------------------------------- | --------------------------- |
| `client_id`      | str  | `""`                                | Google OAuth2 Client ID     |
| `client_secret`  | str  | `""`                                | Google OAuth2 Client Secret |
| `refresh_token`  | str  | `""`                                | Google OAuth2 Refresh Token |
| `model_endpoint` | str  | `"models/claude-sonnet-4-20250514"` | Gemini 模型端点路径         |

### 6.3 原生 Anthropic 兼容供应商共用字段

适用于 `zhipu` / `minimax` / `kimi` / `doubao` / `xiaomi` / `alibaba`。

| 字段      | 类型 | 默认值 | 说明                         |
| --------- | ---- | ------ | ---------------------------- |
| `api_key` | str  | `""`   | API Key（支持 `${ENV_VAR}`） |

---

## 7. 模型映射规则

### 7.1 ModelMappingRule 字段

| 字段       | 类型      | 说明                                       |
| ---------- | --------- | ------------------------------------------ |
| `pattern`  | str       | 匹配模式（精确/通配符/正则）               |
| `target`   | str       | 目标模型名称                               |
| `is_regex` | bool      | 是否为正则表达式（默认 `false`）           |
| `vendors`  | list[str] | 规则作用域（留空应用于所有原生兼容供应商） |

### 7.2 ModelPricingEntry 字段

| 字段                        | 类型  | 说明                                               |
| --------------------------- | ----- | -------------------------------------------------- |
| `vendor`                    | str   | 供应商名称                                         |
| `model`                     | str   | 实际模型名                                         |
| `input_cost_per_mtok`       | float | 输入 Token 单价（$/百万 token，支持 `$`/`¥` 前缀） |
| `output_cost_per_mtok`      | float | 输出 Token 单价                                    |
| `cache_write_cost_per_mtok` | float | 缓存创建 Token 单价                                |
| `cache_read_cost_per_mtok`  | float | 缓存读取 Token 单价                                |

---

## 8. tiers — 显式优先级

| 字段    | 类型                     | 说明                                       |
| ------- | ------------------------ | ------------------------------------------ |
| `tiers` | list[VendorType] \| None | 降级链路优先级（None 时回退 vendors 顺序） |

---

## 9. vendors 列表格式

`vendors` 以列表形式定义所有供应商，每个 vendor 自包含其弹性配置（Dashboard → Config 的 Vendors 分区或 `config set vendors` 以同一结构读写；`config export` 导出的也是该格式）：

```yaml
vendors:
  - vendor: anthropic
    enabled: true
    base_url: https://api.anthropic.com
    timeout_ms: 300000
    circuit_breaker:
      failure_threshold: 3
      recovery_timeout_seconds: 300
    quota_guard:
      enabled: false
    weekly_quota_guard:
      enabled: false

  - vendor: copilot
    enabled: false
    # 密钥字段在导出中只会是 ${VAR} 或 <redacted>；实际值经 config set-secret / Dashboard 写入
    account_type: individual
    circuit_breaker:
      failure_threshold: 3
    quota_guard:
      enabled: true
      token_budget: 3000000
      window_hours: 4.0

  - vendor: zhipu
    enabled: true
    # 无 circuit_breaker -> 不启用熔断；是否兜底由 tiers 中的位置决定

  - vendor: minimax
    enabled: false

tiers: [anthropic, copilot, zhipu]  # 显式优先级（可选）
```

### Legacy Flat 格式（仅导入兼容）

旧的 flat 格式字段（`primary`/`copilot`/`antigravity`/`fallback`/`circuit_breaker`/`*_quota_guard`）在一次性导入时经 `ProxyConfig._migrate_legacy_fields()` 自动迁移为 vendors 列表格式，随导入存入 config.db；此后不再出现在写入路径中。

迁移规则：
1. `anthropic` 字段名 → `primary`
2. `zhipu` 字段名 → `fallback`
3. 若无 `vendors` 字段，从 legacy flat 字段自动生成 vendors 列表
4. 迁移时发出 INFO 日志建议迁移至新格式

---

## 10. native_api — 原生 API 透传配置

> **定义位置**：[`native_api/config.py`](../../src/coding/proxy/native_api/config.py)
>
> **端点**：`/api/{openai,gemini,anthropic}/**` catch-all 透传通道；认证头由客户端自带，proxy 不保管凭据。

### 10.1 NativeApiConfig 顶层字段

| 字段          | 类型                   | 默认值                                 | 说明                                      |
| ------------- | ---------------------- | -------------------------------------- | ----------------------------------------- |
| `openai`      | `NativeProviderConfig` | 内置官方 URL，`enabled=true`           | OpenAI chat / responses / embeddings 等   |
| `gemini`      | `NativeProviderConfig` | 内置官方 URL，`enabled=true`           | Gemini generateContent / embedContent 等  |
| `anthropic`   | `NativeProviderConfig` | 内置官方 URL，`enabled=true`           | Anthropic messages / count_tokens / batches |

### 10.2 NativeProviderConfig 字段

| 字段                  | 类型 | 默认值              | 说明                                                                  |
| --------------------- | ---- | ------------------- | --------------------------------------------------------------------- |
| `enabled`             | bool | `true`              | 是否启用该 provider 的原生透传端点（默认启用，开箱即用）              |
| `base_url`            | str  | 见下方内置默认      | 上游 API base_url（纯域名前缀，不含 `/v1`）                           |
| `timeout_ms`          | int  | `300000`            | 单次请求超时（毫秒），LLM 大模型建议 ≥ 120s                           |
| `connect_timeout_ms`  | int  | `15000`             | 连接建立超时（毫秒）                                                  |
| `inject_include_usage` | bool | `false`            | （仅 openai 生效）为 chat/completions 流式请求注入 `stream_options.include_usage=true`，使上游末尾回发 usage chunk（否则流式 tokens 记 0）；注入会改写请求体，默认关闭保持严格字节级透传 |

### 10.3 内置默认 `base_url`

| Provider     | 内置默认                                              |
| ------------ | ----------------------------------------------------- |
| `openai`     | `https://api.openai.com`                              |
| `gemini`     | `https://generativelanguage.googleapis.com`           |
| `anthropic`  | `https://api.anthropic.com`                           |

### 10.4 `base_url` 三级覆写优先级

由 `NativeApiConfig._apply_env_overrides` `@model_validator(mode="after")` 注入：

```text
env var（运行时） > 存储的显式字段（Dashboard / CLI 配置） > Pydantic 内置默认（兜底）
```

| 环境变量                     | 覆写目标                  |
| ---------------------------- | ------------------------- |
| `NATIVE_OPENAI_BASE_URL`     | `openai.base_url`         |
| `NATIVE_GEMINI_BASE_URL`     | `gemini.base_url`         |
| `NATIVE_ANTHROPIC_BASE_URL`  | `anthropic.base_url`      |

空串或纯空白视作未设置，保留上一层值（避免"未设置 env → 空串覆盖内置默认"陷阱）。

> `ANTHROPIC_BASE_URL`（client → proxy，Claude Code 使用）与 `NATIVE_ANTHROPIC_BASE_URL`（proxy → upstream，原生透传使用）方向正交，勿混用。参见 [用户指引 § 4.7 native_api](../user-guide.md#47-native_api--原生-api-透传)。

## 11. session_policies — Session 路由策略与会话级模型绑定

> **定义位置**：[`config/session_policy.py`](../../src/coding/proxy/config/session_policy.py)
>
> **生效链路**：`SessionPolicyResolver` 逐请求按 session_key 解析。运行时绑定（Dashboard/CLI/API）纯内存态：重启后回落配置态；配置热切换（generation swap）经 `_migrate_bindings` 迁移保留——vendor 需仍在新链路、模型需仍在 `bindable_models` 清单，失效部分移除并在 `reset_runtime` 提示。设计详见 [session-model-bind.md](session-model-bind.md)。

### 11.1 本节字段

| 字段 | 类型 | 默认值 | 说明 |
| --- | --- | --- | --- |
| `policies` | `list[SessionPolicy]` | `[]` | 配置态 Session 路由策略，按序求值首次命中生效（存量键：`name`/`match.session_keys`/`tiers`，注释见 `config.default.yaml`） |
| `policies.*.model` | `str \| null` | `null` | 会话级模型覆盖：非空时该 Session 的后续请求体 `model` 统一改写为此值（逐请求生效，在途请求不受影响） |
| `bindable_models` | `list[BindableModel]` | claude 三档 | 可绑定模型清单，Dashboard Model Bind 下拉选项与 API 校验的唯一来源 |
| `bindable_models.*.label` | `str` | — | UI 显示名，清单内唯一，非空 |
| `bindable_models.*.model` | `str` | — | 覆盖目标模型名，清单内唯一，非空 |
| `bindable_models.*.tiers` | `list[str]` | `[]` | 随绑定重排的 vendor 优先级；空 = 不改当前 tier 顺序（已有运行时 vendor 绑定 > 配置态策略 > 全局默认） |
| `title_vendor_bindings` / `title_exempt_prefixes` | — | — | 标题前缀自动绑定与豁免（存量键，语义见 `config.default.yaml` 注释） |

### 11.2 跨系列约束

原生模型名（如 `glm-5.3`）入清单须配套 identity `model_mapping` 规则（pattern = target），防 `ModelMapper` default 静默改写；详见 [session-model-bind.md §6](session-model-bind.md#6-已知边界与风险)。

## 12. antigravity_gateway — Antigravity 客户端透传配置

> **定义位置**：[`antigravity_gateway/config.py`](../../src/coding/proxy/antigravity_gateway/config.py)
>
> **端点**：`/api/antigravity/**` catch-all 透传通道。Antigravity IDE（默认 Gemini agent）经 `CLOUD_CODE_URL` 环境变量将模型流指向本 proxy，proxy 字节级透传至 Google Cloud Code `v1internal` 后端并旁路解析信封完成用量统计（`client_category='antigravity'`，vendor 标签 `antigravity-gateway`）。认证由 IDE 自带，proxy 不保管凭据。

### 12.1 AntigravityGatewayConfig 字段

| 字段                 | 类型 | 默认值                                          | 说明                                                                  |
| -------------------- | ---- | ----------------------------------------------- | --------------------------------------------------------------------- |
| `enabled`            | bool | `true`                                          | 是否启用 `/api/antigravity/**` 端点（默认启用，开箱即用）            |
| `base_url`           | str  | `https://daily-cloudcode-pa.googleapis.com`     | Antigravity 后端（Cloud Code v1internal）base_url                    |
| `timeout_ms`         | int  | `300000`                                        | 单次请求超时（毫秒）                                                  |
| `connect_timeout_ms` | int  | `15000`                                         | 连接建立超时（毫秒）                                                  |

### 12.2 接入方式与上游覆写

```bash
# Antigravity 侧（未文档化 env hook，桌面端与 agy CLI 均生效）：
export CLOUD_CODE_URL="http://127.0.0.1:3392/api/antigravity"
```

上游域名随 Antigravity 版本漂移过（`cloudcode-pa` → `daily-cloudcode-pa`）。若 IDE 报 404/400，可设环境变量 `ANTIGRAVITY_GATEWAY_BASE_URL` 切换（优先级 env > YAML > 内置默认，空串视作未设置）。

> 完整调研背景（三条接入通道裁决矩阵）参见 [Antigravity 客户端接入可行性调研](../research/antigravity-client-feasibility.md)。
