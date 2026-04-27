# Issue 处理档案

> 维护已处理过的 Issue 摘要（问题描述、表因根因、处理方式、后续防范、同类问题影响与处理注意事项），便于同类问题的跨上下文处理。识别相同 Issue 时应在原条目追加复盘，避免同 Issue 多处维护。

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
