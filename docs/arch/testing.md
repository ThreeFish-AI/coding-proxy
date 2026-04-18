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

| 测试文件                | 覆盖范围                                                               |
| ----------------------- | ---------------------------------------------------------------------- |
| `test_config_loader.py` | 配置文件搜索优先级、环境变量展开、缺失文件处理、vendors 格式解析       |
| `test_config_init.py`   | 配置模块初始化与 re-export 验证                                        |
| `test_schema.py`        | ProxyConfig 校验、legacy 迁移、tiers 引用校验、vendor 专属字段 warning |

### 2.4 格式转换（convert）

| 测试文件                           | 覆盖范围                                                                  |
| ---------------------------------- | ------------------------------------------------------------------------- |
| `test_convert_request.py`          | Anthropic→Gemini 请求转换（文本、多轮、system、图片、工具、参数映射）     |
| `test_convert_response.py`         | Gemini→Anthropic 响应转换（文本、多部件、usage 提取、finishReason 映射）  |
| `test_convert_sse.py`              | Gemini SSE→Anthropic SSE 流适配（单/多 chunk、各 finishReason、边界情况） |
| `test_copilot_convert_request.py`  | Anthropic→OpenAI 请求格式转换                                             |
| `test_copilot_convert_response.py` | OpenAI→Anthropic 响应格式转换                                             |

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
| `test_request_normalizer.py` | 请求标准化：私有块清洗、tool_use_id 重写、fatal_reasons |
| `test_cli_usage.py`          | CLI 用量查询命令                                        |
| `test_banner.py`             | CLI Banner 显示                                         |
| `test_logging_dual_write.py` | 日志双写机制                                            |

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
