# Issue 处理档案

> 维护已处理过的 Issue 摘要（问题描述、表因根因、处理方式、后续防范、同类问题影响与处理注意事项），便于同类问题的跨上下文处理。识别相同 Issue 时应在原条目追加复盘，避免同 Issue 多处维护。

---

## `coding reset` 误清配额窗口用量（Dashboard 配额百分比归零且不自愈）

**问题描述**

执行 `coding-proxy reset`（含 `-v anthropic,zhipu` 形态）后，Dashboard「供应商状态」卡片里 zhipu 的「1d配额 45%」徽章掉到 **0%**，且不会随时间恢复。用户预期该指令只复位熔断等异常状态，不应触碰额度记录。

**表因**

`/api/status` 的 `quota_guard.usage_percent` 由 `QuotaGuard.get_info()` 从内存字段 `_total` 计算，reset 后 `_total` 为 0。

**根因**

`QuotaGuard.reset()`（`routing/quota_guard.py`）把**状态机复位**与**用量计数清零**两件正交的事耦合在了一个方法里：

```python
self._transition_to(QuotaState.WITHIN_QUOTA)
self._entries.clear()   # ← 连带清空滑动窗口
self._total = 0         # ← 用量归零
```

之所以「不自愈」，是因为窗口基线 `load_baseline()` 的**唯一**调用点在 `server/app.py` 的 lifespan 启动钩子里 —— 进程运行期间不会再回填，只能靠新请求从 0 重新累加。文档口径（`cli-reference.md` / `api-reference.md`）自始至终只承诺「配额守卫**状态** → WITHIN_QUOTA」，从未声明会清空用量，**实现与契约不一致**。

CLI `reset` 只是 `POST /api/reset` 的瘦 HTTP 客户端，`/api/reset` 又对全部 tier 调 `quota_guard.reset()` + `weekly_quota_guard.reset()`，故 CLI 与 API 两条路径同时中招。

**处理方式**

在 `QuotaGuard.reset()` 中删除 `_entries.clear()` / `_total = 0` 两行，只保留 `_transition_to(QuotaState.WITHIN_QUOTA)`（该方法本身已负责清 `_cap_error_active` 并还原 `_effective_probe_interval`）。**单一事实源修复**：CLI、`/api/reset`、Dashboard 新增的「状态复位」按钮三条路径自动同时受益，无需各自加 `--keep-quota` 之类开关。

语义取舍：用量确已超过 `budget × threshold` 时，复位后守卫会在下一次判定立即回落 `QUOTA_EXCEEDED`（即「点了没反应」）。这是**如实**行为 —— 宁可不放行，也不伪造用量数字；真正被解开的是熔断、Rate Limit 与 cap 错误卡死标志（`_cap_error_active`），后者正是 5h 限额型 vendor 的主要卡死来源。

**后续防范**

- **写操作测试须补「不误伤其他运行时状态」守卫断言**：`tests/test_app_routes.py::test_tier_order_preserves_quota_guard` 早已是这一范式的样板，但 `/api/reset` 侧长期缺失同类断言，缺陷才得以潜伏。新增写端点时，除「改了什么」外必须同时断言「没改什么」。
- **警惕「状态机复位」与「计数清零」的耦合**：`reset()` 这类命名天然模糊。凡是同时持有状态字段与累计计数的组件（`CircuitBreaker` 的 `_failure_count` 属于状态、`QuotaGuard` 的 `_total` 属于观测数据），复位语义须显式区分二者。
- **旧测试固化了错误行为**：`test_reset_clears_all_state` 曾断言 `window_usage_tokens == 0`，等于把缺陷写成契约。修复时应先让新测试在旧实现下 FAIL，确认其真正咬住缺陷。

**同类问题影响与处理注意事项**

- **不可用线上进程验证**：`/api/reset` 会永久抹掉运行中进程的配额基线（直到重启）。验证须另起隔离实例（独立端口 + `/tmp` 数据库），严禁对用户正在使用的代理进程执行 reset。
- **`weekly_quota_guard` 复用同一个类**，修复自动覆盖周级守卫，无需单独处理。
- **`-v` 的重排序语义未变**：`-v` 仍是「提升/替换 N-tier 链路顺序」，不是「只复位这几个 vendor」的过滤器；Dashboard 按钮走无 body 路径，因此不触碰优先级。

**关联缺陷（同批修复）**

Dashboard 的「限速中」徽章读 `rlInfo.limited`，而后端 `VendorTier.get_rate_limit_info()` 产出的键是 `is_rate_limited` —— 键名不匹配导致该徽章**从未渲染过**，Rate Limit 异常态在 UI 上完全不可观测。已同步修正为 `rlInfo.is_rate_limited`。

