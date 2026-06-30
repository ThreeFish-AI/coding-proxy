"""传输层重试策略 — 指数退避与 Equal Jitter.

与 Circuit Breaker 正交互：
- Retry 处理瞬态网络抖动（秒级恢复）
- Circuit Breaker 处理持续故障（分钟级恢复）
- Retry 失败仅向 Circuit Breaker 贡献 1 次失败计数

参考:
[1] M. Nygard, "Release It!," Pragmatic Bookshelf, 2nd ed., 2018.
[2] AWS Architecture Center, "Retry Pattern," 2022.
"""

from __future__ import annotations

import logging
import random
from dataclasses import dataclass

import httpx

logger = logging.getLogger(__name__)


@dataclass
class RetryConfig:
    """传输层重试配置（运行时）."""

    max_retries: int = 2  # 最大重试次数（0 = 禁用）
    initial_delay_ms: int = 500  # 初始退避延迟（毫秒）
    max_delay_ms: int = 5000  # 最大退避延迟（毫秒）
    backoff_multiplier: float = 2.0  # 退避倍数
    jitter: bool = True  # 是否添加随机抖动

    @property
    def enabled(self) -> bool:
        return self.max_retries > 0

    @property
    def max_attempts(self) -> int:
        return self.max_retries + 1


def is_retryable_error(exc: Exception) -> bool:
    """判断异常是否值得重试.

    可重试:
    - httpx.TimeoutException（瞬态超时）
    - httpx.ConnectError（网络连接失败）
    - httpx.HTTPStatusError with 5xx（服务端瞬时错误）

    不可重试:
    - httpx.HTTPStatusError with 4xx（客户端错误）
    - TokenAcquireError（认证层错误）
    - 其他异常
    """
    if isinstance(exc, httpx.TimeoutException):
        return True
    if isinstance(exc, httpx.ConnectError):
        return True
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code >= 500
    return False


def is_retryable_status(status_code: int) -> bool:
    """判断 HTTP 状态码是否值得重试（5xx）."""
    return status_code >= 500


def calculate_delay(attempt: int, cfg: RetryConfig) -> float:
    """计算第 N 次重试的延迟（毫秒），含指数退避和 Equal Jitter.

    Equal Jitter 策略: temp = min(initial * backoff^attempt, max);
    delay = temp/2 + random(0, temp/2)，落在 [temp/2, temp]。
    相较 Full Jitter (random(0, temp))，Equal Jitter 保留一半固定基线，
    使相邻重试的延迟区间仅边界相切，呈现单调非递减的指数形态，
    同时保留抖动以防惊群。

    契约边界：单调非递减依赖 ``backoff_multiplier >= 2.0`` 且未触及
    ``max_delay_ms`` 封顶；封顶后各 attempt 区间退化为同一 [max/2, max]
    （当前 Zhipu ``max_retries=4`` 触及不到该边界）。

    参考: AWS "Exponential Backoff And Jitter" (Marc Brooker, 2015)
    """
    delay = cfg.initial_delay_ms * (cfg.backoff_multiplier**attempt)
    delay = min(delay, cfg.max_delay_ms)

    if cfg.jitter:
        delay = delay / 2 + random.uniform(0, delay / 2)

    return delay
