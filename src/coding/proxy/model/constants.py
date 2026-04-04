"""跨模块共享常量 — 协议级头部过滤规则与 Copilot 元数据."""

# ── 代理转发头过滤规则 ─────────────────────────────────────

# 代理转发时应跳过的 hop-by-hop 请求头
PROXY_SKIP_HEADERS: frozenset[str] = frozenset({
    "host", "content-length", "transfer-encoding", "connection",
})

# 构造合成 Response 时需移除的头部（避免 httpx 二次解压已解压内容）
RESPONSE_SANITIZE_SKIP_HEADERS: frozenset[str] = frozenset({
    "content-encoding", "content-length", "transfer-encoding",
})

# ── Copilot URL / 版本常量 ─────────────────────────────────

_COPILOT_VERSION = "0.26.7"
_EDITOR_VERSION = "vscode/1.98.0"
_EDITOR_PLUGIN_VERSION = f"copilot-chat/{_COPILOT_VERSION}"
_USER_AGENT = f"GitHubCopilotChat/{_COPILOT_VERSION}"
_GITHUB_API_VERSION = "2025-04-01"
