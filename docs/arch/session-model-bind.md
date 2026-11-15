# Session Model Bind（会话级模型绑定）

> Dashboard Sessions 页「Model Bind」的设计与实现锚点：对运行中的 Claude Code 会话，在不打断任务执行的前提下切换实际所用模型。

## 1. 动机与可行性结论

原「Vendor Bind」只能把会话钉在某个供应商上，模型仍由客户端请求体决定。Model Bind 在此之上增加**会话级模型覆盖**：同一会话的后续请求统一改写为目标模型，跨系列切换（如 `claude-sonnet-4.6` → `glm-5.3`）亦支持。

可行性依据（隔离实例 + mock 上游实验，15/15 断言通过）：

- **绑定逐请求生效**：每个请求开始时 resolve 一次会话策略（[executor.py](../../src/coding/proxy/routing/executor.py) `execute_stream` / `execute_message` 入口），请求内 failover 沿用捕获值 → 改绑定对在途请求零影响、下一请求即时生效。
- **模型逐请求声明**：客户端每次请求携带 `model` 字段，各 vendor 的 `map_model` 逐请求解析 → 改写 `body.model` 即换 served model，无需会话重建。
- **响应透传无纠偏**：响应 `model` 字段原样回传，客户端不因「响应模型 ≠ 请求模型」中断会话。

## 2. 数据流（单请求）

```
/v1/messages
  → build_canonical_request（session_key 派生，compat/canonical.py）
  → resolve 会话策略（SessionPolicyResolver，锁内读内存索引）
  → [Model Bind] policy.model 非空 → body = {**body, "model": policy.model}（shadow copy）
  → [Vendor Bind] policy.tiers 重排 tier 顺序（未提及的 vendor 保尾追加）
  → tier 循环：能力门控（model 无关）→ VENDOR_TRANSITIONS 转换通道 → vendor.map_model → 上游
```

关键语义：

- **failover 为 best-effort**：绑定的 tier 被熔断 / quota / 能力门控跳过时，后续 tier 按自身映射接单，实际 served 模型可能偏离绑定模型；偏离时打 WARNING 日志（tier 漂移与 map 漂移两类），dashboard Models 列展示真实 served 模型。
- **运行时绑定纯内存态**：重启后回落默认路由（与 Vendor Bind 现状一致）；标题前缀自动绑定（`title_vendor_bindings`）与手动绑定共用同一存储。
- **`count_tokens` 不同步覆盖**：透传路径非会话感知，token 估算可能与实际对话模型存在偏差（Claude Code 对其精度不敏感）。

## 3. 配置（[config-reference.md](config-reference.md) 为参数权威）

```yaml
session_policies:
  # 可绑定模型清单 —— Model Bind 下拉选项的唯一来源
  bindable_models:
    - label: "Opus 4.6"          # UI 显示名（唯一）
      model: "claude-opus-4.6"   # 覆盖目标模型名（唯一）
      tiers: []                  # 可选：随绑定重排的 vendor 优先级（空 = 不改顺序）
```

- 清单外的模型无法通过 API 绑定（PUT 校验返回 400）。
- **跨系列原生模型名（如 `glm-5.3`）必须配套 identity `model_mapping` 规则**（pattern = target = `glm-5.3`），否则 `ModelMapper` 对未匹配名回退 default 目标（如 `glm-5.1`），绑定会被静默改写。
- 默认清单为 claude 家族三档，已有映射规则全覆盖，零额外配置。

## 4. API 与 CLI

- `PUT /api/session-vendor`：body `{session_key, vendors?: [..], model?: "..."}`，至少一项；`model` 必须在 `bindable_models` 清单内；`vendors` 省略时沿用清单 entry 的 `tiers`。
- `GET /api/session-vendor`：bindings 增返 `model` 字段（无模型覆盖时为 `null`）。
- `DELETE /api/session-vendor/{session_key}`：清除整条运行时绑定（model + tiers 一并移除，恢复配置态策略备份）。
- `GET /api/status`：增返 `bindable_models` 数组（UI 选项源）。
- CLI：`coding-proxy session bind --key <k> --model <m>`（`--vendor` 语义不变，二者可组合）。

## 5. 实现锚点

| 模块 | 位置 | 变更 |
| --- | --- | --- |
| 配置模型 | `config/session_policy.py` | `SessionPolicy.model` 可选字段；`BindableModel` 与 `bindable_models` 校验（label/model 唯一） |
| 解析引擎 | `routing/session_policy.py` | `upsert(..., model=None)`、`get_runtime_binding()`、`list_runtime_bindings()` 增返 model |
| 执行器 | `routing/executor.py` | 两入口注入模型覆盖（shadow copy）；failover / map 漂移 WARNING；`_apply_title_based_policy` 遇已有运行时绑定即跳过 |
| 路由 | `server/routes.py` | PUT 增 `model` 校验；GET/status 增返字段 |
| UI | `server/dashboard.py` | Vendor Bind 列 → Model Bind；选项来自 `bindable_models`；Default = 清除整条绑定 |
| CLI | `cli/__init__.py` | `session bind --model`、`session list` 显示 model |

## 6. 已知边界与风险

| 风险 | 处置 |
| --- | --- |
| ModelMapper default 静默改写原生模型名 | 清单配套 identity 规则（文档约束）+ executor map 漂移 WARNING |
| 标题自动绑定覆盖手动绑定（compat TTL 24h 过期重触发） | `_apply_title_based_policy` 先查 `get_runtime_binding`，存在即跳过 |
| 跨供应商 thinking signature / cache_control 残留 | `VENDOR_TRANSITIONS` 通道按方向注册；无通道方向原样透传，可能上游 400 → 走 semantic failover；跨系列放开前需补通道并实测 |
| 跨模型切换 prompt cache 失效（成本跳变） | 预期行为：切换即重建缓存，dashboard 成本列天然反映 |
| context window 差异无预检 | 上游报错走 failover 链，全部失败则该轮报错（会话不丢，可重试） |
| 绑定重启丢失 | 内存态现状；持久化属独立增强 |

## 7. 测试锚点

- `tests/test_session_aware.py`：resolver model 语义（upsert / get_runtime_binding / list）、空 tiers 继承配置策略顺序、`bindable_models` 配置校验（唯一性 / 非空）。
- `tests/test_router_executor.py`：模型覆盖注入（shadow copy、session_key 不变、原始客户端名留存日志）、在途请求不受绑定变更影响（流式 / 非流式）、failover 接单与映射改写的漂移告警、配置策略携带 model、标题自动绑定避让。
- `tests/test_app_routes.py`：`PUT/DELETE/GET /api/session-vendor` 路由级校验（含 model 分支、清单为空、vendors 非列表）与 `/api/status` 的 `bindable_models`。
- `tests/test_config_loader.py`：默认清单来自 `config.default.yaml` 的加载守护。
- `tests/test_cli_usage.py`：`session bind --model/--vendor` payload 与缺参退出码。
