# 订阅状态面板（Subscription Status）

> Overview 页「🎫 订阅状态」卡片：聚合 **Claude Code / GLM / Antigravity** 三家订阅的档位与用量，并承载各供应商的路由运行状态（熔断 / 配额守卫 / 限速），同一账号合并为一行。三家上游端点均未正式文档化，解析一律宽容式（缺字段即跳过），单家失败不影响其他家。

## 数据流

```
Dashboard Overview ── GET /api/dashboard/subscriptions
                          │
                          ▼
              SubscriptionStatusService（TTL 120s 缓存 + per-vendor 锁 + 错误隔离）
              ┌─────────────┬──────────────────────┬─────────────────────────┐
              │ claude      │ glm                  │ antigravity             │
              │ 被动快照    │ ZhipuUsageFetcher    │ AntigravityUsageFetcher │
              │ (内存单例)  │ (api_key 主动查询)   │ (OAuth token 主动查询)  │
              └─────────────┴──────────────────────┴─────────────────────────┘
                     │                 │                        │
        透传链路 unified 头   open.bigmodel.cn       cloudcode-pa / daily-cloudcode-pa
        旁路采集(零凭证)     /api/monitor/...        v1internal:loadCodeAssist
                                                + retrieveUserQuotaSummary
```

## 卡片行模型

卡片是纯前端渲染：订阅数据（`GET /api/dashboard/subscriptions`）与路由 tier 数据（`GET /api/status` 的 `tiers`）并发拉取后，在 `_renderStatusRows` 里做 full outer join。两个端点与响应形状互不耦合，订阅数据不并入 `/api/status`（后者被 Model Calling 面板 10 秒轮询，而订阅拉取带上游超时）。

