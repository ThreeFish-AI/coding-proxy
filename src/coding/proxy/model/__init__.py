"""集中化数据模型 — coding-proxy 所有共享类型的单一事实源.

本模块将分散在各处的类型定义（供应商类型、兼容层抽象、认证凭证、
Token 管理、定价模型、共享常量）统一收归于此，遵循正交分解原则
按职责域划分子模块。

使用者可直接从本包导入::

    from coding.proxy.model import UsageInfo, VendorResponse, CanonicalRequest

也可从具体子模块导入以获得更细粒度的依赖控制::

    from coding.proxy.model.vendor import UsageInfo, VendorResponse
    from coding.proxy.model.compat import CanonicalRequest, CompatibilityProfile
"""

# ── 供应商核心类型 ──────────────────────────────────────────
from .vendor import (  # noqa: F401
    VendorCapabilities,
    VendorResponse,
    CapabilityLossReason,
    CopilotExchangeDiagnostics,
    CopilotMisdirectedRequest,
    CopilotModelCatalog,
    NoCompatibleVendorError,
    RequestCapabilities,
    UsageInfo,
    decode_json_body,
    extract_error_message,
    sanitize_headers_for_synthetic_response,
)

# ── 向后兼容别名（v2 移除）────────────────────────────────────
BackendCapabilities = VendorCapabilities  # noqa: F401
BackendResponse = VendorResponse  # noqa: F401
NoCompatibleBackendError = NoCompatibleVendorError  # noqa: F401

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
    # vendor（新命名）
    "VendorCapabilities", "VendorResponse", "NoCompatibleVendorError",
    # 向后兼容别名
    "BackendCapabilities", "BackendResponse", "NoCompatibleBackendError",
    # 通用类型
    "CapabilityLossReason", "CopilotExchangeDiagnostics",
    "CopilotMisdirectedRequest", "CopilotModelCatalog",
    "RequestCapabilities", "UsageInfo",
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
