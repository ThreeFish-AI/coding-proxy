# Antigravity IDE 作为客户端接入 coding-proxy 的可行性调研与验证

> 调研日期：2026-11-15 · 代码基线：`abalone` worktree（v0.5.2 后）· 方法：3 路代码深读 + 3 路外部调研 + 2 组本地真机实测（mock 上游 + 真实 proxy 进程 + 真实 google-genai SDK）

## 1. 裁决结论

**可行，且核心能力已经存在**：coding-proxy 自带的 `native_api` 透传子包（`/api/{openai,gemini,anthropic}/**` catch-all，[src/coding/proxy/native_api/](../../src/coding/proxy/native_api/)）无需任何协议转换代码即可承接 Antigravity 的两类官方客户端流量，本地代理、上游透传、用量统计三件事全部实测通过。Antigravity 官方不存在个人 BYOK 入口（官方 plans 文档明文 [1]），但存在三条真实接入通道（见 §2）；其中两条（enterprise gateway 的 `wireProtocol=openai`、agy CLI 的 Gemini 原生模式）把 base_url 指到 coding-proxy 即接入，**零代码改动**；第三条（接管默认 agent 的 `CLOUD_CODE_URL`）需要新增一个 v1internal 信封通道（P2 工作量）。与 Claude Code 同级的 failover/熔断/会话绑定等特性不在「透传」语义内，属可选增强（§5 P3）。

需要用户拍板的三个决策点：通道取舍（§2）、流式 usage 统计补救方式（§5 P1，涉及「是否允许注入 `stream_options.include_usage` 改写请求体」的透传原则权衡）、是否新增 Antigravity 客户端身份维度（§5 P2）。

## 2. 接入通道裁决矩阵

Antigravity 客户端面与协议事实（外部调研，信源见 §7）：

| # | 通道 | 客户端面 | 线协议 | 接入 coding-proxy 的方式 | 官方性 | 裁决 |
| --- | --- | --- | --- | --- | --- | --- |
| ① | Enterprise LLM Gateway（`wireProtocol=openai`） | App ≥ 2.15.0 / CLI ≥ 1.2.15 / VS Code 扩展；JetBrains/Zed 仅支持 gemini wire；Web 版无 gateway | 标准 OpenAI Chat Completions（`POST {gateway.url}/v1/chat/completions` + SSE + Bearer，客户端自动拼 `/v1` 后缀） | `gateway.url = http://127.0.0.1:3392/api/openai`，配法：MDM `admin_settings.json` / `AGY_LLM_GATEWAY_*` env / 用户级 `~/.antigravity/settings.json` | 官方 enterprise 机制（Portkey 已官方集成 [2]）；**无 admin 权限时用户级配置是否生效仅有单一信源，需实机验证** | 首选，零代码（P1 统计补强） |
| ② | agy CLI Gemini API key 模式 | 仅 Antigravity CLI（`modelProvider=gemini` + `GEMINI_API_KEY`） | Gemini 原生（`models/{model}:generateContent` / `:streamGenerateContent?alt=sse`） | `GOOGLE_GEMINI_BASE_URL = http://127.0.0.1:3392/api/gemini` | 官方文档化配置 [3] | 零代码，**用量统计质量最高的通道**（实测 11 项全过，§4） |
| ③ | `CLOUD_CODE_URL` 环境变量接管默认 agent | 默认 Gemini agent 的全部模型流（桌面 + CLI，订阅账号） | Cloud Code `v1internal` 私有信封：请求为 Gemini `GenerateContentRequest` 外包 `{project:"aicode-consumers", requestId, model, userAgent, requestType, request}`；响应（流式 SSE 帧与非流式 JSON 同构）外包 `{response, traceId, metadata}`。端点为模型面 3 个（`:generateContent` / `:streamGenerateContent?alt=sse` / `:countTokens`）+ 控制面 7 个（`:loadCodeAssist` / `:fetchUserInfo` / `:retrieveUserQuotaSummary` / `:listExperiments` / `:fetchAdminControls` / `:fetchAvailableModels` / `:writeTrajectoryAcls`），全部 POST 直挂在 `CLOUD_CODE_URL` 之后（model 名在信封 `body.model`，不在 URL）。会话身份在内层 `request.systemInstruction` 的 `Conversation ID: <uuid>` 标签（信封 `request.sessionId` 恒为 FNV 常量不可用）；SSE 帧必须带 `{response, ...}` 信封，纯 verbatim 不拆包则天然满足（bili 重封仅因它解包改写内容）[4][5] | 需在 coding-proxy 新增 v1internal 通道：路由 + 信封旁路解析统计（Conversation ID → session_key） | **未文档化 hook**（二进制逆向发现 [4]，社区 bili 真机验证 [5]）；Google 官方口径「endpoint 不支持 override」且默认上游域名已发生过版本间漂移（`cloudcode-pa` → `daily-cloudcode-pa`）[6]；macOS 桌面端无专门注入机制（需从带 env 的环境启动 IDE 或 MITM），桌面真机样本为 Windows [4][5] | P2 中等工作量；覆盖「订阅账号全量流量」的唯一无 patch 通道，维护成本最高 |
| ④ | HTTPS_PROXY + 企业 CA MITM | 同③ | 同③ | 超出 coding-proxy 产品形态（明文反代非 MITM CA 代理） | 官方文档明示支持企业 inspected TLS、无证书钉扎 [7] | 不推荐，仅作③被移除后的理论兜底 |
| ⑤ | 社区 IDE patch（antigravity-add-model 等） | IDE 内自定义模型 UI | openai/anthropic/google 三种 wire | patch 在 IDE 侧注入自定义模型，其 `apiUrl` 可直接填 coding-proxy 的 `/api/openai` 或 `/api/anthropic` | 非官方（binary patch，按版本维护）[8] | 不作为 coding-proxy 依赖项；但证明生态需求真实（193★/37★），可作兼容性参考 |