- 归并键：`SUB_META[key].tier`，`claude → anthropic`、`glm → zhipu`、`antigravity → antigravity`。`tier.name` 恒等于 vendor 类型字面量且运行时同类型至多一个 tier，按名归并不会串行；取值必须是 [config/routing.py](../../src/coding/proxy/config/routing.py) 的 `VendorType` 之一，由测试钉死；
- 三类行：合并行（订阅 × tier，行名取订阅名；路由区只有该 vendor 的可用状态，tier 名只进悬停详情）、供应商行（无订阅对应，路由区同样只有可用状态；头像首字母与订阅行头像撞字母时取前两个字母，如 copilot → `Co`，见 `_tierInitial`）、订阅行（无对应 tier，行尾灰字「未加入路由链」，没有路由区）；
- 渲染语法：订阅区是 `[灰字标签] [状态胶囊] [用量格]*`，路由区是 `[可用状态]`。胶囊只承载状态（订阅区：等待 / 未启用 / 查询失败；路由区：熔断 / 半开 / 限速中 / 配额超限）；档位是行名后的加粗亮字，档位名与行名相同时不重复显示（`_planLabel`，如 Antigravity）；用量格 `renderQuotaCell` = 窗口短标签 + 40px 进度条 + 百分比，订阅窗口再缀紧凑重置倒计时（`fmtResetShort`，完整写法 `fmtResetCountdown` 进 Tooltip），严重度类名 `quotaClass` 同时决定格底色与进度条填充色。订阅窗口的短标签取自 `window`（`5h` / `weekly` → `5h` / `周`），认不出的类别回退 `label`；配额守卫窗口按 `window_hours` 折算（`quotaWindowLabel`），只出现在路由区悬停详情里。类别表与分组表都是 `Map`：上游给到 `constructor`、`__proto__` 一类的值时不会命中继承成员而让整卡渲染抛错；
- 可用状态（`_tierZoneHTML`）：路由区只回答该行 vendor 现在能不能接请求，取值与顺序固定为 `熔断 ×N`、`半开`、`限速中`、`配额超限`：`熔断 ×N` 对应 `circuit_breaker.state` 为 `OPEN`（N 取 `failure_count`，为 0 或非数字时只写「熔断」），`半开` 对应 `HALF_OPEN`，`限速中` 对应 `rate_limit.is_rate_limited` 为真，`配额超限` 对应 `quota_guard` / `weekly_quota_guard` 任一 `state` 为 `quota_exceeded`。多个条件并存时全部列出，一个都没有时只有「正常」。呈现分两档：无异常是 6px 绿点加灰字，异常才是 `status-badge` 胶囊，整列里异常一眼可见。tier 名、熔断器状态与失败次数、限速剩余秒数、各配额守卫窗口的已用百分比与阈值收进整块的 `title` 悬停详情；
- 分组与排序（`_subZoneHTML`）：窗口按 `group` 分组，每组 = 灰字标签 + 该组用量格，无 `group` 的家（GLM / Claude）并为一组不出标签；组内先 5h 后 周，未知类别殿后（`_windowRank`）；
- 单行与对齐：视口宽于 960px 时行是 `nowrap` 的 flex 行，分区各自 `overflow-x: auto`，余量不足时只由订阅区横向滚动（路由区 `flex: 0 0 auto` 不收缩）。滚动条隐藏：经典滚动条平台（Windows / Linux、macOS 接鼠标）上它会占掉分区的行内高度，让放不下的一行文字比行名高出几个像素。分区滚动位置按「行名 + 区」在重绘前后带走（`_zoneScroll` / `_restoreZoneScroll`），刷新不会把用户滚到的位置弹回起点。标识区 `min-width: 168px` 让各行的订阅区从同一竖线起排；路由区按内容取宽、右对齐，状态多的行（熔断、限速中、配额超限并存）自动外扩，余量不足时被挤压的仍是订阅区。960px 及以下退回两行：标识与路由区占首行，订阅区独占次行；
- 容器与行序：带 tier 的行按 `tiers` 顺序排在 `#vendor-list`，无 tier 的订阅行排在其后的 `#subscription-list`。拖拽按 `#vendor-list` 的 DOM 顺序拼 `PUT /api/tier-order` 载荷，可拖拽行必须是连续前缀，订阅行不得进入该容器；
- 并发守卫：`updateVendorStatus` 与 `updateSubscriptions` 只更新缓存（`_tierStatus` / `_subData`）再调 `_renderStatusRows`，拖拽进行中或 PUT 未决时跳过重绘。仅带 tier 数据的定向重渲染（拖拽回滚、复位后刷新）复用订阅缓存，订阅区不会被清空；`PUT /api/tier-order` 成功后先按已提交顺序同步 `_tierStatus`（`_syncTierOrder`）再刷新，拖拽结束时的补绘不会用过期顺序把行序画回去。三处拉取路由状态的入口统一走 `fetchTierStatus`：请求发出后才有重排被确认时（连按方向键），迟到的快照只采用状态字段，行序以已确认的顺序为准；整页刷新时两路数据到齐后只重绘一次，避免行节点被连续替换；
- 键盘调整：手柄是可聚焦按钮，`↑` / `↓` 触发 `_tierMove`（乐观重排并重绘，再走与拖拽相同的 `persistTierOrder` 与失败回滚）；重绘会替换节点，`_renderStatusRows` 记住焦点所在的行，重绘后回到同一行的手柄；
- 在途交互：按住手柄尚未越过阈值时行被重绘，被拖行节点已脱离文档，下一次移动按名字重新定位（找不到则本次拖拽作废）；指针按在手柄上时方向键只拦截页面滚动，不触发移动；
- 订阅数据缺失（接口失败、服务未装配）时行名不跳变，卡片底部显示「无订阅数据」。

## 三家获取途径与契约

### Claude Code（Anthropic 订阅）— 被动采集

代理对 Anthropic 官方通道纯透传、不持有 OAuth 凭证，故无主动查询通道：