## Session 标题豁免前缀对 `[Session]` 兜底标题不生效（豁免仅 Level 1 生效）

**问题描述**

用户在 `session_policies.title_exempt_prefixes` 配置中加入 `"[Session]"`，期望过滤掉 `[Session] claude-opus-4-8` 这类不预期标题（此前已用同机制成功豁免 `Write the title in the language ...` 注入式 Prompt）。但配置后无效——`[Session] claude-opus-4-8` 仍被写入 Session 标题。

**表因**

`[Session] claude-opus-4-8` **不是用户输入**，而是标题提取 Level 4 元数据兜底（`_extract_title_from_metadata`，`f"[Session] {request.model}"`）的产物。用户误以为它来自 user 文本，故尝试用 `title_exempt_prefixes` 豁免。

**根因**

`_extract_session_title`（`routing/executor.py`）采用 4 层回退，但 `exempt_prefixes` **仅在 Level 1**（`_extract_title_from_user_text`）消费——这是原始设计：v0.5.2a1 #265 引入该配置时仅覆盖 L1，docstring 明写「Level 2/3/4 不参与豁免」。Level 2/3/4 的合成标题（`[Tool output]` / `[N Image]` / `[Tool call]` / `[Session]`）完全不查询豁免名单。故用户配的 `"[Session]"` 只进到 L1，对 L4 兜底标题毫无作用——这是「配了却不生效」的根本原因，**非配置错误**。

**处理方式**

在 `_extract_session_title` 编排层引入 `_is_exempt` 闭包，对 L1/L2/L3/L4 每层候选统一做大小写敏感 `startswith` 拦截，命中则向下一层回退、全豁免返回空串。返回空串触发调用方 `if title:` 跳过写库，标题保持空，待后续请求带真实 user 文本时经 `update_empty_session_title` 回填。L1 内部「逐条消息跳过」语义不变，外层闭包作一致性兜底；并补入参空串前缀防御。**默认行为零影响**（不添加前缀则不豁免）；**不**把合成前缀塞进默认 config，避免误杀正常 `[Tool output]` 标题。新增 8 个回归测试（含 mock recorder 验证写库跳过）。

**后续防范**

- **新增合成兜底标题须纳入豁免链路**：未来若在 L2/L3/L4 增加新的 `f"[Xxx] ..."` 合成标题，编排层的 `_is_exempt` 自动覆盖（无需改子函数），但应在 `config.default.yaml` 注释里补充示例前缀，提示用户可豁免。
- **空串前缀恒真陷阱**：`"".startswith` 恒真会豁免一切。配置层 field_validator、构造器、模块函数入参已三重过滤，任何新增的 startswith 豁免/绑定逻辑都须保留空串防御。

**同类问题影响与处理注意事项**

- **`[Session] <model>` 触发条件**：请求无可提取的 user TEXT / TOOL_RESULT / IMAGE / tool_names，只剩 model 字段（典型如首条消息全是 `<system-reminder>` 噪声、或仅 assistant 消息的续接请求）。排查此类标题应优先确认请求体内容，而非怀疑配置。
- **豁免语义边界**：豁免 =「跳过该候选、继续向下一层回退」，非「替换为别的标题」。L4 是最后一层，被豁免只能落到空串；依赖标题触发 `_apply_title_based_policy` 供应商自动绑定的场景，空标题不会触发绑定（预期副作用，语义自洽）。
- **不要把合成前缀写进默认配置**：`[Tool output]` 等对用户有信息量，默认豁免会造成正常工具结果摘要标题消失的回归。

---

## Zhipu 529 过载重试退避非单调（对齐 429 指数退避语义）

**问题描述**

cc 调用 zhipu 返回 529 过载触发重试时，延迟序列呈非单调形态（实测 `418.8ms → 1857.7ms → 961.6ms → 3769.7ms`，第 3 次反而短于第 2 次），不像 429 那样呈现干净的指数退避。

**表因**

`src/coding/proxy/routing/retry.py::calculate_delay` 的兜底抖动为 **Full Jitter**：`delay = random.uniform(0, ceiling)`，每次延迟是 `[0, ceiling]` 区间均匀随机值，本质非单调。实测值逐项精确匹配 `attempt 0/1/2/3` 的 `uniform(0,1000)/(0,2000)/(0,4000)/(0,8000)`。

**根因**

关键认知修正：429 与 529 在代码层面**早已共用同一退避路径** `ZhipuVendor._compute_retry_delay_from_headers`（`vendors/zhipu.py:230-247`），并非"两套规则"。感知差异来自两个因素叠加：

