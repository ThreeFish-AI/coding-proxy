# 路由模块（routing/）

> **路径约定**：本文档中模块路径均相对于 `src/coding/proxy/`。
>
> **定位**：从 `framework.md` 提取，详述 N-tier 链式路由核心的 13 个子模块。

[TOC]

---

## 1. 模块总览

路由模块正交分解为 13 个子模块，每个子模块职责单一：

| 子模块              | 文件                                                               | 职责                                        |
| ------------------- | ------------------------------------------------------------------ | ------------------------------------------- |
| **router**          | [`routing/router.py`](../../src/coding/proxy/routing/router.py)    | `RequestRouter` — 路由门面（Facade）        |
| **executor**        | [`routing/executor.py`](../../src/coding/proxy/routing/executor.py)| `_RouteExecutor` — tier 迭代门控引擎        |
| **tier**            | [`routing/tier.py`](../../src/coding/proxy/routing/tier.py)        | `VendorTier` — 最小调度单元（Composite）    |
| **circuit_breaker** | [`routing/circuit_breaker.py`](../../src/coding/proxy/routing/circuit_breaker.py) | `CircuitBreaker` — 熔断器状态机 |
| **quota_guard**     | [`routing/quota_guard.py`](../../src/coding/proxy/routing/quota_guard.py) | `QuotaGuard` — 配额守卫状态机     |
| **retry**           | [`routing/retry.py`](../../src/coding/proxy/routing/retry.py)      | `RetryConfig` / `calculate_delay()` — 重试策略 |
| **rate_limit**      | [`routing/rate_limit.py`](../../src/coding/proxy/routing/rate_limit.py) | `RateLimitInfo` / 解析与截止时间计算 |
| **error_classifier**| [`routing/error_classifier.py`](../../src/coding/proxy/routing/error_classifier.py) | 请求能力画像 + 语义拒绝判定 |
| **model_mapper**    | [`routing/model_mapper.py`](../../src/coding/proxy/routing/model_mapper.py) | `ModelMapper` — 三级优先级匹配链 |
| **usage_recorder**  | [`routing/usage_recorder.py`](../../src/coding/proxy/routing/usage_recorder.py) | 用量记录、定价计算与证据构建 |
| **usage_parser**    | [`routing/usage_parser.py`](../../src/coding/proxy/routing/usage_parser.py) | SSE chunk Token 用量提取          |
| **session_manager** | [`routing/session_manager.py`](../../src/coding/proxy/routing/session_manager.py) | 兼容性会话生命周期管理            |

---

## 2. VendorTier

**文件**：[`routing/tier.py`](../../src/coding/proxy/routing/tier.py)

```python
@dataclass
class VendorTier:
    vendor: BaseVendor
    circuit_breaker: CircuitBreaker | None = None
    quota_guard: QuotaGuard | None = None
    weekly_quota_guard: QuotaGuard | None = None
    retry_config: RetryConfig | None = None
    _rate_limit_deadline: float = 0.0
```

**向后兼容别名**：`BackendTier = VendorTier`（deprecated）

**关键方法**：

| 方法                                                                     | 逻辑                                                                      |
| ------------------------------------------------------------------------ | ------------------------------------------------------------------------- |
| `name`                                                                   | → `vendor.get_name()`                                                     |
| `is_terminal`                                                            | → `circuit_breaker is None`（终端层无故障转移）                           |
| `can_execute()`                                                          | CB.can_execute() AND QG.can_use_primary() AND WQG.can_use_primary()       |
| `can_execute_with_health_check()`                                        | 三层恢复门控：Rate Limit Deadline → Health Check → Cautious Probe         |
| `record_success(usage_tokens)`                                           | CB.record_success() + QG/WQG 探测恢复 + 用量记录 + 清除 RL deadline       |
| `record_failure(is_cap_error, retry_after_seconds, rate_limit_deadline)` | CB.record_failure(+retry) + 若 cap error 则通知 QG/WQG + 更新 RL deadline |
| `is_rate_limited`                                                        | `_rate_limit_deadline > time.monotonic()`                                 |

