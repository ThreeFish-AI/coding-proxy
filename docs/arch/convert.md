# 格式转换模块（convert/）

> **路径约定**：本文档中模块路径均相对于 `src/coding/proxy/`。
>
> **定位**：从 `framework.md` 提取，详述 Anthropic ↔ Gemini ↔ OpenAI 三向格式转换。

[TOC]

---

## 1. 模块总览

[`convert/`](../../src/coding/proxy/convert/) 模块提供独立的纯函数适配器层，支持三向格式转换：

| 转换方向                    | 模块                                                                                           | 说明               |
| --------------------------- | ---------------------------------------------------------------------------------------------- | ------------------ |
| Anthropic → Gemini          | [`convert/anthropic_to_gemini.py`](../../src/coding/proxy/convert/anthropic_to_gemini.py)     | 请求格式转换       |
| Gemini → Anthropic          | [`convert/gemini_to_anthropic.py`](../../src/coding/proxy/convert/gemini_to_anthropic.py)     | 响应格式转换       |
| Gemini SSE → Anthropic SSE  | [`convert/gemini_sse_adapter.py`](../../src/coding/proxy/convert/gemini_sse_adapter.py)       | 流式事件重构       |
| Anthropic → OpenAI          | [`convert/anthropic_to_openai.py`](../../src/coding/proxy/convert/anthropic_to_openai.py)     | Copilot 请求适配   |
| OpenAI → Anthropic          | [`convert/openai_to_anthropic.py`](../../src/coding/proxy/convert/openai_to_anthropic.py)     | Copilot 响应逆适配 |

---

## 2. 请求转换（Anthropic → Gemini）

**应用位置**：[`convert/anthropic_to_gemini.py`](../../src/coding/proxy/convert/anthropic_to_gemini.py) -- `convert_request()`

**转换映射**：

| Anthropic 字段                    | Gemini 字段                        | 说明                                               |
| --------------------------------- | ---------------------------------- | -------------------------------------------------- |
| `system`（str \| list）           | `systemInstruction.parts[].text`   | 支持字符串和文本块列表两种格式                     |
| `messages[]`                      | `contents[]`                       | 角色映射：`assistant` → `model`，`user` → `user`   |
| `content`（text）                 | `parts[].text`                     | 文本内容块                                         |
| `content`（image）                | `parts[].inlineData`               | Base64 数据 + MIME 类型                            |
| `content`（tool_use）             | `parts[].functionCall`             | `name` + `input` → `args`                          |
| `content`（tool_result）          | `parts[].functionResponse`         | `tool_use_id` → `name`，`content` → `result`       |
| `max_tokens`                      | `generationConfig.maxOutputTokens` |                                                    |
| `temperature` / `top_p` / `top_k` | `generationConfig.*`               | 参数名驼峰转换                                     |
| `stop_sequences`                  | `generationConfig.stopSequences`   |                                                    |

**不支持的字段**（静默剥离并记录 WARNING）：`tools`、`tool_choice`、`metadata`、`extended_thinking`、`thinking`

---

## 3. 响应转换（Gemini → Anthropic）

**应用位置**：[`convert/gemini_to_anthropic.py`](../../src/coding/proxy/convert/gemini_to_anthropic.py) -- `convert_response()` / `extract_usage()`

**finishReason 映射**：

| Gemini                            | Anthropic    |
| --------------------------------- | ------------ |
| `STOP`                            | `end_turn`   |
| `MAX_TOKENS`                      | `max_tokens` |
| `SAFETY` / `RECITATION` / `OTHER` | `end_turn`   |

**Parts 转换**：
- `text` → `{"type": "text", "text": "..."}`
- `functionCall` → `{"type": "tool_use", "id": "toolu_...", "name": "...", "input": {...}}`

**Usage 提取**：
- `usageMetadata.promptTokenCount` → `input_tokens`
- `usageMetadata.candidatesTokenCount` → `output_tokens`
- 缓存字段填 0（Gemini 不直接暴露缓存信息）

---

## 4. SSE 流适配

**应用位置**：[`convert/gemini_sse_adapter.py`](../../src/coding/proxy/convert/gemini_sse_adapter.py) -- `adapt_sse_stream()`

将 Gemini SSE 流重构为 Anthropic 消息生命周期事件序列：

```mermaid
flowchart LR
    Input["Gemini SSE chunks"] --> MS["message_start<br/>← 首次收到内容时发出"]
    MS --> CBS["content_block_start<br/>← 内容块开始"]
    CBS --> CBD["content_block_delta*<br/>← 增量文本"]
    CBD --> CBS2["content_block_stop<br/>← 内容块结束"]
    CBS2 --> MD["message_delta<br/>← stop_reason + output_tokens"]
    MD --> MSP["message_stop<br/>← 消息结束"]

    style Input fill:#1a5276,color:#fff
    style MSP fill:#196f3d,color:#fff
```

**边界情况处理**：
- 空 parts 后跟有 text 的 chunk → 延迟发出 `message_start` + `content_block_start`
- 流结束但未收到 `finishReason` → 补发默认 `message_delta`（`stop_reason: "end_turn"`）+ `message_stop`

---

## 5. OpenAI 格式转换

**应用位置**：
- [`convert/anthropic_to_openai.py`](../../src/coding/proxy/convert/anthropic_to_openai.py) -- Anthropic → OpenAI Chat Completions 请求格式
- [`convert/openai_to_anthropic.py`](../../src/coding/proxy/convert/openai_to_anthropic.py) -- OpenAI Chat Completions → Anthropic 响应格式

专为 CopilotVendor 适配，处理 Anthropic Messages API 与 OpenAI Chat Completions API 之间的双向格式差异（角色映射、工具格式、usage 字段名等）。
