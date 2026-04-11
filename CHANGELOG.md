# Changelog

本文件基于 [Keep a Changelog](https://keepachangelog.com/zh-CN/) 规范维护，版本号遵循 [语义化版本](https://semver.org/lang/zh-CN/)。

## [Unreleased]

### 🐛 Bug 修复

- **fix(antigravity)**: 修复 Google OAuth token 刷新后 scope 校验过严导致 403 `ACCESS_TOKEN_SCOPE_INSUFFICIENT` 的问题。将 `_acquire()` 中严格的 5-scope 全量校验降级为 warning 日志，与参考项目 Antigravity-Manager 行为对齐。Google OAuth2 规范允许 refresh_token 返回的 access_token 仅包含部分已授权 scope，此前因校验过于严格导致有效 token 被拒绝使用，触发熔断器。

## [v0.2.0](https://github.com/ThreeFish-AI/coding-proxy/releases/tag/v0.2.0) — 2026-04-09

> [!IMPORTANT]
>
> **🚀 供应商大扩军 × 用量仪表盘全面进化，双线暴击！**
>
> 卡在一家供应商的限额天花板下抬不起头？现在你手握 **九条命**——新增 MiniMax、小米 MiMo、阿里千问、Kimi、豆包五路援军，全部原生讲 Anthropic 话，无缝接入 N-tier。 Token 烧到哪儿心里没数？新版 `usage` 命令解锁日/周/月/全量四档视角，多供应商并排比，汇总行一行看全局。**备用仓更满，账单更透，从此宕机只是别人家的故事。**

### ✨ 核心亮点

- **5 家供应商集体入场**：MiniMax、小米 MiMo、阿里千问、Kimi、豆包（火山引擎）正式入编 N-tier。备用通道数量直接翻倍，不怕堵；
- **`usage` 命令全面升级**：从"只有天数"进化为**日 / 周 / 月 / 全量**四档时间维度（`-d 7` / `-w` / `-m` / `-t`）。支持多值过滤——`-v anthropic,kimi` 或 `--model claude-opus-4-6,glm-5.1` 用逗号隔开随便选。表格末行自动追加**汇总行**，请求总量、Token 总计、总成本、加权平均延迟四项一览无余。Token 花在哪家、烧了多少、谁最能扛——这张表给你答案；

### 🔧 更多特性

- **品牌横幅正式上线**：`proxy start` 启动时打印 Coding Proxy 专属 ASCII Banner 与版本号，告别冷冰冰的裸日志起手式；
- **529 过载纳入降级触发**：HTTP `529 overloaded_error` 正式加入故障转移白名单，Anthropic 喊"我堵了"时 Proxy 不再干等；
- **Zhipu 跨供应商级联故障根治**：`Internal Network Failure` 纳入 500 降级条件；`tool_result` 角色错位导致的下游级联崩溃彻底斩断，再也不因历史 message 的"历史遗留问题"把整条链拖下水；

## [v0.1.3](https://github.com/ThreeFish-AI/coding-proxy/releases/tag/v0.1.3) — 2026-04-07

> [!IMPORTANT]
>
> **🔥 跨供应商"身份危机" + 熔断器"装死"双杀！**
>
> Zhipu 的 thinking blocks 偷渡到 Anthropic 被当场识破 → 400 无限循环降级？斩了。429 限流后熔断器嘴上说"我没事"身体却已躺平？修了。两大隐蔽 Bug 一锅端，跨供应商丝滑切换从此告别"薛定谔的可用性"。

### ✨ 核心亮点

- **Thinking Blocks "安检门"**：Anthropic 对请求体 deepcopy 后，**精准剥离** assistant messages 中的 `thinking` / `redacted_thinking` blocks。Zhipu → Anthropic 迁移时历史思考签名不再越界，400 `invalid_request_error` 彻底根除，其他供应商零影响；
- **熔断器 Force-Open 闪电响应**：为 `record_failure()` 新增 `force_open` 参数——当检测到 429/403 携带 `retry_after_seconds`（即 Rate Limit 硬信号）时，**跳过累积阈值直接 OPEN**，状态展示与实际可用性分秒对齐；非 429 错误（5xx、超时等）保持原有累积行为不变。

## [v0.1.2](https://github.com/ThreeFish-AI/coding-proxy/releases/tag/v0.1.2) — 2026-04-06

> [!IMPORTANT]
>
> **🔓 count_tokens 终于不再"偏心" Anthropic 了！**
>
> 全面拥抱多供应商泛化透传。配合全局活跃 Vendor 状态追踪机制，智能跟随 Vendor 当前移位，熔断降级？无缝切换，零感知！

### ✨ 核心亮点

- **全局活跃 Vendor 状态追踪**：🧠 Router 新增活跃 Vendor 属性，Executor 在每次流式/非流式请求成功后自动写入当前活跃供应商名称。精准锁定"此刻谁在干活"，完美适配熔断降级等动态切换场景；

### 🔧 更多特性

- 🔧 **CI 三合一修复**：一次性根治 ruff lint 的 F821/F401 导入幽灵、formatter 行长规范对齐，CI 绿灯常亮！

## [v0.1.1](https://github.com/ThreeFish-AI/coding-proxy/releases/tag/v0.1.1) — 2026-04-05

> [!IMPORTANT]
> 
> **🎉 coding-proxy MVP 惊艳登场！** 
> 
> 仅需配置一行环境变量，立刻为你的 Claude Code 接入“永不宕机”的多源智能引擎。主供应商打盹？毫秒级自动无缝切换备用通道，全天候护航你的编码心流，向打断大声说不！

### ✨ 核心亮点

- **N-tier 高可用接力**：随心编排供应商优先级；默认内置 `Claude → GitHub Copilot → Antigravity → GLM` 丝滑降级链路，天塌下来有 Proxy 顶着；
- **自愈式智能熔断**：微秒级状态机严防“雪崩效应”，搭配指数退避重试，一旦主干回血，静默自愈切回；
- **账单刺客克星**：极客专属的 SQLite 本地账本 + CLI 多维看板（按维度：日/模型/供应商），把 Token 消耗拆解到每一比特，精打细算不背锅；
- **OAuth2 丝滑接入**：原生集成 GitHub Device Flow 与 Google OAuth。告别干枯的断更密钥，令牌到期自动接力轮转，专注写码不分心；
- **多协议“同传专家”**：Anthropic 与 OpenAI / Gemini 协议底层双向无损翻译，鸡同鸭讲？在 proxy 层是不存在的；
- **模型指名道姓**：随需定制你的神级转发地图，`claude-opus-*` 秒变 `glm-5v-turbo`，指哪打哪，模型矩阵全由你做主；
- **全透明“隐身衣”**：FastAPI 强劲异步驱动，开箱即用。仅需覆盖注入 `ANTHROPIC_BASE_URL`，对上层应用百分百零侵入、零违和；
- **SSE 星际流水线**：彻底打破协议壁垒，流式连线跨体系无损透传，体验每一颗 Token 如丝般顺滑的输出快感；
- **双擎配额守卫**：“5小时滑动窗口 + 固化周配额”双重护城河。余额濒临红线？主动预警机制，断然拒绝突然“断奶”；

### 🔧 更多特性

- 💰 **细粒度计价引擎**：内置主流大模型实时公开保价，调用开销追踪精确至每分每厘，资本家也薅不到你一根毛；
- 🔄 **强迫症级重试流**：深度可配的指数退避策略（不仅是次数，还有倍率），将偶发性异常全部静默拦截在黑盒之中；
- 🧠 **Vendor 降级脑图**：内置多维度供应商能力全息映射，危机时刻全自动施行“损失最小”的兼容降级路线；
- ⏱️ **RateLimit 算命仪**：智能嗅探并解析 Rate Limit Headers，精准算准每一秒 CD 冷却，弹无虚发；
- 🛡️ **神秘 421 疫苗**：专治 GitHub Copilot 偶尔抽风的著名 `421 Misdirection` 顽疾，内置“即刻重试”自愈特效药；
- 🧹 **洁癖级优雅退出**：挥一挥衣袖不带走一片云彩，挂起、清理、落数据，进程结束得干干净净，像风一样自由；