> **设计模式**：Composite 模式，参见 [设计模式 -- Composite](./design-patterns.md#composite)

---

## 3. _RouteExecutor

**文件**：[`routing/executor.py`](../../src/coding/proxy/routing/executor.py)

统一的 tier 迭代门控引擎，封装 `execute_stream()` / `execute_message()` 共享的循环逻辑。

| 方法                                                               | 说明                                                                 |
| ------------------------------------------------------------------ | -------------------------------------------------------------------- |
| `execute_stream(body, headers)`                                    | 流式路由主循环，yield `(chunk, vendor_name)`                         |
| `execute_message(body, headers)`                                   | 非流式路由主循环，返回 `VendorResponse`                              |
| `_try_gate_tier(tier, is_last, caps, canonical, session, reasons)` | 单 tier 门控：能力匹配 → 兼容性决策 → 上下文应用 → 健康检查         |
| `_handle_token_error(tier, exc, is_last, failed_name)`             | TokenAcquireError 处理 + reauth 触发                                 |
| `_handle_http_error(tier, exc, ...)`                               | HTTP 错误处理：语义拒绝 / cap error / rate limit 解析 / failure 记录 |
| `_is_cap_error(resp)`                                              | 静态方法：检测 429/403 + 配额关键词                                  |

---

## 4. UsageRecorder

**文件**：[`routing/usage_recorder.py`](../../src/coding/proxy/routing/usage_recorder.py)

| 方法                                                                     | 说明                                                     |
| ------------------------------------------------------------------------ | -------------------------------------------------------- |
| `set_pricing_table(table)`                                               | 注入 PricingTable（lifespan 启动时调用）                 |
| `build_usage_info(usage_dict)`                                           | 从原始 dict 构建结构化 UsageInfo                         |
| `log_model_call(vendor, model_requested, model_served, duration, usage)` | 输出 ModelCall 级别 Access Log（含定价）                 |
| `record(vendor, ..., evidence_records)`                                  | 持久化用量到 TokenLogger + evidence 记录（Copilot 专用） |
| `build_nonstream_evidence_records(...)`                                  | 构建非流式证据记录                                       |

---

## 5. RouteSessionManager

**文件**：[`routing/session_manager.py`](../../src/coding/proxy/routing/session_manager.py)

| 方法                                                       | 说明                                  |
| ---------------------------------------------------------- | ------------------------------------- |
| `get_or_create_record(session_key, trace_id)`              | 获取或创建会话记录                    |
| `apply_compat_context(tier, canonical, decision, session)` | 构建 CompatibilityTrace 并注入 vendor |
| `persist_session(trace, session)`                          | 持久化会话状态到 CompatSessionStore   |

---

## 6. Error Classifier

**文件**：[`routing/error_classifier.py`](../../src/coding/proxy/routing/error_classifier.py)

| 函数                                                            | 说明                                                   |
| --------------------------------------------------------------- | ------------------------------------------------------ |
| `build_request_capabilities(body)`                              | 从请求体提取能力画像（tools/thinking/images/metadata） |
| `is_semantic_rejection(status_code, error_type, error_message)` | 判断是否为语义拒绝（400 + 特定模式）                   |
| `extract_error_payload_from_http_status(exc)`                   | 从 HTTPStatusError 安全提取 JSON payload               |

---

## 7. Rate Limit

**文件**：[`routing/rate_limit.py`](../../src/coding/proxy/routing/rate_limit.py)

| 函数/类                                                 | 说明                                                                     |
| ------------------------------------------------------- | ------------------------------------------------------------------------ |
| `RateLimitInfo`                                         | 数据类：retry_after / requests_reset_at / tokens_reset_at / is_cap_error |
| `parse_rate_limit_headers(headers, status, error_body)` | 从响应头解析所有速率限制信号                                             |
| `compute_effective_retry_seconds(info)`                 | 计算最保守恢复等待时间（相对秒数，+10% 余量）                            |
| `compute_rate_limit_deadline(info)`                     | 计算最保守恢复截止时间（绝对 monotonic 时间戳，+10% 余量）               |

---

## 8. Retry

**文件**：[`routing/retry.py`](../../src/coding/proxy/routing/retry.py)

| 函数/类                         | 说明                                                                                |
| ------------------------------- | ----------------------------------------------------------------------------------- |
| `RetryConfig`                   | 数据类：max_retries / initial_delay_ms / max_delay_ms / backoff_multiplier / jitter |
| `is_retryable_error(exc)`       | 判断异常是否值得重试                                                                |
| `is_retryable_status(code)`     | 判断状态码是否值得重试（5xx）                                                       |
| `calculate_delay(attempt, cfg)` | 计算第 N 次重试延迟（含 Full Jitter）                                               |

> **参数默认值**：参见 [配置参考 -- RetryConfig](./config-reference.md#elastic-params)

---

## 9. CircuitBreaker

**文件**：[`routing/circuit_breaker.py`](../../src/coding/proxy/routing/circuit_breaker.py)

状态机：CLOSED → OPEN → HALF_OPEN → CLOSED，含指数退避。

> **参数默认值**：参见 [配置参考 -- CircuitBreakerConfig](./config-reference.md#elastic-params)
>
> **设计语义**：参见 [设计模式 -- Circuit Breaker](./design-patterns.md#circuit-breaker)

---

## 10. QuotaGuard

**文件**：[`routing/quota_guard.py`](../../src/coding/proxy/routing/quota_guard.py)

基于滑动窗口的双态状态机（WITHIN_QUOTA / QUOTA_EXCEEDED）。

**公共方法**：

| 方法                          | 说明                                             |
| ----------------------------- | ------------------------------------------------ |
| `can_use_primary()`           | 综合判断是否允许使用此后端                       |
| `record_usage(tokens)`        | 记录 Token 用量到滑动窗口                        |
| `record_primary_success()`    | 探测成功后恢复为 WITHIN_QUOTA                    |
| `notify_cap_error()`          | 外部通知检测到 cap 错误，强制进入 QUOTA_EXCEEDED |
| `load_baseline(total_tokens)` | 从数据库加载历史用量基线                         |
| `reset()`                     | 手动重置为 WITHIN_QUOTA                          |
| `get_info()`                  | 获取状态信息（供 `/api/status` 使用）            |

> **参数默认值**：参见 [配置参考 -- QuotaGuardConfig](./config-reference.md#elastic-params)
>
> **设计语义**：参见 [设计模式 -- QuotaGuard State Machine](./design-patterns.md#quota-guard)
