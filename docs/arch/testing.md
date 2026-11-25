# 测试策略

> **定位**：从 `framework.md` 提取，详述测试覆盖与工具链。

[TOC]

---

## 1. 测试工具

- **pytest** (>=9.0) — 测试框架
- **pytest-asyncio** (>=1.3) — 异步测试支持
- **monkeypatch** — 环境变量和工作目录隔离
- **tmp_path** — 临时文件测试
- **respx** — httpx Mock（用于 vendor 集成测试）

---

## 2. 测试覆盖

### 2.1 供应商（vendors）

| 测试文件                   | 覆盖范围                                                        |
| -------------------------- | --------------------------------------------------------------- |
| `test_vendors.py`          | 请求头过滤、模型映射、故障转移判断、数据类默认值                |
| `test_copilot.py`          | CopilotTokenManager 交换/缓存/过期/失效、CopilotVendor 请求准备 |
| `test_antigravity.py`      | GoogleOAuthTokenManager 刷新/缓存/过期/失效、格式转换+token注入 |
| `test_native_vendors.py`   | NativeAnthropicVendor 基类行为、401 归一化、子类继承            |
| `test_zhipu.py`            | ZhipuVendor 特定行为                                            |
| `test_mixins.py`           | TokenBackendMixin 行为                                          |
| `test_token_manager.py`    | BaseTokenManager 抽象行为                                       |
| `test_copilot_models.py`   | CopilotModelResolver 模型解析与误导向处理                       |
| `test_copilot_urls.py`     | Copilot URL 工具函数                                            |
| `test_vendor_streaming.py` | Vendor 流式响应行为                                             |

### 2.2 路由（routing）

| 测试文件                   | 覆盖范围                                                               |
| -------------------------- | ---------------------------------------------------------------------- |
| `test_circuit_breaker.py`  | 状态转换（CLOSED→OPEN→HALF_OPEN→CLOSED）、恢复超时、指数退避、手动重置 |
| `test_quota_guard.py`      | 配额守卫状态机、预算追踪、探测机制、基线加载                           |
| `test_model_mapper.py`     | 精确匹配、正则匹配、Glob 匹配、默认回退、空规则集                      |
| `test_tier.py`             | VendorTier 可执行判断、成功/失败记录、终端判定、三层门控、RL deadline  |
| `test_router_chain.py`     | N-tier 链式路由（2/3/4-tier 降级、CB/QG 跳过、流式/非流式、连接异常）  |
| `test_router_executor.py`  | _RouteExecutor 门控逻辑、能力匹配、兼容性决策、语义拒绝                |
| `test_error_classifier.py` | 请求能力画像提取、语义拒绝判定、错误 payload 解析                      |
| `test_rate_limit.py`       | Rate limit header 解析、deadline 计算、cap error 检测                  |
| `test_retry.py`            | RetryConfig 参数、delay 计算、可重试异常判定                           |
| `test_usage_recorder.py`   | UsageRecorder 用量构建、定价日志、evidence 记录                        |

### 2.3 配置（config）

| 测试文件                  | 覆盖范围                                                                   |
| ------------------------- | -------------------------------------------------------------------------- |
| `test_config_store.py`    | config.db 0600 创建、事务与 revision、409 冲突、损坏/占用错误分类、隔离守卫 |
| `test_config_secrets.py`  | 密钥三态、Fernet 加解密、key_check canary、主密钥不匹配                    |
| `test_config_normalize.py`| `$tmpl` 归一化、最小化覆盖、vendors 存储结构                                |
| `test_config_loader.py`   | 旧 YAML 搜索优先级、环境变量展开、缺失文件处理、vendors 格式解析（导入基准）|
| `test_config_import.py`   | 一次性导入等价矩阵（首启全量 / 仅 vendors / legacy flat / alias / ¥ 定价）  |
| `test_config_service.py`  | 读写服务、边界校验、密钥 op、目标地址联动、错误不回显 input                 |
| `test_config_ui_registry.py` | 字段注册表覆盖门（非 legacy 叶子字段必须在 REGISTRY 或 HIDDEN）          |
| `test_config_api.py`      | GET/PUT /api/config、admin token 鉴权、409/422/503 映射                    |
| `test_config_cli.py`      | `config path/export/import/set/reset/set-secret/token`                    |
| `test_config_init.py`     | 配置模块初始化与 re-export 验证                                            |
| `test_schema.py`          | ProxyConfig 校验、legacy 迁移、tiers 引用校验、vendor 专属字段 warning     |

### 2.4 格式转换（convert）

