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
