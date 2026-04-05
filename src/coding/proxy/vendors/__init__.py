"""供应商适配层 — 所有供应商实现的统一入口."""

from .base import (  # noqa: F401
    BaseBackend,  # 向后兼容别名
    BaseVendor,
    CapabilityLossReason,
    CopilotExchangeDiagnostics,
    CopilotMisdirectedRequest,
    CopilotModelCatalog,
    NoCompatibleBackendError,  # 向后兼容别名
    NoCompatibleVendorError,
    RequestCapabilities,
    UsageInfo,
    VendorCapabilities,
    VendorResponse,
    decode_json_body,
    extract_error_message,
    sanitize_headers_for_synthetic_response,
)

__all__ = [
    "BaseVendor",
    "BaseBackend",
    "VendorCapabilities",
    "BackendCapabilities",
    "VendorResponse",
    "BackendResponse",
    "NoCompatibleVendorError",
    "NoCompatibleBackendError",
    "CapabilityLossReason",
    "RequestCapabilities",
    "UsageInfo",
    "CopilotExchangeDiagnostics",
    "CopilotMisdirectedRequest",
    "CopilotModelCatalog",
    "decode_json_body",
    "extract_error_message",
    "sanitize_headers_for_synthetic_response",
]