| 测试文件                           | 覆盖范围                                                                  |
| ---------------------------------- | ------------------------------------------------------------------------- |
| `test_convert_request.py`          | Anthropic→Gemini 请求转换（文本、多轮、system、图片、工具、参数映射）     |
| `test_convert_response.py`         | Gemini→Anthropic 响应转换（文本、多部件、usage 提取、finishReason 映射）  |
| `test_convert_sse.py`              | Gemini SSE→Anthropic SSE 流适配（单/多 chunk、各 finishReason、边界情况） |
| `test_copilot_convert_request.py`  | Anthropic→OpenAI 请求格式转换                                             |
| `test_copilot_convert_response.py` | OpenAI→Anthropic 响应格式转换                                             |
| `test_vendor_channels.py`          | 源→目标通道：zhipu/copilot 兼容性清洗、tool_use 配对、内容感知源推断      |

### 2.5 数据模型（model）

| 测试文件                  | 覆盖范围                                            |
| ------------------------- | --------------------------------------------------- |
| `test_model_vendor.py`    | UsageInfo/VendorResponse/RequestCapabilities 数据类 |
| `test_model_compat.py`    | CanonicalRequest/CompatibilityDecision 数据模型     |
| `test_model_constants.py` | 常量定义与使用                                      |
| `test_model_pricing.py`   | ModelPricingEntry 校验、币种一致性                  |
| `test_model_token.py`     | Token 相关模型                                      |
| `test_model_auth.py`      | 认证相关模型                                        |

### 2.6 认证（auth）

| 测试文件                 | 覆盖范围                                          |
| ------------------------ | ------------------------------------------------- |
| `test_runtime_reauth.py` | RuntimeReauthCoordinator 状态机、幂等触发、锁保护 |
| `test_auto_login.py`     | 自动登录流程                                      |

### 2.7 流式处理（streaming）

| 测试文件                             | 覆盖范围                 |
| ------------------------------------ | ------------------------ |
| `test_streaming_anthropic_compat.py` | Anthropic 流式兼容层行为 |

### 2.8 服务端与 CLI（server/cli）

| 测试文件                     | 覆盖范围                                                |
| ---------------------------- | ------------------------------------------------------- |
| `test_app_routes.py`         | FastAPI 路由端点测试                                    |
| `test_cli_usage.py`          | CLI 用量查询命令                                        |
| `test_banner.py`             | CLI Banner 显示                                         |
| `test_logging_dual_write.py` | 日志双写机制                                            |
| `test_generations.py`        | GenerationManager 单元测试：build/activate/swap、bootstrap pin、degraded 启动回退、排空与关停 |
| `test_generations_app.py`    | 真实 app/store 下的 PUT 热切换、流式跨切换、会话绑定迁移、真实 uvicorn |
| `test_generations_cli.py`    | CLI `start` 与 GenerationManager 接线、Dashboard URL 提示、degraded 启动 |
| `test_security_middleware.py`| L1 跨站请求拒绝 / L2 管理面 Host 白名单 / 响应安全头加固 |
| `test_dashboard_config_tab.py`| Config 页 HTML 接线、CSP/XSS 静态检查与 config.js Node harness |
| `test_dashboard_subscription_card.py`| 订阅状态卡片 HTML 接线、XSS 静态检查、归并键一致性、单行 CSS 约束静态断言、合并渲染与拖拽状态机 Node harness（三类行 / 行序 / 用量格语法与分组排序 / 档位去重 / 并发守卫 / 列表外松开 / 多指 / 键盘调整 / 焦点保持 / 迟到快照 / 失败回滚 / 重绘保留滚动位置） |
| `test_dashboard_charts.py`| 图表空态：canvas 恒留在 DOM 中，无数据与有数据之间反复刷新不抛错（静态检查 + Node harness） |
| `test_subscription_status.py`| 订阅状态聚合：被动信号解析、两家 fetcher（含模型族短名与上游缺字段容错）、TTL 服务、端点集成 |

### 2.9 其他

| 测试文件               | 覆盖范围                                                         |
| ---------------------- | ---------------------------------------------------------------- |
| `test_pricing.py`      | PricingTable 加载、单价查询（精确+规范化）、费用计算、币种一致性 |
| `test_token_logger.py` | 用量记录、窗口查询、按供应商/模型过滤、evidence 记录             |
| `test_compat.py`       | CanonicalRequest 构建、session_key 派生                          |
| `test_parse_usage.py`  | 用量解析工具函数                                                 |
| `test_currency.py`     | 币种检测与转换                                                   |
| `test_types.py`        | 公共类型定义                                                     |
| `test_time_range.py`   | 时间范围工具                                                     |
| `test_tiers_config.py` | Tiers 配置验证                                                   |