1. **服务端响应头不对称**：429（限流）响应通常携带 `Retry-After` 头 → 走确定性 server-guided 路径（`retry_after * 1.1`），看起来"干净递增"；529（过载）响应通常**不携带**该头 → 落入 Full Jitter 兜底分支。
2. **Full Jitter 本身非单调**：即使 429 也一样，只是 429 多数情况有 `Retry-After` 遮蔽了该问题。

故真正修复对象是共享的抖动策略，而非给 529 单独加逻辑。

**处理方式**

将 `calculate_delay` 从 Full Jitter 改为 **Equal Jitter**（AWS M. Brooker, "Exponential Backoff And Jitter," 2015）：

```
temp  = min(initial * backoff^attempt, max)
delay = temp/2 + random.uniform(0, temp/2)      # [temp/2, temp]
```

Zhipu 配置下区间变为 `[500,1000] → [1000,2000] → [2000,4000] → [4000,8000]`，相邻区间仅边界相切，延迟几乎必然单调非递减；保留抖动以防惊群；429/529 同步受益；`retry-after` 优先级链不动。同步更新 `retry.py` / `zhipu.py` 共 4 处 "Full Jitter" docstring，新增 `tests/test_retry.py` 独立单元测试与 `test_zhipu.py::test_529_equal_jitter_delay_in_expected_band`。

**后续防范**

- **单调性是配置相关的弱保证**：依赖 `backoff_multiplier >= 2.0` 且未触及 `max_delay_ms` 封顶。封顶后（`initial*2^attempt >= max`）各 attempt 区间退化为同一 `[max/2, max]`，单调性丧失。当前 Zhipu `max_retries=4` 触及不到，但未来调参须注意。函数 docstring 已写明此契约边界。
- **诚实权衡（Brooker 反方观点）**：Brooker 2015 分布式吞吐基准显示 Full Jitter 优于 Equal Jitter。但本场景是单代理（CC）低并发过载恢复，诉求是可解释性（日志递增可预测）而非分布式吞吐最优，CC 并发量级远未达"海量"，差异在噪声内。若未来观测到并发过载下的惊群回归，应优先要求 server 提供 `retry-after` 或评估 Decorrelated Jitter，而非回退 Full Jitter。

**同类问题影响与处理注意事项**

- **双 `RetryConfig` 死代码（重要遗留债）**：仓库存在两个 `RetryConfig`——`routing/retry.py:25`（活跃 dataclass，仅 Zhipu 用）与 `config/resiliency.py:15`（Pydantic 死代码，`config/routing.py:285` 注册为 `retry` 字段但 `VendorTier.retry_config` 从未赋值，全仓库 `grep "retry_config="` 为空）。`docs/arch/routing.md:22` 与 `config-reference.md:95` 引用了不同的 `RetryConfig`，构成认知陷阱。本次修复**未触碰**死代码（超出范围），后续应单独清理。这也是本次"为何选方案 A（直改 `calculate_delay`）而非方案 B（加可配置 jitter_strategy 字段）"的决定性理由——方案 B 会扩大 schema 与运行时的裂缝。
- **`calculate_delay` 唯一消费者确证**：经全仓库 grep，仅 `vendors/zhipu.py:247` 调用（`routing/__init__.py` 仅 re-export，`tier.retry_config` 字段未激活）。修改其抖动策略爆炸半径仅限 Zhipu，安全。
- **测试缺口已补**：修复前 `calculate_delay` 无任何独立单元测试（仅通过 zhipu 集成测试间接覆盖，且那些测试要么禁用 jitter、要么走 retry-after 路径），现已新建 `tests/test_retry.py`。

---

## streaming usage parse failed: 'NoneType' object has no attribute 'get'

**问题描述**

OpenAI 兼容 SSE 流式响应过程中，单次请求日志反复刷出数十条 WARNING：

```
WARNING streaming usage parse failed: 'NoneType' object has no attribute 'get'
```

警告本身被上层 `try/except` 吞掉不影响主链路，但日志噪声严重，且每帧都丢失了 usage 累加。

**表因**

`StreamingUsageAccumulator.feed` 调用 `parse_usage_from_chunk` 解析 SSE chunk 时抛出 `AttributeError`。

**根因**

`src/coding/proxy/routing/usage_parser.py::parse_usage_from_chunk` 中 Anthropic message_start 与 Anthropic message_delta / OpenAI 两条分支都使用了脆弱的判空模式：

