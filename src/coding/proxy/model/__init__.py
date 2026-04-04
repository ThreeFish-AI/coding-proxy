"""集中化数据模型 — coding-proxy 所有共享类型的单一事实源.

本模块将分散在各处的类型定义（后端类型、兼容层抽象、认证凭证、
Token 管理、定价模型、共享常量）统一收归于此，遵循正交分解原则
按职责域划分子模块。

使用者可直接从本包导入::

    from coding.proxy.model import UsageInfo, BackendResponse, CanonicalRequest

也可从具体子模块导入以获得更细粒度的依赖控制::

    from coding.proxy.model.backend import UsageInfo, BackendResponse
    from coding.proxy.model.compat import CanonicalRequest, CompatibilityProfile
"""

# ── 后端核心类型 ──────────────────────────────────────────
from .backend import (  # noqa: F401
    BackendCapabilities,
    BackendResponse,
    CapabilityLossReason,
    CopilotExchangeDiagnostics,
    CopilotMisdirectedRequest,
    CopilotModelCatalog,
    NoCompatibleBackendError,
    RequestCapabilities,
    UsageInfo,
    decode_json_body,
    extract_error_message,
    sanitize_headers_for_synthetic_response,
)

# ── 兼容层抽象类型 ────────────────────────────────────────
from .compat import (  # noqa: F401
    CanonicalMessagePart,
    CanonicalPartType,
    CanonicalRequest,
    CanonicalThinking,
    CanonicalToolCall,
    CompatSessionRecord,
    CompatibilityDecision,
    CompatibilityProfile,
    CompatibilityStatus,
    CompatibilityTrace,
)

# ── 认证凭证模型 ──────────────────────────────────────────
from .auth import ProviderTokens  # noqa: F401

# ── Token 管理类型 ──────────────────────────────────────────
from .token import (  # noqa: F401
    TokenAcquireError,
    TokenErrorKind,
    TokenManagerDiagnostics,
)

# ── 定价模型 ────────────────────────────────────────────────
from .pricing import CostValue, Currency, ModelPricing  # noqa: F401

# ── 共享常量 ────────────────────────────────────────────────
from .constants import (  # noqa: F401
    PROXY_SKIP_HEADERS,
    RESPONSE_SANITIZE_SKIP_HEADERS,
)

__all__ = [
    # backend
    "BackendCapabilities", "BackendResponse", "CapabilityLossReason",
    "CopilotExchangeDiagnostics", "CopilotMisdirectedRequest", "CopilotModelCatalog",
    "NoCompatibleBackendError", "RequestCapabilities", "UsageInfo",
    "decode_json_body", "extract_error_message", "sanitize_headers_for_synthetic_response",
    # compat
    "CanonicalMessagePart", "CanonicalPartType", "CanonicalRequest",
    "CanonicalThinking", "CanonicalToolCall", "CompatSessionRecord",
    "CompatibilityDecision", "CompatibilityProfile", "CompatibilityStatus", "CompatibilityTrace",
    # auth
    "ProviderTokens",
    # token
    "TokenAcquireError", "TokenErrorKind", "TokenManagerDiagnostics",
    # pricing
    "CostValue", "Currency", "ModelPricing",
    # constants
    "PROXY_SKIP_HEADERS", "RESPONSE_SANITIZE_SKIP_HEADERS",
]
