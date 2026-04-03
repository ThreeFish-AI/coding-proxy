"""Copilot URL 管理纯函数."""

_COPILOT_VERSION = "0.26.7"
_EDITOR_VERSION = "vscode/1.98.0"
_EDITOR_PLUGIN_VERSION = f"copilot-chat/{_COPILOT_VERSION}"
_USER_AGENT = f"GitHubCopilotChat/{_COPILOT_VERSION}"
_GITHUB_API_VERSION = "2025-04-01"


def _normalize_base_url(url: str) -> str:
    return url.rstrip("/")


def build_copilot_candidate_base_urls(account_type: str, configured_base_url: str) -> list[str]:
    """构建 Copilot 候选基础地址列表."""
    if configured_base_url.strip():
        return [_normalize_base_url(configured_base_url.strip())]

    normalized = (account_type or "individual").strip().lower() or "individual"
    candidates = [f"https://api.{normalized}.githubcopilot.com"]
    candidates.append("https://api.githubcopilot.com")

    unique_candidates: list[str] = []
    for candidate in candidates:
        normalized_candidate = _normalize_base_url(candidate)
        if normalized_candidate not in unique_candidates:
            unique_candidates.append(normalized_candidate)
    return unique_candidates


def resolve_copilot_base_url(account_type: str, configured_base_url: str) -> str:
    """解析 Copilot API 基础地址.

    保留用户显式覆盖；仅当值为空时按账号类型回退到官方推荐域名。
    """
    return build_copilot_candidate_base_urls(account_type, configured_base_url)[0]
