# Issue 处理档案

> 维护已处理过的 Issue 摘要（问题描述、表因根因、处理方式、后续防范、同类问题影响与处理注意事项），便于同类问题的跨上下文处理。识别相同 Issue 时应在原条目追加复盘，避免同 Issue 多处维护。

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

## zhipu 自循环 400 + tool_results 偶发降级

**问题描述**

生产日志反复出现下述链路: 请求一开始命中 zhipu 主 tier, 但在含 `tool_results` 的多轮工具调用场景下偶发返回 400, 触发到 copilot 二级 tier。具体日志特征:

```
WARNING Tier zhipu likely format incompatibility (400 + tool_results), trying next tier without recording failure
WARNING Tier zhipu semantic rejection (400), trying next tier without recording failure
DEBUG Applied transition channel zhipu → copilot: rewritten_38_srvtoolu_ids, stripped_16_thinking_blocks, removed_3_cache_control_fields, misplaced_tool_result_relocated
```

zhipu → copilot 通道的 adaptations 列表暴露了上一轮 zhipu 响应中存在的非标准产物 (`srvtoolu_*` ID、自签 thinking、错位的 `tool_result`)。

**表因**

zhipu 自身偶发返回 400, 但错误体非 JSON 结构, 由 `_is_likely_request_format_error()` 判定为「格式不兼容」并跳过当前 tier。

**根因**

1. zhipu 是 `NativeAnthropicVendor` 薄透传供应商, **不做任何请求体预处理**。
2. `executor._determine_source_vendor` 三条优先级路径均以 `source != target_name` 过滤掉了同 vendor 自转换。
3. `VENDOR_TRANSITIONS` 注册表中无 `("zhipu", "zhipu")` 条目。

后果: GLM-5 偶发产出非标准产物 (assistant 内联 `tool_result`、`server_tool_use_delta` 流式残块) 后, Claude Code 把这些产物原样回送下一轮请求时, **没有任何清洗发生**, 直接被转发到 zhipu 自身, 命中 zhipu 端的输入校验返回 400。

**处理方式**

- 在 `vendor_channels.py` 新增 `prepare_zhipu_self_cleanup` 函数, 仅修复 zhipu 自身拒绝的两类产物:
  1. 剥离 `server_tool_use_delta` 流式残块
  2. `enforce_anthropic_tool_pairing` 把 assistant 内联 `tool_result` 重定位到紧随 user 消息
- 显式 **保留** zhipu 原生支持的特性: `srvtoolu_*` ID、`server_tool_use` 类型、自签 thinking signature、`cache_control` (cache_read 已在生产实证)、顶层 `thinking` 参数。
- 在 `VENDOR_TRANSITIONS` 注册 `("zhipu", "zhipu") = prepare_zhipu_self_cleanup`。
- 在 `executor._determine_source_vendor` 三条优先级路径中, 把「`source != target`」过滤替换为「通道已注册」门控 (`get_transition_channel(...) is not None`), 让自转换通道在显式注册时启用, 未注册时退化为原行为。

**后续防范**

- 新增 `NativeAnthropicVendor` 子类 (minimax / kimi / doubao / xiaomi / alibaba 等) 时, 若上游 vendor 偶发产出违反 Anthropic 规范的产物, 可按需注册同名自清理通道, executor 无需任何额外改动。
- 同 vendor 自转换通道应**精确剪裁**: 仅修复 vendor 自身拒绝的产物, 不要套用跨 vendor 通道的全量清理 (会误伤 vendor 原生支持的特性, 如 cache_control 损失带来 cache_read miss)。

**同类问题影响与处理注意事项**

- `enforce_anthropic_tool_pairing` 仅识别 `tool_use` 类型 (不含 `server_tool_use`), 因为 `server_tool_use` 由 vendor 自身执行, 不需要客户端 `tool_result`。构造测试或类似清洗逻辑时需注意此差别。
- `_is_likely_request_format_error()` 把「400 + tool_results + 非结构化错误体」一律标记为格式不兼容并跳过 tier 不计熔断器, 这层兜底虽能维持可用性但会**掩盖** vendor 自身的间歇性问题, 让根因更难发现。处理类似 400 偶发时, 应优先看 `Applied transition channel` 日志中的 adaptations 列表, 它能精确暴露上游响应中的非标准产物。

---

## anthropic 报 messages.X tool_use 缺 tool_result (zhipu→anthropic 故障转移失败)

**问题描述**