对①②的关键结论：两条通道的流量形态与 `native_api` 现有三个 catch-all 端点**逐字段对齐**（①对应 `/api/openai/{rest:path}`，②对应 `/api/gemini/{rest:path}`），透传语义（不转协议、不保管凭据、字节级转发）与用户命题严格一致。上游侧：①②的透传目标默认分别是 `api.openai.com` 与 `generativelanguage.googleapis.com`（[src/coding/proxy/native_api/config.py:60-77](../../src/coding/proxy/native_api/config.py)），可用 `NATIVE_{PROVIDER}_BASE_URL` env 或 YAML 覆写指向任意同协议网关。

## 3. 现有能力盘点（对齐 Claude Code 特性基线）

Claude Code 基线五层：透明接入、会话识别与标题、会话级供应商固定、N 级故障转移（熔断/配额/限流）、统一用量统计。`native_api` 链路与 `/v1/messages` 链路**共用同一 `TokenLogger`/SQLite/dashboard**（`client_category='cc'|'api'` 拆行共存，[src/coding/proxy/server/app.py:97,175-186](../../src/coding/proxy/server/app.py)），存储与呈现层天然统一。

| Claude Code 基线特性 | native_api 现状 | 证据 |
| --- | --- | --- |
| 透明接入（改一个 base_url） | ✅ 已覆盖：三家 provider 默认 `enabled=true`，SDK `base_url` 指向 `http://127.0.0.1:3392/api/{provider}` 即用 | [src/coding/proxy/native_api/config.py:33-36](../../src/coding/proxy/native_api/config.py) |
| 上游透传（不转换协议） | ✅ 已覆盖：path（含 `:verb` 与 `%3A` 编码）/query/认证头/body/SSE 字节级转发，上游状态码与错误体原样返回，proxy 不保管凭据 | [src/coding/proxy/native_api/handler.py:46-78,178-231](../../src/coding/proxy/native_api/handler.py)；测试 `tests/test_native_api_handler.py::test_sse_passthrough_byte_identical` |
| 用量统计（四类 token/duration/model/operation/endpoint/extra_usage） | ✅ 已覆盖：`usage_log` 17 列落账 + `usage_evidence` 全量归档（含 `usageMetadata` 的 `thoughtsTokenCount` 等进 `extra_usage_json`） | [src/coding/proxy/native_api/handler.py:683-729](../../src/coding/proxy/native_api/handler.py)；[src/coding/proxy/routing/usage_recorder.py:95-168](../../src/coding/proxy/routing/usage_recorder.py) |
| dashboard 混合视图 | ✅ 已覆盖：`client_category='api'` 流量进 summary/timeline，sessions 视图按标签渲染 | [src/coding/proxy/server/dashboard.py:1082-1094,2536-2584](../../src/coding/proxy/server/dashboard.py) |
| 非 chat 端点（models 列表/countTokens/embeddings） | ✅ 透传 + 审计行落账（`operation=model.list` 等） | [src/coding/proxy/native_api/operation.py:28-129](../../src/coding/proxy/native_api/operation.py) |
| 会话维度（session_key/标题/sessions 视图） | ❌ 未覆盖：native 流量 `session_key` 恒空，sessions 视图对其不可见 | [src/coding/proxy/native_api/handler.py:706-722](../../src/coding/proxy/native_api/handler.py)；`logging/db.py` sessions 查询过滤 `session_key != ''` |
| 客户端身份区分 | ❌ 未覆盖：`client_category` 仅 `cc|api` 两值，无法区分 Antigravity 与其他 native 客户端 | `logging/db.py` DDL；[docs 通道①③的入口指纹（UA `Antigravity/x.x Electron`、`x-client-name: antigravity`）已在 vendor 侧作为出口指纹掌握](../../src/coding/proxy/vendors/antigravity.py)，尚无入口检测 |
| N 级故障转移/熔断/配额/并发管理 | ❌ 未覆盖：native 链路纯单上游，上游不可达直接 502 | [src/coding/proxy/server/app.py:114-166](../../src/coding/proxy/server/app.py)；handler 无 semaphore/CB |
| 模型映射（model_mapper） | ❌ 未覆盖：`model_requested = model_served` | handler.py:706-711 |
| 流式 usage 兜底（上游不回 usage 时） | ❌ 缺失：OpenAI 面流式若上游不发 usage chunk 则记 0/0 且 `model_served='unknown'`；全仓无 `stream_options.include_usage` 注入（注入点仅存在于反方向转换链路） | handler.py:292-301；[src/coding/proxy/convert/anthropic_to_openai.py:72](../../src/coding/proxy/convert/anthropic_to_openai.py) |
| 成本统计 | ⚠️ 部分：gemini/openai vendor 需配 `model_pricing` 条目才有 cost；`thoughtsTokenCount` 等不计价（no-op 钩子） | [src/coding/proxy/pricing.py:64-116](../../src/coding/proxy/pricing.py) |

