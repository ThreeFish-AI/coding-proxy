"""constants.py 跨模块共享常量单元测试."""

from coding.proxy.model.constants import (
    _COPILOT_VERSION,
    _EDITOR_PLUGIN_VERSION,
    _EDITOR_VERSION,
    _GITHUB_API_VERSION,
    _USER_AGENT,
    PROXY_SKIP_HEADERS,
    RESPONSE_SANITIZE_SKIP_HEADERS,
)

# ── Header 常量 ──────────────────────────────────────────────


def test_proxy_skip_headers_is_frozenset_with_expected_members():
    """PROXY_SKIP_HEADERS 应为 frozenset[str] 且包含全部 hop-by-hop 请求头."""
    assert isinstance(PROXY_SKIP_HEADERS, frozenset)
    assert {
        "host",
        "content-length",
        "transfer-encoding",
        "connection",
    } == PROXY_SKIP_HEADERS


def test_response_sanitize_skip_headers_is_frozenset_with_expected_members():
    """RESPONSE_SANITIZE_SKIP_HEADERS 应为 frozenset[str] 且包含需移除的响应头."""
    assert isinstance(RESPONSE_SANITIZE_SKIP_HEADERS, frozenset)
    assert {
        "content-encoding",
        "content-length",
        "transfer-encoding",
    } == RESPONSE_SANITIZE_SKIP_HEADERS


def test_header_sets_have_correct_overlap():
    """两个跳过头部集合的交集应恰好包含 content-length 与 transfer-encoding."""
    overlap = PROXY_SKIP_HEADERS & RESPONSE_SANITIZE_SKIP_HEADERS
    assert overlap == {"content-length", "transfer-encoding"}


# ── Copilot 版本 / URL 常量 ─────────────────────────────────


def test_copilot_version_constant_values():
    """基础版本与 API 版本常量应具有预期字面值."""
    assert _COPILOT_VERSION == "0.26.7"
    assert _EDITOR_VERSION == "vscode/1.98.0"
    assert _GITHUB_API_VERSION == "2025-04-01"


def test_copilot_derived_constants_use_correct_interpolation():
    """派生常量应基于 _COPILOT_VERSION 正确拼接字符串."""
    assert f"copilot-chat/{_COPILOT_VERSION}" == _EDITOR_PLUGIN_VERSION
    assert _EDITOR_PLUGIN_VERSION == "copilot-chat/0.26.7"

    assert f"GitHubCopilotChat/{_COPILOT_VERSION}" == _USER_AGENT
    assert _USER_AGENT == "GitHubCopilotChat/0.26.7"
