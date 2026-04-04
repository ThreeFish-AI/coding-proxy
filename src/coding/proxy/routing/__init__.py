"""路由模块."""

from .circuit_breaker import CircuitBreaker, CircuitState
from .error_classifier import (
    build_request_capabilities,
    extract_error_payload_from_http_status,
    is_semantic_rejection,
)
from .executor import _RouteExecutor
from .model_mapper import ModelMapper
from .quota_guard import QuotaGuard, QuotaState
from .rate_limit import RateLimitInfo
from .rate_limit import (
    compute_effective_retry_seconds,
    compute_rate_limit_deadline,
    parse_rate_limit_headers,
)
from .retry import RetryConfig, calculate_delay, is_retryable_error
from .router import RequestRouter
from .session_manager import RouteSessionManager
from .tier import BackendTier
from .usage_parser import (
    build_usage_evidence_records,
    has_missing_input_usage_signals,
    parse_usage_from_chunk,
)
from .usage_recorder import UsageRecorder

__all__ = [
    # Core routing
    "CircuitBreaker", "CircuitState",
    "ModelMapper", "RequestRouter", "BackendTier",
    # Decomposed components (internal use)
    "_RouteExecutor", "UsageRecorder", "RouteSessionManager",
    # Resiliency
    "QuotaGuard", "QuotaState", "RateLimitInfo",
    "RetryConfig",
    "parse_rate_limit_headers", "compute_effective_retry_seconds",
    "compute_rate_limit_deadline",
    "is_retryable_error", "calculate_delay",
    # Error classification
    "build_request_capabilities", "is_semantic_rejection",
    "extract_error_payload_from_http_status",
    # Usage parsing
    "build_usage_evidence_records", "has_missing_input_usage_signals",
    "parse_usage_from_chunk",
]
