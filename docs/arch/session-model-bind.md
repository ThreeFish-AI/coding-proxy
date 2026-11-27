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

- **vendor.model 锁定**：绑定内置目录模型（如 `glm-5.3`）时为严格链——候选 tier 恰为该条目锁定的 vendor（用户显式点名多 vendor 时按点名顺序，均不尾部追加），绑定模型只发给原生支持它的 vendor，不做跨系列静默降级；对应 vendor 全体不可用时请求显式失败。custom 条目（tiers 留空）沿用「置头 + 垫尾」重排语义，并受模型门控保护（`supports_model` 不支持时 skip 并记因）。
- **failover 为 best-effort（custom 条目）**：绑定的 tier 被熔断 / quota / 能力门控跳过时，后续 tier 按自身映射接单，实际 served 模型可能偏离绑定模型。`_log_model_drift` 在两种情形打 WARNING：served 既非绑定名、也非接单 vendor 映射预期（failover 接单 / 上游换名），或绑定名命中映射 default 回退被改写（缺显式规则）；配置内等价映射（如 claude 档位名 → glm 档位名）属预期行为不告警。dashboard Models 列展示真实 served 模型。
- **运行时绑定纯内存态**：重启后回落默认路由（与 Vendor Bind 现状一致）；配置热切换（generation swap）时绑定经迁移保留——vendor 需仍在新链路、模型需仍在合成目录内，失效部分移除并在 `reset_runtime` 提示。标题前缀自动绑定（`title_vendor_bindings`）与手动绑定共用同一存储。
- **`count_tokens` 不同步覆盖**：透传路径非会话感知，token 估算可能与实际对话模型存在偏差（Claude Code 对其精度不敏感）。

## 3. 可绑定目录与配置（[config-reference.md](config-reference.md) 为参数权威）

下拉选项 = **内置 vendor 目录 ∪ pricing 行（仅 native vendor）∪ `bindable_models` 显式条目**（三者均按激活 vendor 过滤），合成于 app 构建时（[model_catalog.py](../../src/coding/proxy/vendors/model_catalog.py)）：

```yaml
session_policies:
  # 可绑定模型显式定制层；空清单 = 整体关闭模型绑定（含内置目录）
  bindable_models:
    - label: "Opus 4.6"          # UI 显示名（唯一）
      model: "claude-opus-4-6"   # 覆盖目标模型名（唯一）
      tiers: []                  # 留空继承内置目录锁定值；非空覆盖
      exclude: false             # true = 剔除同名内置条目
```

- 合成规则：按 `model` 去重；config 同名条目覆盖 `label`/`tiers`（`tiers` 留空继承内置锁定值），`exclude: true` 剔除内置同名条目；纯自定义条目原样保留（`vendor: custom`，tiers 留空 = 沿用全局链路顺序）。
- **pricing 派生条目**：native 系 vendor（zhipu/minimax/kimi/doubao/xiaomi/alibaba）的已配价模型即时进目录（label = model 名，tiers 锁定该 vendor），填补内置目录的发版滞后；与内置/config 同名时让位。
- **下线（`pricing.*.online: false`）**：模型带 `offline` 标记——不进下拉，`PUT /api/session-vendor` 拒绝新绑（400，文案指明 offline）；executor 锁定链与热切换迁移消费含下线的全量清单（`get_known_models()`），已绑会话保留并继续按锁定 vendor 路由（Sessions 页显示 `(off-list)`）；成本照常计价，identity 规则保留。
- **pricing 行不可删除**：Config 页与 `PUT /api/config`、CLI `config set` / `config reset` 对任何会使 (vendor, model) 行消失的保存一律拒绝（错误只带条数不回显值；`config import` 整体替换除外）。Config 页已保存行以「下线/上线」替代删除，vendor/model 只读，「恢复默认价格」保留自定义行。
- **identity 映射规则自动合成**：合成目录中 native 系 vendor（zhipu/minimax/kimi/doubao/xiaomi/alibaba）的模型在 app 构建时自动获得 `pattern=target=model` 规则（custom 条目按锁定 tiers 或原生前缀族归属），无需手工配置，杜绝 ModelMapper default 回退静默改写；已被用户显式映射规则（exact/通配/正则）覆盖的模型跳过合成，用户规则优先。
- 未知名（不在任何原生前缀族，如自定义微调模型名）绑定到 native 系 vendor 需配套显式 `model_mapping` 规则，否则门控不放行（native 系对未知名的 default 回退是静默改写而非透传，不放行是刻意防错）；anthropic 真透传 vendor 对未知名仍放行。
- 合成目录外的模型无法通过 API 绑定（PUT 校验返回 400）。
- 内置目录是发版节奏的静态快照：native vendor 的新模型配价（pricing 行）即入下拉，或经自定义条目即时自救（自动获得 identity 规则）。

## 4. API 与 CLI

- `PUT /api/session-vendor`：body `{session_key, vendors?: [..], model?: "..."}`，至少一项；`model` 必须在合成目录（内置 ∪ pricing ∪ config）内且未下线（下线模型返回 400，文案指明 offline）；`vendors` 省略时沿用目录 entry 的 `tiers`。
- `GET /api/session-vendor`：bindings 增返 `model` 字段（无模型覆盖时为 `null`）。
- `DELETE /api/session-vendor/{session_key}`：清除整条运行时绑定（model + tiers 一并移除，恢复配置态策略备份）。
- `GET /api/status`：增返 `bindable_models` 数组（UI 选项源）。
- CLI：`coding-proxy session bind --key <k> --model <m>`（`--vendor` 语义不变，二者可组合）。

