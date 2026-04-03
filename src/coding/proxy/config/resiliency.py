"""弹性设施配置模型（熔断、重试、故障转移、配额守卫）."""

from __future__ import annotations

from pydantic import BaseModel, Field


class CircuitBreakerConfig(BaseModel):
    failure_threshold: int = 3
    recovery_timeout_seconds: int = 300
    success_threshold: int = 2
    max_recovery_seconds: int = 3600


class RetryConfig(BaseModel):
    """传输层重试配置."""

    max_retries: int = 2
    initial_delay_ms: int = 500
    max_delay_ms: int = 5000
    backoff_multiplier: float = 2.0
    jitter: bool = True


class FailoverConfig(BaseModel):
    status_codes: list[int] = Field(
        default=[429, 403, 503, 500],
    )
    error_types: list[str] = Field(
        default=["rate_limit_error", "overloaded_error", "api_error"],
    )
    error_message_patterns: list[str] = Field(
        default=["quota", "limit exceeded", "usage cap", "capacity"],
    )


class QuotaGuardConfig(BaseModel):
    enabled: bool = False
    token_budget: int = 0
    window_hours: float = 5.0
    threshold_percent: float = 99.0
    probe_interval_seconds: int = 300

__all__ = [
    "CircuitBreakerConfig", "RetryConfig", "FailoverConfig", "QuotaGuardConfig",
]
