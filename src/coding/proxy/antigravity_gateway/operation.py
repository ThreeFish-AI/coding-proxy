"""v1internal 路径 → 规范化操作名分类器.

Antigravity 后端使用 Google AIP 冒号方法风格路径（如
``/v1internal:generateContent``），与 Gemini 公开 API 的
``models/{model}:generateContent`` 形态不同，故在本包内独立分类，
不与 :mod:`..native_api.operation` 混用。
"""

from __future__ import annotations

import re

# 冒号方法名 → 规范 operation（与 usage_log.operation 既有词表对齐）
# 端点清单来自真机抓包（模型面 3 + 控制面 7），未列出的动词 → unknown 照常透传
_V1INTERNAL_VERB_MAP: dict[str, str] = {
    # 模型面
    "generateContent": "generate_content",
    "streamGenerateContent": "generate_content",
    "countTokens": "count_tokens",
    # 控制面
    "fetchAvailableModels": "model.list",
    "loadCodeAssist": "load_code_assist",
    "fetchUserInfo": "user.info",
    "retrieveUserQuotaSummary": "quota.summary",
    "listExperiments": "list_experiments",
    "fetchAdminControls": "admin_controls",
    "writeTrajectoryAcls": "write_trajectory_acls",
}

# ``/v1internal:verb`` 或 ``/v1internal%3Averb``（编码形态）
_V1INTERNAL_RE = re.compile(r"^/?v1internal(?:%3A|:)(?P<verb>[A-Za-z]+)$")


def classify_v1internal_path(method: str, path: str) -> str:
    """``(method, path) → operation``；未命中返回 ``"unknown"``（照常透传）."""
    _ = method  # v1internal 全部为 POST，保留签名与 native_api 分类器一致
    normalized = path if path.startswith("/") else f"/{path}"
    m = _V1INTERNAL_RE.match(normalized)
    if m:
        return _V1INTERNAL_VERB_MAP.get(m.group("verb"), "unknown")
    return "unknown"


__all__ = ["classify_v1internal_path"]