## 5. 实现锚点

| 模块 | 位置 | 变更 |
| --- | --- | --- |
| 模型目录 | `vendors/model_catalog.py` | 内置 vendor 目录常量 + `compose_bindable_catalog`（内置 ∪ config，exclude/覆盖/去重）+ `builtin_identity_rules`（自动 identity 规则）+ 原生前缀族 |
| 配置模型 | `config/session_policy.py` | `SessionPolicy.model` 可选字段；`BindableModel`（label/model/tiers/exclude）与 `bindable_models` 校验（label/model 唯一；空清单 = 关闭） |
| 解析引擎 | `routing/session_policy.py` | `upsert(..., model=None)`、`get_runtime_binding()`、`list_runtime_bindings()` 增返 model |
| 执行器 | `routing/executor.py` | 两入口注入模型覆盖（shadow copy）；`_pinned_tier_names` 严格链（锁定条目不垫尾）；`_try_gate_tier` 模型门控（`supports_model` 不支持时 skip）；failover / map 漂移 WARNING；`_apply_title_based_policy` 遇已有运行时绑定即跳过 |
| 路由 | `server/routes.py` | PUT 增 `model` 校验（合成目录）；GET/status 增返字段（含 vendor 归属） |
| Vendor 契约 | `vendors/base.py` + `antigravity.py`、`native_anthropic.py`、`copilot.py` | `BaseVendor.map_model_with_reason` 扩展点（默认恒等 + "vendor"）；`supports_model` 门控判定（前缀族 + exact/pattern 命中） |
| UI | `server/dashboard.py` | Vendor Bind 列 → Model Bind；选项来自合成目录并按 vendor optgroup 分组；Default = 清除整条绑定；vendor-only 绑定（CLI/旧 API 建立）以 `Default (vendor: …)` 标注可见 |
| CLI | `cli/__init__.py` | `session bind --model`、`session list` 显示 model |

## 6. 已知边界与风险

| 风险 | 处置 |
| --- | --- |
| ModelMapper default 静默改写原生模型名 | 目录内模型自动获得 identity 规则（构造期合成）；目录外自定义条目按 tiers/前缀族兜底合成 + executor map 漂移 WARNING |
| 内置目录滞后于上游新模型 | 发版补目录为补给线；native vendor 新模型配价（pricing 行）即入目录；自定义条目即时自救（自动 identity 规则）；identity 透传保证「发什么是什么」，无静默错路由 |
| 删除 pricing 行导致已绑会话在热切换时被丢弃 | pricing 行不可删除（服务端 `_plan` 校验 + Config 页去删除），退役走 `online: false` 下线：下拉隐藏但目录保留，已绑会话继续路由 |
| 绑定 vendor.model 后该 vendor 全体不可用 | 严格链显式失败（可用性换确定性，流式/非流式同型 `NoCompatibleVendorError`、文案指向锁定链真因）；custom 条目（tiers 空）不受影响 |
| 标题自动绑定覆盖手动绑定（compat TTL 24h 过期重触发） | `_apply_title_based_policy` 先查 `get_runtime_binding`，存在即跳过 |
| 跨供应商 thinking signature / cache_control 残留 | `VENDOR_TRANSITIONS` 通道按方向注册；无通道方向原样透传，可能上游 400 → 走 semantic failover；跨系列放开前需补通道并实测 |
| 跨模型切换 prompt cache 失效（成本跳变） | 预期行为：切换即重建缓存，dashboard 成本列天然反映 |
| context window 差异无预检 | 上游报错走 failover 链，全部失败则该轮报错（会话不丢，可重试） |
| 绑定重启丢失 | 内存态现状（热切换经迁移保留，见 §2）；持久化属独立增强 |

## 7. 测试锚点

- `tests/test_model_catalog.py`：目录合成矩阵（激活过滤/覆盖/exclude/去重/label 冲突降级/空清单关闭）、pricing 派生（native 过滤/激活过滤/去重/下线标记/identity 规则）、identity 规则合成（内置/custom/前缀族兜底/用户规则优先）、`supports_model` 判定矩阵、executor 严格链与模型门控。
- `tests/test_session_aware.py`：resolver model 语义（upsert / get_runtime_binding / list）、空 tiers 继承配置策略顺序、`bindable_models` 配置校验（唯一性 / 非空）。
- `tests/test_router_executor.py`：模型覆盖注入（shadow copy、绑定按原 session_key 命中、客户端原始模型名留存于 INFO 日志）、在途请求不受绑定变更影响（流式 / 非流式）、failover 接单与映射改写的漂移告警、配置策略携带 model、标题自动绑定避让。
- `tests/test_app_routes.py`：`PUT/DELETE/GET /api/session-vendor` 路由级校验（含 model 分支、合成目录、下线模型拒绑、清单为空、vendors 非列表）、`/api/status` 的 `bindable_models`（下线模型过滤）与热切换迁移保留已绑下线模型。
- `tests/test_config_service.py`：pricing 行不可删除（删除 / 改名 / 恢复默认丢行拒绝，新增与 `online` 开关放行，错误不回显值）。
- `tests/test_dashboard_config_tab.py`：Config 页 Pricing 表（已保存行无删除、vendor/model 只读、下线/上线、恢复默认价格保留自定义行）。
- `tests/test_config_loader.py`：默认清单来自 `config.default.yaml` 的加载守护。
- `tests/test_cli_usage.py`：`session bind --model/--vendor` payload 与缺参退出码。