- 采集点：`BaseVendor._on_upstream_response` 钩子（`vendors/base.py`，流式/非流式各一处调用），`AnthropicVendor` 覆写为 `capture_anthropic_ratelimit`（`routing/subscription_signals.py`）；
- 信号：OAuth 订阅流量专属的 `anthropic-ratelimit-unified-*` 响应头（API key 流量不返回，解析为空即跳过）；utilization 为 0.0–1.0 小数、reset 为 epoch 秒（与 `/api/oauth/usage` JSON 的 0–100/ISO8601 不同源，勿混淆）；
- 存储：进程内单例快照，重启清零，首个订阅请求即回填；无数据时卡片显示「等待首个请求」；
- 档位（Pro/Max）拿不到：响应头不含档位信息（需要凭证或读 claude.ai 网页，见「已知边界」）。

### GLM（智谱 Coding Plan）— 主动查询

- 端点：`GET {scheme://host(base_url)}/api/monitor/usage/quota/limit`，host 由 zhipu `base_url` 派生（默认 `open.bigmodel.cn`，国际站 `api.z.ai` 同路径）；
- 鉴权：**裸 api_key**（`Authorization: <key>`，无 Bearer 前缀，加 Bearer 会 401）——智谱官方插件（zai-coding-plugins）生产同款；
- 响应：`data.level`（lite/pro/max 档位）+ `data.limits[]`：`TOKENS_LIMIT` 条目按 `unit/number` 编码窗口（`3/5` → 5 小时、`6/1` → 每周），已用百分比存在 `percentage`（官方插件形态）与 `used_percent`（社区历史形态）两种字段名，重置时间 `nextResetTime`（epoch 毫秒）或 `reset_time`——全部防御式取值；
- 到期时间任何端点不返回；按量计费余额无 API key 查询通道（仅套餐额度口径）。

### Antigravity（Google）— 主动查询

- 两步：`POST cloudcode-pa.../v1internal:loadCodeAssist`（取 `currentTier.name` 档位 + `cloudaicompanionProject`，结果缓存 1h）→ `POST daily-cloudcode-pa.../v1internal:retrieveUserQuotaSummary`（body `{"project": ...}`）；
- 响应：`groups[].buckets[]`，每桶 `remainingFraction`(0–1) + `resetTime`(ISO8601) + `window`(5h/weekly)，按模型族分组（Gemini / Claude+GPT 各自双窗口）；used = 1 − remaining。透出时 `window` 原样保留，`group` 取 `displayName` 的短名（去尾部 models、「and」缩成「+」，见 `_ag_group_label`），完整名称留在 `label`；
- **UA 门控**：请求必须带 Antigravity 客户端指纹（`User-Agent: Antigravity/...` + `x-client-name: antigravity`，常量复用 `vendors/antigravity.py` 权威定义），否则 403 或缺 Gemini 组；
- 双 host 数据分裂：Gemini 组用量以 `daily-cloudcode-pa` 为准（配额查询 host 默认取 `antigravity_gateway` 的漂移敏感默认值，可经其 `base_url` 配置覆写）；
- 凭证来源优先级：vendor 通道 token manager（reauth 热更新天然跟随）→ 回退 config + token store 合成独立 `GoogleOAuthTokenManager`（vendor 未进路由链路时，如仅用 IDE gateway 透传）。

## 代码锚点

| 模块 | 职责 |
|---|---|
| [routing/subscription_signals.py](../../src/coding/proxy/routing/subscription_signals.py) | unified 头解析 + 进程级被动快照单例（取头复用 [rate_limit.py](../../src/coding/proxy/routing/rate_limit.py) 的 `_get_header`） |
| [server/subscription_status.py](../../src/coding/proxy/server/subscription_status.py) | 两家 fetcher + `SubscriptionStatusService` 聚合（TTL/锁/错误隔离）；客户端指纹与上游 host 复用 [vendors/antigravity.py](../../src/coding/proxy/vendors/antigravity.py) / [antigravity_gateway/config.py](../../src/coding/proxy/antigravity_gateway/config.py) 权威常量，zhipu 默认端点复用 [config/vendors.py](../../src/coding/proxy/config/vendors.py) 的 `ZHIPU_DEFAULT_BASE_URL` |
| [vendors/base.py](../../src/coding/proxy/vendors/base.py) | `_on_upstream_response` 上游响应抵达钩子（默认 no-op） |
| [vendors/anthropic.py](../../src/coding/proxy/vendors/anthropic.py) | 覆写钩子 → 被动采集 |
| [server/app.py](../../src/coding/proxy/server/app.py) | `app.state.subscription_status` 装配（随 generation 整体重建） |
| [server/dashboard.py](../../src/coding/proxy/server/dashboard.py) | `GET /api/dashboard/subscriptions` 端点 + Overview 卡片；合并渲染 `_renderStatusRows`（订阅 × 路由 tier），`updateSubscriptions` / `updateVendorStatus` 为其两个数据入口 |