zhipu 完成响应后, executor 故障转移至 anthropic 时反复失败 (HTTP 400):

```
DEBUG Applied transition channel zhipu → anthropic: rewritten_86_srvtoolu_ids, misplaced_tool_result_relocated, stripped_18_thinking_blocks
WARNING anthropic stream error: status=400 ... messages.3: `tool_use` ids were found without `tool_result` blocks immediately after: toolu_normalized_3.
INFO  Failover: anthropic → zhipu (reason: HTTP 400)
```

adaptations 列表显示 `misplaced_tool_result_relocated` 但**没有** `orphaned_tool_use_repaired`, 即 enforce 单遍扫描视角下认为所有 tool_use 都已配对; 但 anthropic 仍报 messages.X 缺 tool_result, 导致请求级 cascade failover 反复回到 zhipu。

**表因**

`prepare_zhipu_to_anthropic` 链路输出的请求体中, 某个 assistant 的 `tool_use` 在紧邻的 user 消息中没有匹配的 `tool_result` 块。

**根因**

`_rewrite_srvtoolu_ids` 采用单遍正向扫描: 在同一次循环中一边收集 srvtoolu_* → toolu_normalized_* 的 id_map, 一边改写遇到的 `tool_result.tool_use_id`。GLM-5 流式偶发将 inline tool_result 输出在本消息 `server_tool_use` 之前 (block 顺序异常), 导致:

1. 处理 inline tool_result 时, id_map 尚未填入对应 srvtoolu_* → 漏改名, inline 仍保留 `srvtoolu_X`
2. 处理本消息 server_tool_use 时, 填入 id_map 并把 tool_use 改名为 `toolu_normalized_X`
3. 进入 `enforce_anthropic_tool_pairing` 时:
   - A 步 extracted dict key = `srvtoolu_X` (inline 保留的旧 ID)
   - B 步 tool_use_ids = `[toolu_normalized_X]` (已改名)
   - F 步 `uid in extracted` 检查失败 (key 错位), 但若 next user 已含其他 stale tool_result 让 existing_result_ids "巧合" 命中, F 步会跳过 synth → 不触发 orphan 标签
   - 最终 anthropic 看到 messages.X 真的缺 toolu_normalized_X 的 tool_result → 400

**处理方式**

- `_rewrite_srvtoolu_ids` 改为**两遍扫描**: Pass 1 仅遍历 assistant 消息, 收集 id_map 并改写 tool_use 自身的 id 与 type; Pass 2 全量遍历所有消息 (含 user / 异常 assistant 内联), 统一改写所有 `tool_result.tool_use_id` 引用。彻底消除 block 顺序敏感性。
- `enforce_anthropic_tool_pairing` 主循环结束后追加**全局 sanity check pass**: 重新遍历每条 assistant, 验证其 tool_use_ids 全部在 next user 的 tool_result 中存在; 发现遗漏直接合成 is_error 占位并打 `pairing_sanity_repaired` 标签。作为防御深度抵御未来其他主循环边角错位。
- A 步对 `tool_use_id` 缺失的破损 inline tool_result 也计入 relocated_count (避免 silent drop 影响 adaptations 标签可观测性)。

**后续防范**

- 任何"按出现顺序填充字典 + 同遍引用查询"的两阶段操作都应警惕**顺序耦合**问题。两遍扫描 (collect → apply) 是消除此类 bug 的标准 pattern。
- 关键校验函数应有**主循环 + 全局 sanity check** 的双层结构, 单层校验在边角场景下容易被绕过。
- 处理 anthropic `tool_use ids without tool_result blocks immediately after` 类 cascade failover 时, **adaptations 标签能否复现日志**是定位 root cause 的强信号: 若 enforce 视角与 anthropic 视角不一致 (有 misplaced 但无 orphan, anthropic 仍报错), 必有上游 _rewrite / id 改写阶段的隐藏漏洞。

**同类问题影响与处理注意事项**

- 任何对 messages 进行 ID 重写的转换链 (如 `_rewrite_srvtoolu_ids`、`anthropic_to_openai`、`anthropic_to_gemini`) 都应使用两遍扫描或一次性收集后再批量改写, 以保证 block 顺序无关性。
- enforce 类校验函数若依赖 dict key 与 list 元素的**等同性**, 必须先确保两者在同一参考系下 (改名前 vs 改名后); 否则错位会以 "看起来 OK 实际有漏" 的方式静默泄漏到下游。
