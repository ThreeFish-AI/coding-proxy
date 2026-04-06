# Changelog

本文件基于 [Keep a Changelog](https://keepachangelog.com/zh-CN/) 规范维护，版本号遵循 [语义化版本](https://semver.org/lang/zh-CN/)。

## [Unreleased]

## [v0.1.2](https://github.com/ThreeFish-AI/coding-proxy/releases/tag/v0.1.2) — 2026-04-06

> [!IMPORTANT]
>
> **🔓 count_tokens 终于不再"偏心" Anthropic 了！**
>
> 当 Zhipu GLM 作为主供应商时，Token 计数接口终于告别 403 报错，全面拥抱多供应商泛化透传。配合全局活跃状态追踪机制，count_tokens 能智能跟随当前实际在用的供应商，熔断降级？无缝切换，零感知！

### ✨ 核心亮点

- **count_tokens 多供应商泛化透传**：🎯 告别硬编码 Anthropic 独占！引入 `_find_count_tokens_vendor()` 全局活跃状态感知函数，优先读取 Executor 成功响应时写入的当前活跃供应商名称，冷启动时优雅回退到 `tiers[0]`。Zhipu / Anthropic 及所有兼容 Anthropic 协议的供应商均可正确承接 Token 计数请求，上游错误原样透传，无可用供应商时返回语义清晰的 `501 Not Implemented`（替代原先不准确的 `404`）；
- **全局活跃供应商状态追踪**：🧠 Router 新增 `_active_vendor_name` 属性，Executor 在每次流式/非流式请求成功后自动写入当前活跃供应商名称。这意味着 count_tokens 不再盲目猜测，而是精准锁定"此刻谁在干活"，完美适配熔断降级等动态切换场景；

### 🔧 更多特性

- 📐 **配置文件逻辑分组优化**：将 `logging` 和 `tiers` 配置项从文件末尾提至顶部（server 配置之后、vendors 定义之前），形成「全局配置 → 降级策略 → 供应商定义 → 故障转移 → 其他」的清晰层次结构，配置值完全不变，阅读体验大幅提升；
- 🧪 **count_tokens 测试矩阵扩容**：新增 5 个测试用例覆盖泛化功能全链路 —— 无可用供应商返回 501、Zhipu 主供应商转发成功、Zhipu 上游错误透传、全局活跃状态跟随（模拟熔断降级场景）、冷启动回退到 tiers[0]，信心拉满；
- 🔧 **CI 三合一修复**：一次性根治 ruff lint 的 F821/F401 导入幽灵、formatter 行长规范对齐、以及 `_executor` 辅助函数缺少 router 必需参数导致的 TypeError，CI 绿灯常亮！

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