```python
if "usage" in data:        # 仅判断 key 存在
    u = data["usage"]      # 但值可能是 null
    u.get("output_tokens", 0)  # AttributeError
```

部分上游（含某些 OpenAI 兼容供应商）在中间 chunk 显式发送 `"usage": null` 占位帧，`in` 检查通过但取出的是 `None`。

**处理方式**

将两处 guard 统一改为 `u = container.get("usage"); if isinstance(u, dict):`，既排除缺省也排除 null，并顺手移除内部冗余的 `if isinstance(u, dict):` 包装层（已被外层 guard 覆盖）。同时新增三个回归用例覆盖 `data.usage = null` / `message.usage = null` / null 帧后跟有效帧三种场景。

**后续防范**

- 解析外部 SSE / JSON 结构时, 不要单独使用 `if key in data` 作为安全 guard, 应统一采用 `value = data.get(key); if isinstance(value, dict):` 的双重保护, 同时排除缺省与显式 null。
- 对 try/except 包裹的 WARNING 路径要保持警觉: 异常被吞不代表无害，重复刷屏的同类警告往往暗示防御性 guard 过窄，需要回溯至根因修复，而非依赖 except 兜底。

**同类问题影响与处理注意事项**

- 本仓库内 `parse_usage_from_chunk` 的 Gemini `usageMetadata` 分支 (line ~219) 已经使用 `isinstance(um, dict)` 防御, 不受影响, 可作为参考实现。
- 检查其他解析器 (如 routing / vendor adapter 层) 是否还有 `if "key" in data: v = data["key"]; v.get(...)` 这种模式, 必要时同步加固。

---

## anthropic 400: `tool_use` ids were found without `tool_result` blocks immediately after

**问题描述**

zhipu → anthropic 通道流式请求偶发 400, 错误形如:

```
WARNING anthropic stream error: status=400 body=...
  messages.3: `tool_use` ids were found without `tool_result` blocks immediately after: toolu_normalized_2.
INFO  Failover: anthropic → zhipu (reason: HTTP 400)
INFO  Tier zhipu stream succeeded (took over from failed tier: anthropic)
```

同一请求伴随 `Applied transition channel zhipu → anthropic: rewritten_N_srvtoolu_ids, misplaced_tool_result_relocated, stripped_M_thinking_blocks` 的 adaptations 但**没有 `orphaned_tool_use_repaired`**, 即转换层主观上认为已配对、但 Anthropic 仍判定结构不合规。Failover 至 zhipu 后请求成功, 证明上游消息体本身没有损坏, 问题出在 zhipu→anthropic 通道转换过程引入了不一致。

**表因**

`src/coding/proxy/convert/vendor_channels.py::_rewrite_srvtoolu_ids` 在单遍循环中同时承担 Case A (assistant 端 `server_tool_use` → `tool_use` 与 `srvtoolu_*` ID 重写) 与 Case B (任意位置 `tool_result.tool_use_id` 同步重写)。Case B 依赖 `id_map` 已被 Case A 填入。

**根因**

Zhipu GLM-5 流式响应偶发将 inline `tool_result` 块输出在**对应的 `server_tool_use` 块之前** (同 assistant content 内乱序), 或将 `tool_result` 放在更早的 user 消息中而对应 `tool_use` 在更晚的 assistant 消息。两种乱序下, 单遍扫描遍历到 `tool_result` 时 `id_map` 还是空 → `tool_result.tool_use_id` 不被改写, 停留在 `srvtoolu_X`; 随后 Case A 把对应 `tool_use.id` 改写为 `toolu_normalized_N`。

后续 `enforce_anthropic_tool_pairing` Step A 提取这条 misplaced tool_result 时使用**旧 ID** 作为 `extracted_tool_results` 字典 key, Step F 用新 ID 去查 → 不命中 → 走 `existing_result_ids` 分支, 因为相邻 user 的 tool_result 已经被改写到新 ID, 该 uid 命中 `existing_result_ids` 被 continue 跳过, 于是 enforce 错误地认为完成配对、不产生 `orphaned_tool_use_repaired` 标签, 而被默默丢弃的 misplaced tool_result 本应填补到的 user 槽位实际上**仍然缺位**。最终 body 中某条 assistant 的 tool_use 在下一条 user 中找不到对应 tool_result → Anthropic 400。

**处理方式**