既有 `vendors/antigravity.py`（反方向：Antigravity 订阅账号当上游供 Claude Code 用）与本命题方向相反、互不冲突，但它沉淀了 Antigravity 真实后端协议的完整知识：v1internal 端点/信封/指纹头/OAuth 刷新，正是通道③实施时的仓内参照（[src/coding/proxy/vendors/antigravity.py:36-43,392-426](../../src/coding/proxy/vendors/antigravity.py)，自述与逆向项目 Antigravity-Manager 对齐）。

## 4. 本地实测验证

两组实测均在隔离环境完成（mock 上游 + `uv run coding-proxy start` 真实进程 + SQLite 指到临时目录，未触碰 `~/.coding-proxy`，进程与端口均已清理）。

**Gemini 原生面（通道②形态）：11 用例全 pass**，含真实 `google-genai` SDK 3.3.0 端到端（客户端完整解析经 proxy 透传的响应与 usage）。关键证据：

- `generateContent`/`streamGenerateContent?alt=sse`/`countTokens`/ListModels 的 body、query（`?key=` 认证）、认证头（`x-goog-api-key`）、`:verb` 冒号路径与 `%3A` 编码全部保真抵达上游；
- `usageMetadata` 全字段落账：流式末帧 `promptTokenCount/candidatesTokenCount/thoughtsTokenCount` 正确入 `usage_log`（in=42/out=13，thoughts=17 进 `extra_usage_json`）；
- dashboard summary 与 `usage_log` 手工累加逐项一致（input 117/output 45/cache_read 6）；
- 嵌套 OpenAI 面 `/api/gemini/v1beta/openai/chat/completions` 亦透传可用，usage 按 OpenAI 字段形态正确解析；
- 上游 404 原样返回，记 `success=0` 审计行。

**OpenAI 兼容面（通道①形态）：8 用例，透传全 pass，用量统计一项 conditional**：

