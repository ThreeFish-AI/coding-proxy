"""Copilot URL 管理纯函数 — 向后兼容 re-export shim.

所有常量与函数已合并至 :mod:`coding.proxy.backends.copilot_models`。
"""

# noqa: F401
from .copilot_models import (
    _COPILOT_VERSION,
    _EDITOR_PLUGIN_VERSION,
    _EDITOR_VERSION,
    _GITHUB_API_VERSION,
    _USER_AGENT,
    _normalize_base_url,
    build_copilot_candidate_base_urls,
    resolve_copilot_base_url,
)