1. `_rewrite_srvtoolu_ids` 改为**两遍扫描**: Pass 1 仅遍历 assistant 消息收集 `id_map` (按 assistant 出现顺序分配, 保持序号兼容性); Pass 2 全量遍历改写任意 `tool_result.tool_use_id`。以"先建表、后改写"的次序消除时序耦合。
2. 在 `enforce_anthropic_tool_pairing` 主循环末尾追加独立 helper `_enforce_pairing_sanity_pass`, 仅做检测+合成 `is_error=True` 占位 (不剥离、不重定位), 命中追加 `pairing_sanity_repaired` adaptation 并打 WARNING (含 message index 与 uid)。这层作为纵深防御, 在主循环未来重构时仍能稳定守住 Anthropic 配对约束。
3. 新增回归测试覆盖三类场景: 同 assistant content 内乱序、跨消息边界 tool_result 早于 tool_use、端到端复现日志故障形态。新增 `TestEnforcePairingSanityPass` 独立测试套件确保兜底分支具备正向回归保护。

**后续防范**

- 任何在多 content block 之间存在**前向引用** (后出现的块定义的标识符被前面的块引用) 的就地改写逻辑, 都必须采用两遍扫描或全局表先建后用, 不可依赖遍历位置上 "上一次循环已经写入" 的隐含次序。
- 纵深防御层 (sanity helper) 必须**独立可单测**, 而不是把 sanity 内嵌在主路径内部 — 否则主路径的快速通道会让 sanity 分支永远走不到正向测试, 缺乏回归保护。
- adaptations 标签 (`pairing_sanity_repaired`) 与主循环标签 (`orphaned_tool_use_repaired`) 分离, 便于运维聚合时按层归因。

**同类问题影响与处理注意事项**

- 历史教训: commit `9061cd0` 曾经实现"两遍扫描 + sanity helper"修复了正是这类问题, 但 commit `2bac9a7` revert 至 v0.3.0 时**连带回滚**了它 — revert 的真实目标是去除 `f497077` / `fdd4a92` / `43488a1` 引入的"zhipu 自清理通道"和"tool_result.id 注入"副作用, 两遍扫描属无辜方。**后续若再次需要 revert `vendor_channels.py`**, 必须先 `grep _enforce_pairing_sanity_pass` 与 `Pass 1` / `Pass 2` 注释, 确认这两段是核心修复而非可以一起回滚的实验性代码。
- 类似 "vendor 私有 ID 跨消息体改写" 场景 (如 doubao、minimax 未来若引入类似机制), 实现时同样应当遵循"先全局收集 id_map、后统一改写"的两阶段模式。
- 单元测试覆盖"块顺序敏感"类 bug 时, 建议在用例命名中显式标注顺序条件 (如 `test_two_pass_handles_inline_tool_result_before_server_tool_use`), 让未来 reviewer 一眼看出测试的边界价值。

---

## count_tokens 路由 `AttributeError: 'ZhipuVendor' object has no attribute 'name'`

**问题描述**

后台日志反复出现 `POST /v1/messages/count_tokens?beta=true 500 Internal Server Error`，并伴随：

```
File ".../coding/proxy/server/routes.py", line 153, in count_tokens
    channel_fn = get_transition_channel(source, target_vendor.name)
AttributeError: 'ZhipuVendor' object has no attribute 'name'
```

同一时间窗口内大量请求 200 OK、少量请求 500，呈"间歇性"故障特征。

**表因**

`src/coding/proxy/server/routes.py` 的 `count_tokens` 在 153 / 160 两处访问 `target_vendor.name`，触发 `AttributeError` 被 ASGI 中间件捕获返回 500。

**根因**

`BaseVendor` 仅暴露**抽象方法** `get_name() -> str`（`src/coding/proxy/vendors/base.py:75-77`），所有派生类（`AnthropicVendor`、`ZhipuVendor`、`CopilotVendor`、`MinimaxVendor`、`DoubaoVendor`、`KimiVendor` 等）均通过 `_vendor_name` 类属性配合 `get_name()` 返回名称 —— **并无 `name` 实例属性**。该错误访问在 lint/类型检查阶段无告警（因 `BaseVendor` 未在类型系统中约束 `name` 字段），仅在运行时触发。

间歇性原因：第 152 行 `if source:` 是守卫；`source` 由 `infer_source_vendor_from_body(body)`（`src/coding/proxy/convert/vendor_channels.py:357-394`）从请求体启发式推断，仅当出现 zhipu 私有产物（`srvtoolu_*` 形式的 `tool_use.id` 或 `server_tool_use` / `server_tool_use_delta` 类型 content block）时返回 `"zhipu"`，否则 `None`。纯净的首轮 count_tokens 请求 `source is None` 自然绕过 153 行，因此 200/500 共存。

**处理方式**