- 非流式、流式 SSE、`tool_calls` 流式分片、DeepSeek 风格 `reasoning_content`、自定义业务头、`Authorization: Bearer` 全部逐字节透传抵达上游（mock 侧 headers 逐条比对确认）；
- `GET /v1/models` 透传并落 `operation=model.list` 审计行；
- 用量统计：非流式恒可记账（prompt/completion/cached/reasoning 全字段正确映射，含 `source_field_map_json` 证据）；流式**仅当上游发出 usage chunk**（`stream_options.include_usage` 或 DeepSeek 式末 chunk 自带 usage）才记账——若 Antigravity 客户端不发 `include_usage` 且上游是标准 OpenAI 行为，则流式 tokens 记 0 且无估算兜底（`usage_evidence` 亦无行）。这是通道①接入前必须确认/补救的第一优先项；
- 流式与 非流式的 `prompt_tokens_details.cached_tokens`/`completion_tokens_details.reasoning_tokens` 映射口径不一致：流式只进 evidence 不进规范列（[src/coding/proxy/routing/usage_parser.py:194-215](../../src/coding/proxy/routing/usage_parser.py) vs [src/coding/proxy/native_api/extractors/openai.py:46-69](../../src/coding/proxy/native_api/extractors/openai.py)），流式成本口径偏低。

实测边界：两组均未用真实 Antigravity IDE/CLI 端到端（本机无安装），请求形态按外部调研结论建模（通道①客户端在 `gateway.url` 后自动拼 `/v1/chat/completions`，与实测路径一致）；未对真实 `generativelanguage.googleapis.com` 回放（企业网络限制）。

## 5. 缺口与实施分级（2026-11-15 已批准全量实施，P0-P2 已落地）

| 级别 | 内容 | 工作量估计 | 说明 |
| --- | --- | --- | --- |
| P0 | 文档化「Antigravity 接入指南」：通道①②的一行配置（`gateway.url`/`GOOGLE_GEMINI_BASE_URL` 指向 `/api/openai`/`/api/gemini`）、限制说明（JetBrains/Zed/Web 不适用）、`docs/guide/api-reference.md` 补齐 native_api 端点文档 | ~0.5 天 | 零代码，现有能力即刻可用 |
| P1 | 统计补强：(a) OpenAI 面流式 usage 补救（`stream_options.include_usage` 注入，需配置开关且默认关，涉及透传原则的决策点；或上游侧约定）；(b) 流式 details 字段映射对齐非流式；(c) gemini 定价条目按现行模型名补齐（`gemini-4-carbon/argon/hydrogen`、`gemini-3.8-flash` 等 [9]）；(d) 通道①用户级 settings.json 生效性实机验证 | ~2-3 天 | 全部是既有链路的增量补丁 |
| P2 | （若需覆盖默认 agent 订阅流量）v1internal 通道：`/api/antigravity` 路由 + 信封旁路解析统计（`body.model` → model，`systemInstruction` 的 `Conversation ID` → session_key，SSE 帧 `response.usageMetadata` → tokens；纯 verbatim 转发不改写）+ `client_category='antigravity'` 身份维度 | ~5-8 天 | 仓内 `vendors/antigravity.py` + bili + Antigravity-Manager 三方参照；非官方契约，需按「版本漂移跟进」定价维护成本；SSE 解析须按事件语义聚合多行 `data:` 帧与 CRLF [5] |
| P3 | （可选）会话维度、native 链路 failover/多上游轮换、模型映射 | 另议 | 与「透传不转换」存在架构张力，收益需单独评估 |

风险清单：通道③的 hook 未获 Google 支持承诺且默认上游域名已漂移过一次 [6]；Google 明示勿改 IDE 二进制（env var 属灰色地带，ToS 风险自担）[6]；通道①用户级 gateway 配置未经官方文档确认 [2]；Antigravity 第三方官方模型目录在快速变动（Claude 4.6/GPT-OSS-120b 定于 2026-11-18 下线 [10]）；Gemini OpenAI 兼容面自述 beta，图像参数静默忽略等子集限制 [11]。

## 6. 结论与实施状态

用户命题的三件事在通道①②上**零代码即成立**（实测背书）；通道③补齐「默认 agent + 订阅账号」场景，以持续维护成本为代价。