响应形状：`{"vendors": [{key, plan?, status, error?, windows: [{id, label, window, group?, used_percent, reset_epoch, exhausted?}], updated_at?}]}`。`window` 是三家统一的窗口类别（`5h` / `weekly`，上游未知类别原样透出、可能为空串），前端据此取短标签，认不出时回退 `label`；`group` 仅 Antigravity 窗口携带（模型族短名，无分组时为空串）。`status` 为 `ok | waiting | not_configured | error`；例外：claude 条目在被动快照存在时透传 `anthropic-ratelimit-unified-status` 头原值（如 `allowed`，缺省 `ok`），前端仅按 error/not_configured/waiting 三态渲染徽章、其余值不显示状态徽章。`plan` 为可选字段（claude 恒为 null；error/not_configured 条目缺省）。

## 风险边界与已知缺口

- **Claude 档位缺失**：完整档位需服务端接管 OAuth（新建 Provider + TokenManager + 凭证落库，样板见 antigravity/google），当前按「被动用量」交付；
- **端点漂移**：三家均为非官方文档化接口（GLM 为官方插件所用、Antigravity 为仓内既有 v1internal 同族），字段漂移由宽容式解析兜底，表现为面板缺数据而非报错；
- **限频**：主动查询由 Dashboard 刷新驱动、TTL 120s 缓存（错误同样入缓存，防刷新风暴）；
- **账号口径**：Antigravity gateway 通道（IDE 自带凭证）与凭证通道账号可能不同，本面板展示凭证通道口径；
- **泄露纪律（I2）**：上游错误收敛为短消息（`HTTP 401`/`网络异常`/`凭证失效，需重新登录`），未预期异常只记类型名，绝不携带凭证/URL 细节。

## 测试锚点

- [tests/test_subscription_status.py](../../tests/test_subscription_status.py)：unified 头解析（含大小写/畸形值/NaN/Infinity/钳制）、vendor 钩子旁路采集（流式+非流式）、两家 fetcher（MockTransport、双字段形态、403 门控、token 失效、缺 displayName / window 的容错、模型族短名）、服务 TTL/错误隔离、端点集成、token store 回退构建（含 enabled 口径与 gateway host 覆写）；
- [tests/test_dashboard_subscription_card.py](../../tests/test_dashboard_subscription_card.py)：HTML 接线、XSS 转义静态检查、`SUB_META` 归并键与 `VendorType` 的一致性、内联渲染函数 Node vm 行为验证（三类行与行序、用量格语法与严重度、倒计时两种写法、分组与排序、档位去重、未知窗口类别与原型键名称回退、路由区只剩状态：正常、熔断、半开、限速中、配额超限、多条件并存、缺字段、订阅数据缺失降级、拖拽并发守卫、恶意档位名 / 模型族 / 窗口标签与 tier 名转义）、单行 CSS 约束的静态断言；拖拽状态机在最小假 DOM 上的 Node 行为验证（指针在列表外松开、多指重入、过期缓存补绘、PUT 失败回滚、键盘调整与焦点保持、迟到的状态快照不覆盖行序、按住期间行被重绘、重绘保留分区滚动位置）。