1. `routes.py:153,160` 将 `target_vendor.name` 改为 `target_vendor.get_name()`，并将结果提取到局部变量 `target_name` 复用，避免重复方法调用与日志/调用点不一致风险。
2. `tests/test_app_routes.py` 新增 `test_count_tokens_triggers_zhipu_to_target_channel`：通过注入 `server_tool_use` + `srvtoolu_*` 让 `infer_source_vendor_from_body` 返回 `"zhipu"`，断言返回 200 且 debug 日志含 `"count_tokens channel zhipu → anthropic"`，证明通道被实际触发。此前 6 个 count_tokens 测试的请求体都是纯净的、未触达该分支，是 bug 长期漏过的根因。

**后续防范**

- 跨模块引用 Vendor 实例字段时，**统一通过 `BaseVendor` 暴露的方法**（`get_name()`、`map_model()` 等），避免直接访问派生类未定义的"假属性"。
- 长期演进可考虑在 `BaseVendor` 增加 `@property name` 指向 `get_name()`，将契约前移到类型系统由 mypy / pyright 拦截 —— 该重构属"演进式设计"范畴，不在本次最小干预范围内。
- 测试覆盖原则：路由层涉及"内容感知"分支（如 `infer_source_vendor_from_body`）时，至少补一个让分支命中的最小用例，避免守卫掩盖代码缺陷。

**同类问题影响与处理注意事项**

- 已 `grep -rn "vendor\.name\b" src/` 全仓扫描，确认 `target_vendor.name | vendor.name` 误用仅 routes.py 的这两处，已随本次修复一并消除。`/v1/messages` 主链路在 executor 中调用 `tier.name`（`Tier` 对象的合法 dataclass 属性），与 vendor 实例 `name` 无关，不受影响。
- 若未来新增 Vendor 子类，仍只需实现 `get_name()` 抽象方法；外部调用方应遵循同一契约，本档案的修复模式可作为参考。

---

## Gemini embedding 透传至 Vertex AI 上游返回 `request body doesn't contain valid prompts`

**问题描述**

通过本代理调用 Gemini embedding 模型时，上游返回 400：

```
litellm.BadRequestError: GeminiException BadRequestError -
{"error":{"message":"request body doesn't contain valid prompts"}}
POST /api/gemini/v1beta/models/gemini-embedding-001%3AbatchEmbedContents 400
```

litellm 报错日志中 URL 路径是 `:batchEmbedContents`，调用端疑似格式不兼容。

**表因**

litellm 按 Google AI Studio 格式构造请求：
- 路径：`POST {api_base}/v1beta/models/{model}:batchEmbedContents`
- Body：`{"requests": [{"model": "models/...", "content": {"parts": [{"text": "..."}]}}]}`

但实际上游（如 `llms.as-in.io` 这类 Vertex AI 风格网关）只接受 Vertex AI 格式：
- 路径：`POST {api_base}/v1beta1/publishers/google/models/{model}:embedContent`
- Body：`{"content": {"parts": [{"text": "..."}]}}`

且无 `batchEmbedContents` 端点。

**根因**

1. 代理 `NativeProxyHandler.dispatch()` 是字节级透传，对 embedding 端点未做协议适配，直接把 Google AI Studio 格式的 URL/Body 转给 Vertex AI 上游，路由不匹配。
2. litellm `_check_custom_proxy()` 在自定义 `api_base` 场景下会丢失 `v1beta/` 版本前缀，发送 `{api_base}/models/{model}:verb`，使代理原有的 `OperationClassifier` 正则（要求 `v1beta/` 前缀）失配，进而走原始透传分支再次失败。

**处理方式**

1. `src/coding/proxy/native_api/operation.py`：放宽 Gemini 路径正则中的 `v1(?:beta1?)?/` 段为可选，兼容 litellm 丢失版本前缀的异常路径。
2. `src/coding/proxy/native_api/handler.py`：在 `dispatch()` 中新增 Gemini embedding Vertex AI 适配分支：
   - 仅当 `provider == "gemini"`、`operation in {"embedding", "embedding.batch"}`、且 `base_url` 非官方 `generativelanguage.googleapis.com` 时启用；
   - `embedContent` → 重写路径为 `v1beta1/publishers/google/models/{model}:embedContent`，剥离 body 中的 `model` 字段；
   - `batchEmbedContents` → 拆分为多次并发 `embedContent` 调用（`asyncio.gather`），聚合响应为 `{"embeddings": [...]}` 返回；
   - 用量抽取累加各子请求的 `usageMetadata`。
3. `tests/test_native_api_handler.py`：新增 3 个回归测试覆盖单次 / 批量 / 官方上游透传不变三类场景。