**实施决策（2026-11-15，已批准并落地）**：按 P0 + P1 + P2 全量实施——新增 `antigravity_gateway` 通道（`/api/antigravity/**`，`client_category='antigravity'`，session 维度取自请求信封 `Conversation ID`）、共享透传内核 `passthrough_kernel`、OpenAI 面 `inject_include_usage` opt-in 开关、流式 details 字段映射、gemini 现行模型定价条目、四处文档。实现细节见 [配置字段参考 — antigravity_gateway](../arch/config-reference.md#11-antigravity_gateway--antigravity-客户端透传配置) 与 [用户指引 § 4.8](../user-guide.md#48-antigravity_gateway--antigravity-ide-客户端透传)；P3（native 链路 failover/会话标题等）未实施，另行立项。待办：真机验证（用户侧 `export CLOUD_CODE_URL` 后跑一轮对话核对 dashboard 落账）。

## 7. 参考文献

[1] Google, "Antigravity plans and rate limits," antigravity.google. [Online]. Available: https://antigravity.google/docs/plans. [Accessed: 2026-11-15].

[2] Portkey (Prisma AIRS AI Gateway), "Antigravity integration: LLM gateway admin settings and wire protocols," portkey.ai. [Online]. Available: https://portkey.ai/docs/integrations/libraries/antigravity. [Accessed: 2026-11-15].

[3] Google, "Antigravity settings: modelProvider, GEMINI_API_KEY, GOOGLE_GEMINI_BASE_URL, customModelsConfig," antigravity.google. [Online]. Available: https://antigravity.google/docs/settings. [Accessed: 2026-11-15].

[4] ranxianglei et al., "billion-context (bili) docs/clients.md: undocumented CLOUD_CODE_URL override verified in Antigravity v2.19.1 binary," GitHub. [Online]. Available: https://github.com/ranxianglei/billion-context/blob/master/docs/clients.md. [Accessed: 2026-11-15].

[5] ranxianglei et al., "bili issue #3129: Antigravity 真机抓包与 v1internal 桥接验证（信封/SSE 重封包/修复记录）," GitHub. [Online]. Available: https://github.com/ranxianglei/billion-context/issues/3129. [Accessed: 2026-11-15].

[6] Google AI Developer Forum, "Bug report: v2.28.x routes all requests to daily-cloudcode-pa.googleapis.com; Google staff response on endpoint override policy," discuss.ai.google.dev. [Online]. Available: https://discuss.ai.google.dev/t/bug-report-v2-28-x-routes-all-requests-to-daily-cloudcode-pa-googleapis-com-staging-endpoint-persistent-503-capacity-failures-with-gemini-3-8-flash-high/198078. [Accessed: 2026-11-15].

[7] Google Cloud, "Deploy Antigravity for US public sector: system proxy and OS trust store TLS behavior," docs.cloud.google.com. [Online]. Available: https://docs.cloud.google.com/docs/security/compliance/deploy-antigravity-us-public-sector. [Accessed: 2026-11-15].

[8] vahapogut, "antigravity-add-model: IDE patch adding custom models (custom_models.json, openai/anthropic/google wire)," GitHub. [Online]. Available: https://github.com/vahapogut/antigravity-add-model. [Accessed: 2026-11-15]. 另见 fork Aminetwiti/antigravity-patch-proxy-remote.

[9] Google, "Gemini API models and deprecations," aistudio.google.com. [Online]. Available: https://aistudio.google.com/docs/models 与 https://aistudio.google.com/docs/deprecations. [Accessed: 2026-11-15].

[10] Google, "Antigravity available models and removal schedule," antigravity.google. [Online]. Available: https://antigravity.google/docs/models/. [Accessed: 2026-11-15].

[11] Google, "Gemini API OpenAI compatibility," aistudio.google.com. [Online]. Available: https://aistudio.google.com/docs/openai. [Accessed: 2026-11-15]. 另见原生面参考 https://aistudio.google.com/api-reference/generate-content/generate-content.

[12] Google, "Antigravity SDK local models: LocalOpenAIAgentConfig," antigravity.google. [Online]. Available: https://antigravity.google/docs/sdk/local-models. [Accessed: 2026-11-15].
