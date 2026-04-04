"""后端类型定义 — 向后兼容 re-export shim.

所有类型已迁移至 :mod:`coding.proxy.model.backend`，常量迁移至
:mod:`coding.proxy.model.constants`。本文件仅保留 re-export 以确保
现有 import 路径继续工作。

.. deprecated::
    未来版本将移除此 shim，请直接从 :mod:`coding.proxy.model` 导入。
"""

# noqa: F401
from ..model.backend import (
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
from ..model.constants import (
    PROXY_SKIP_HEADERS,
    RESPONSE_SANITIZE_SKIP_HEADERS,
)

# ── 废弃别名（向后兼容旧名称） ──────────────────────────
_decode_json_body = decode_json_body
_extract_error_message = extract_error_message
_sanitize_headers_for_synthetic_response = sanitize_headers_for_synthetic_response
