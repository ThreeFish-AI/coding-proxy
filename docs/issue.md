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
