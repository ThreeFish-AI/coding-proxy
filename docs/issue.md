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