**后续防范**

- 协议适配层只对**非官方上游**生效，官方 `generativelanguage.googleapis.com` 仍走字节级透传，避免引入不必要的转换开销与协议偏差。
- 上游路径分支的判定优先用 base_url 域名而非依赖网关行为特征，便于后续扩展（如 Vertex Express、其他 LLM gateway）时的精确匹配。
- 真实链路验证：使用 litellm `embedding(api_base=..., api_key=...)` 单输入 / 多输入分别调用，确认返回 3072 维向量及正确批量计数。

**同类问题影响与处理注意事项**

- litellm 在 Gemini 其他端点（`generateContent` / `countTokens`）同样存在 `_check_custom_proxy` 丢失 `v1beta/` 前缀的 bug；本次仅放宽了 `operation.py` 中的路径正则（让分类器能识别此类异常路径），未对这些端点做格式转换，因为非 embedding 端点的 Google AI Studio / Vertex AI 请求体差异较小，多数上游兼容。如未来出现类似失配再做针对性适配。
- 若上游网关同时支持 OpenAI `/v1/embeddings` 与 Vertex AI 路径，建议优先在客户端配置 OpenAI 兼容路径，减少协议转换链路。

---

## Dashboard Sessions 页 `Tokens` 列漏算缓存 Token

**问题描述**

Dashboard 的 **Sessions** 标签页中，每条会话的 `Tokens` 列与展开详情卡的 `Tokens` 值，仅统计 `input + output`，遗漏了 `cache_creation`（写缓存）与 `cache_read`（读缓存）。在长链路 Anthropic Prompt Cache 场景下，读取命中常常是 input/output 的数倍，导致 Sessions 页总量被显著低估，与 Overview 标签页（卡片、Token 时序图）跨页口径分裂。

**表因**

前端 `dashboard.py:1597 / 1614` 直接渲染 `s.total_tokens`，该值由 `/api/dashboard/sessions` 透传自 `token_logger.query_recent_sessions()` 的聚合结果。

**根因**

`src/coding/proxy/logging/db.py` 中两条按 `session_key` 分组的聚合 SQL 使用了不完整的求和口径：

```sql
SUM(input_tokens + output_tokens) AS total_tokens   -- 第 607 行（query_recent_sessions）
SUM(input_tokens + output_tokens) AS total_tokens   -- 第 634 行（query_session_profile）
```

而同文件内 `query_usage()`（第 465–466 行分别 `SUM(...)` 四列）与 `query_total_tokens_by_vendor()`（第 584 行 `SUM(input + output + cache_creation + cache_read)`）已采用完整四项口径，构成了同文件内的口径双标。

**处理方式**

复用 `query_total_tokens_by_vendor` 的四项求和表达式，将两处 `total_tokens` 改写为：

```sql
SUM(input_tokens + output_tokens
    + cache_creation_tokens + cache_read_tokens) AS total_tokens
```

不改动 API 返回结构、不新增字段、不改前端 detail-card——前端 `fmtTokens(s.total_tokens)` 调用无须变更。同时在 `tests/test_session_aware.py` 的 `test_query_recent_sessions_basic` / `test_query_session_profile_found` 中追加 `cache_creation_tokens` / `cache_read_tokens` 入参与完整口径断言，覆盖回归。

**后续防范**

- SQL 聚合层涉及"总 Tokens"概念时，必须保持**单一权威定义**（Single Source of Truth）：要么所有视图共用同一求和表达式，要么抽取为常量片段集中引用，杜绝多处独立维护造成的语义漂移。
- 未来若引入新的 token 维度（如 reasoning_tokens、tool_tokens 等），需要全文检索 `SUM(input_tokens + output_tokens` 这一历史模式并同步补齐，避免出现新的口径分裂点。

**同类问题影响与处理注意事项**

- 历次 PR 中 cache token 字段的引入是渐进式的（schema 已有四列、`log()` 入参齐全、Overview 已全口径消费），但部分聚合视图的口径升级被遗漏；任何向 `usage_log` 增列后，**必须**审计所有 `SUM(input_tokens` / `SUM(output_tokens` 出现处的聚合表达式是否需要同步更新。
- 跨标签页同一指标（如"总 Tokens"）的口径一致性，建议在添加新视图时主动与 Overview 现有口径做交叉核对，必要时在 SQL 注释中标注口径来源，便于后续 review。

---

## Zhipu vendor 间歇性 `[1210][API 调用参数有误]` 拒绝（诊断阶段）

**问题描述**

