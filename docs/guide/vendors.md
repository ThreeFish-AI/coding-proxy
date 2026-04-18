# 供应商配置

<details>
<summary><strong>📑 目录（点击展开）</strong></summary>

- [供应商配置](#供应商配置)
  - [1. 供应商分类](#1-供应商分类)
  - [2. 通用字段](#2-通用字段)
  - [3. 供应商配置详情](#3-供应商配置详情)
    - [3.1 anthropic — Anthropic Claude](#31-anthropic--anthropic-claude)
    - [3.2 copilot — GitHub Copilot](#32-copilot--github-copilot)
    - [3.3 antigravity — Google Antigravity](#33-antigravity--google-antigravity)
    - [3.4 zhipu — 智谱 GLM](#34-zhipu--智谱-glm)
    - [3.5 minimax — MiniMax](#35-minimax--minimax)
    - [3.6 alibaba — 阿里 Qwen](#36-alibaba--阿里-qwen)
    - [3.7 xiaomi — 小米 MiMo](#37-xiaomi--小米-mimo)
    - [3.8 kimi — Kimi](#38-kimi--kimi)
    - [3.9 doubao — 豆包 Doubao](#39-doubao--豆包-doubao)
  - [4. tiers — 降级链路优先级](#4-tiers--降级链路优先级)
  - [5. model\_mapping — 模型映射规则](#5-model_mapping--模型映射规则)
  - [6. pricing — 模型定价](#6-pricing--模型定价)

</details>

## 1. 供应商分类

coding-proxy 支持三类供应商，共 9 种：

| 类别                    | 供应商                                                    | 说明                                                     |
| ----------------------- | --------------------------------------------------------- | -------------------------------------------------------- |
| **直连 Anthropic**      | `anthropic`                                               | 直接透传请求到 Anthropic API                             |
| **协议转换**            | `copilot`、`antigravity`                                  | 内部完成认证交换与格式转换（Anthropic ↔ Gemini）         |
| **原生 Anthropic 兼容** | `zhipu`、`minimax`、`alibaba`、`xiaomi`、`kimi`、`doubao` | 提供原生 Anthropic 兼容端点，仅需 `api_key` + `base_url` |

> 类层次结构与设计细节参见 [架构文档 — 供应商模块](../arch/vendors.md)。

## 2. 通用字段

所有供应商共享以下字段。完整参数表参见 [配置字段参考 — 通用字段](../arch/config-reference.md#4-vendorconfig-通用字段)。

| 字段         | 类型   | 默认值             | 说明                                                                                                         |
| ------------ | ------ | ------------------ | ------------------------------------------------------------------------------------------------------------ |
| `vendor`     | string | —                  | 供应商类型标识                                                                                               |
| `enabled`    | bool   | `true`             | 是否启用                                                                                                     |
| `base_url`   | string | `""`               | API 基础 URL；留空使用供应商默认值                                                                           |
| `timeout_ms` | int    | `300000`/`3000000` | 请求超时（毫秒）；直连/协议转换供应商默认 300000（5 分钟），原生 Anthropic 兼容供应商默认 3000000（50 分钟） |

弹性设施字段（`circuit_breaker`、`quota_guard`、`weekly_quota_guard`、`retry`）参见 [配置字段参考 — 弹性字段](../arch/config-reference.md#5-vendorconfig-弹性字段)。

## 3. 供应商配置详情

### 3.1 anthropic — Anthropic Claude

直连 Anthropic API，无额外配置字段。OAuth token 由 Claude Code 透传。

```yaml
- vendor: anthropic
  base_url: "https://api.anthropic.com"
  timeout_ms: 300000
  circuit_breaker:
    failure_threshold: 3
    recovery_timeout_seconds: 300
    success_threshold: 2
  quota_guard:
    enabled: false
    token_budget: 65000000
    window_hours: 5.0
```

### 3.2 copilot — GitHub Copilot

通过 GitHub Copilot 内部 API 调用 Claude 模型。需 OAuth 登录或手动配置 token。

| 字段                       | 类型   | 默认值                                               | 说明                                               |
| -------------------------- | ------ | ---------------------------------------------------- | -------------------------------------------------- |
| `github_token`             | string | `""`                                                 | GitHub OAuth token / PAT，支持 `${ENV_VAR}`        |
| `account_type`             | string | `"individual"`                                       | 账号类型：`individual` / `business` / `enterprise` |
| `token_url`                | string | `"https://api.github.com/copilot_internal/v2/token"` | Token 交换端点                                     |
| `base_url`                 | string | `""`                                                 | 留空时按 `account_type` 自动解析                   |
| `models_cache_ttl_seconds` | int    | `300`                                                | 模型列表缓存 TTL（秒）                             |

> 默认已启用（`enabled: true`）。首次启动时若缺少有效凭证，自动触发 GitHub Device Flow 浏览器登录。
> 可通过 [`GET /api/copilot/diagnostics`](./api-reference.md#7-get-apicopilotdiagnostics) 和 [`GET /api/copilot/models`](./api-reference.md#8-get-apicopilotmodels) 排查认证状态。

### 3.3 antigravity — Google Antigravity

通过 Google Generative Language API 调用 Claude / Gemini 模型。代理自动处理 Anthropic ↔ Gemini 格式双向转换。

| 字段             | 类型   | 默认值                                               | 说明                                           |
| ---------------- | ------ | ---------------------------------------------------- | ---------------------------------------------- |
| `client_id`      | string | `""`                                                 | Google OAuth2 Client ID，支持 `${ENV_VAR}`     |
| `client_secret`  | string | `""`                                                 | Google OAuth2 Client Secret，支持 `${ENV_VAR}` |
| `refresh_token`  | string | `""`                                                 | Google OAuth2 Refresh Token，支持 `${ENV_VAR}` |
| `base_url`       | string | `"https://generativelanguage.googleapis.com/v1beta"` | Gemini API 基础地址                            |
| `model_endpoint` | string | `"models/claude-sonnet-4-20250514"`                  | 模型端点路径（仅作为未命中映射时的默认模型）   |

> 默认禁用（`enabled: false`）。启用需配置 OAuth 凭据，启动时自动触发 Google OAuth 登录。access_token 过期时优先静默刷新，无需重新登录。

### 3.4 zhipu — 智谱 GLM

通过智谱 GLM 官方 Anthropic 兼容端点调用模型。

| 字段       | 类型   | 默认值                                     | 说明                            |
| ---------- | ------ | ------------------------------------------ | ------------------------------- |
| `api_key`  | string | `""`                                       | 智谱 API Key，支持 `${ENV_VAR}` |
| `base_url` | string | `"https://open.bigmodel.cn/api/anthropic"` | API 基础地址                    |

> 默认已启用（`enabled: true`），且配置了 `circuit_breaker`（`recovery_timeout_seconds: 30`）和 `quota_guard`（`enabled: true`），属于中间层参与故障转移。默认作为 `tiers` 链路中的最高优先级（Tier 0）。

### 3.5 minimax — MiniMax

通过 MiniMax 原生 Anthropic 兼容端点调用模型。

| 字段       | 类型   | 默认值                                 | 说明                                                       |
| ---------- | ------ | -------------------------------------- | ---------------------------------------------------------- |
| `api_key`  | string | `""`                                   | MiniMax API Key，支持 `${ENV_VAR}`（`${MINIMAX_API_KEY}`） |
| `base_url` | string | `"https://api.minimaxi.com/anthropic"` | API 基础地址                                               |

> 默认禁用（`enabled: false`）。默认模型映射：`minimax-m2.7`。

### 3.6 alibaba — 阿里 Qwen

通过阿里云 DashScope Anthropic 兼容端点调用 Qwen 模型。

| 字段       | 类型   | 默认值                                                        | 说明                                                    |
| ---------- | ------ | ------------------------------------------------------------- | ------------------------------------------------------- |
| `api_key`  | string | `""`                                                          | 阿里 API Key，支持 `${ENV_VAR}`（`${ALIBABA_API_KEY}`） |
| `base_url` | string | `"https://coding-intl.dashscope.aliyuncs.com/apps/anthropic"` | API 基础地址                                            |

> 默认禁用（`enabled: false`）。默认模型映射：`qwen3.6-plus`。

### 3.7 xiaomi — 小米 MiMo

通过小米 Anthropic 兼容端点调用 MiMo 模型。

| 字段       | 类型   | 默认值                                             | 说明                                                   |
| ---------- | ------ | -------------------------------------------------- | ------------------------------------------------------ |
| `api_key`  | string | `""`                                               | 小米 API Key，支持 `${ENV_VAR}`（`${XIAOMI_API_KEY}`） |
| `base_url` | string | `"https://token-plan-cn.xiaomimimo.com/anthropic"` | API 基础地址                                           |

> 默认禁用（`enabled: false`）。默认模型映射：`mimo-v2-pro`。

### 3.8 kimi — Kimi

通过 Kimi Anthropic 兼容端点调用模型。

| 字段       | 类型   | 默认值                           | 说明                                                 |
| ---------- | ------ | -------------------------------- | ---------------------------------------------------- |
| `api_key`  | string | `""`                             | Kimi API Key，支持 `${ENV_VAR}`（`${KIMI_API_KEY}`） |
| `base_url` | string | `"https://api.kimi.com/coding/"` | API 基础地址                                         |

> 默认禁用（`enabled: false`）。默认模型映射：`kimi-k2.5`。

### 3.9 doubao — 豆包 Doubao

通过字节跳动 Volcengine Anthropic 兼容端点调用模型。

| 字段       | 类型   | 默认值                                           | 说明                                                      |
| ---------- | ------ | ------------------------------------------------ | --------------------------------------------------------- |
| `api_key`  | string | `""`                                             | Volcengine API Key，支持 `${ENV_VAR}`（`${ARK_API_KEY}`） |
| `base_url` | string | `"https://ark.cn-beijing.volces.com/api/coding"` | API 基础地址                                              |

> 默认禁用（`enabled: false`）。三档模型映射：`claude-opus-*` → `doubao-seed-2.0-code`，`claude-sonnet-*` → `doubao-seed-2.0-pro`，`claude-haiku-*` → `doubao-seed-2.0-lite`。

## 4. tiers — 降级链路优先级

可选字段，显式指定故障转移时的供应商尝试顺序。

```yaml
tiers: ["zhipu", "anthropic", "copilot", "antigravity"]
```

**规则**：
- 未配置时回退到 `vendors` 列表原始顺序
- 引用的 vendor 名称必须在 `vendors` 中定义且 `enabled=true`
- 可用于在不改变 `vendors` 定义顺序的情况下灵活调整降级策略

**示例**：

| 场景           | 配置                                                          | 效果                       |
| -------------- | ------------------------------------------------------------- | -------------------------- |
| 默认链路       | `["zhipu", "anthropic", "copilot", "antigravity"]`            | 智谱首选，Anthropic 作后备 |
| Anthropic 首选 | `["anthropic", "zhipu"]`                                      | 仅保留首尾两级             |
| 全部启用       | `["zhipu", "anthropic", "copilot", "minimax", "antigravity"]` | 五层降级                   |

> **终端层**：未配置 `circuit_breaker` 的 vendor 自动成为终端层（始终接受请求，不触发向下故障转移）。`config.default.yaml` 中所有已启用 vendor 均配置了 `circuit_breaker`，因此默认无终端层；若需设置终端层，移除对应 vendor 的 `circuit_breaker` 配置即可。
>
> **故障转移触发条件**：详见 [配置字段参考 — FailoverConfig](../arch/config-reference.md#54-failoverconfig--故障转移参数)。

## 5. model_mapping — 模型映射规则

将 Claude 模型名自动转换为各供应商对应的实际模型名。完整规则列表参见项目内置的 `config.default.yaml`（`src/coding/proxy/config/config.default.yaml`）。

| 字段       | 类型      | 说明                             |
| ---------- | --------- | -------------------------------- |
| `pattern`  | string    | 匹配模式（精确匹配或正则）       |
| `vendors`  | list[str] | 规则作用的供应商范围             |
| `target`   | string    | 目标模型名称                     |
| `is_regex` | bool      | 是否为正则表达式（默认 `false`） |

**匹配优先级**：同一供应商内精确匹配 > 正则匹配（按规则顺序） > 供应商默认值。

**典型配置**：

```yaml
model_mapping:
  # GitHub Copilot
  - pattern: "claude-sonnet-.*"
    vendors: ["copilot"]
    target: "claude-sonnet-4.6"
    is_regex: true
  # 智谱 GLM
  - pattern: "claude-sonnet-.*"
    vendors: ["zhipu"]
    target: "glm-5v-turbo"
    is_regex: true
  # 豆包 Doubao（三档模型）
  - pattern: "claude-opus-.*"
    vendors: ["doubao"]
    target: "doubao-seed-2.0-code"
    is_regex: true
  - pattern: "claude-sonnet-.*"
    vendors: ["doubao"]
    target: "doubao-seed-2.0-pro"
    is_regex: true
  - pattern: "claude-haiku-.*"
    vendors: ["doubao"]
    target: "doubao-seed-2.0-lite"
    is_regex: true
```

> **兼容规则**：未设置 `vendors` 的历史规则默认只作用于原生 Anthropic 兼容供应商，避免映射误套。

## 6. pricing — 模型定价

按 `(vendor, model)` 配置四维定价，用于 [`coding-proxy usage`](./cli-reference.md#3-coding-proxy-usage) 的费用统计展示。完整定价表参见项目内置的 `config.default.yaml`（`src/coding/proxy/config/config.default.yaml`）。

| 字段                        | 类型   | 说明                                      |
| --------------------------- | ------ | ----------------------------------------- |
| `vendor`                    | string | 供应商名称                                |
| `model`                     | string | 实际模型名                                |
| `input_cost_per_mtok`       | price  | 输入 Token 单价（支持 `$` USD / `¥` CNY） |
| `output_cost_per_mtok`      | price  | 输出 Token 单价                           |
| `cache_write_cost_per_mtok` | price  | 缓存创建 Token 单价                       |
| `cache_read_cost_per_mtok`  | price  | 缓存读取 Token 单价                       |

> **币种规则**：同一模型的所有价格必须使用相同币种。未配置定价的模型在 `usage` 统计中 Cost 列显示 `-`。