Zhipu vendor 作为首选 tier 时，处理 `claude-haiku-* → glm-5-turbo` 的部分请求被上游直接拒绝：

```
WARNING Tier zhipu semantic rejection
  (type=invalid_request_error,
   msg=[1210][API 调用参数有误，请检查文档。][...])
  [model=claude-haiku-4-5-20251001, messages=1], trying next tier without recording failure
INFO  Tier anthropic message succeeded (took over from failed tier: zhipu)
```

失败请求统一表现为 `duration<1s + tokens=[0 0 0 0]`，被 zhipu 在入口校验阶段直接拒绝、未消耗任何 token。两次观察窗口失败率分别为 4%（2026-05-23 22:24，glm-4.7 旧映射）与 27%（2026-05-25 17:26+，glm-5-turbo 当前映射），均触发降级至 anthropic / copilot。

**表因**

`is_semantic_rejection` 检测到 zhipu 返回 `invalid_request_error + 1210` 含「API 调用参数有误」中文标记，判定为语义拒绝，跳过下一层 tier。1210 是智谱官方错误码，[官方文档](https://docs.bigmodel.cn/cn/api/api-code) 定义为「参数格式/类型不符规范」（区别于 1213「必需字段缺失」、1214「字段参数非法」）。

**根因（已定位，修复中）**

PR #247 (Step 1 v2) 部署后，2026-05-26 16:30–16:31 的诊断日志显示 8 次连续拒绝**全部携带 `thinking={"type": "adaptive"}`**（Anthropic Claude 4.x 新增的参数类型），而同一时段其他会话的请求持续成功。之前 curl 测试仅验证了 `{"type": "enabled"}`，未覆盖 `adaptive` 类型。GLM 可能不支持此特定类型值，导致 [1210] 参数校验失败。

**处理方式（分阶段）**

- **Step 1（PR #244，已合并）**：在 `executor.py::_build_semantic_rejection_diagnostic` 中输出 thinking / cache_control 相关字段 — 但证据反转，覆盖不足以定位真因。
- **Step 1 v2（PR #247，已合并）**：扩展诊断函数覆盖 `system_kind|blocks(+cc)` / `tools` / `tool_choice` / 采样参数 / `stream` / `metadata_keys` / `content_types` / `body_bytes` 等维度。所有项「仅存在时输出」以控制日志噪声。配套 14 个单元测试（`TestBuildSemanticRejectionDiagnostic`）覆盖各字段组合。
- **Step 2（进行中）**：基于 Step 1 v2 的日志证据，在 `ZhipuVendor._prepare_request` 中实现 **兼容转换**（而非移除）：
  - `thinking.type="adaptive"` → `{"type": "enabled", "budget_tokens": 16000}`（保留 thinking 能力）
  - 新增 `_build_zhipu_request_snapshot` 诊断快照，同时覆盖成功/失败请求，建立可对比证据链
  - 扩展语义拒绝日志的错误体截断限制（200 → 500 字符），保留完整字段级诊断
  - `metadata` 暂不处理（待进一步诊断确认兼容性）

**后续防范**

- **「无证据，不下结论」**：当初版诊断字段无法覆盖根因时，禁止反复猜测，应优先扩展诊断维度抓取更多线索。本次先扩展再修复的迭代节奏可作为同类「黑盒 API 报错」问题的范式。
- **诊断字段设计原则**：所有诊断项应「仅存在时输出」，避免常态化噪声；输出格式紧凑（`key=val`）便于日志检索；参数值用 `!r:.N` 截断防止巨型对象灌入日志。
- **错误码差异化**：智谱 12xx 系列错误码语义并不等价（1210 ≠ 1213 ≠ 1214），未来面对类似 `[code][message]` 形式的供应商错误时，应优先查阅其官方错误码字典，避免基于错误消息字面意思的误判。

**同类问题影响与处理注意事项**

- 其他薄透传 vendor（minimax / kimi / doubao / alibaba / xiaomi）共用 `NativeAnthropicVendor._prepare_request`，若它们也开始报「参数错误」类语义拒绝，可复用本次扩展的诊断函数定位差异。
- 若证据指向 `tools` 字段（如工具 schema 不兼容）、`metadata` 字段（如自定义键被 zhipu 拒收）等具体路径，修复时应优先复用 `convert/vendor_channels.py` 中已有的 `normalize_for_zhipu` / `strip_thinking_blocks` 工具，避免在 vendor 内部重复实现剥离逻辑。
- 部署 Step 1 v2 后，建议观察至少 48 小时收集足够样本（>20 次失败），通过失败/成功请求形态对比统计找出**唯一差异维度**，再进入 Step 2。
