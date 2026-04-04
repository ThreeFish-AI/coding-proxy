"""copilot_urls.py URL 管理纯函数单元测试."""

from coding.proxy.vendors.copilot_urls import (
    _normalize_base_url,
    build_copilot_candidate_base_urls,
    resolve_copilot_base_url,
)


def test_resolve_copilot_base_url_individual():
    assert resolve_copilot_base_url("individual", "") == "https://api.individual.githubcopilot.com"


def test_resolve_copilot_base_url_business():
    assert resolve_copilot_base_url("business", "") == "https://api.business.githubcopilot.com"


def test_resolve_copilot_base_url_enterprise():
    assert resolve_copilot_base_url("enterprise", "") == "https://api.enterprise.githubcopilot.com"


def test_resolve_copilot_base_url_custom_overrides_default():
    assert resolve_copilot_base_url("individual", "https://custom.example.com") == "https://custom.example.com"


def test_resolve_copilot_base_url_trailing_slash_stripped():
    assert resolve_copilot_base_url("individual", "https://custom.example.com/") == "https://custom.example.com"


def test_build_candidates_individual():
    result = build_copilot_candidate_base_urls("individual", "")
    assert result == [
        "https://api.individual.githubcopilot.com",
        "https://api.githubcopilot.com",
    ]


def test_build_candidates_business():
    result = build_copilot_candidate_base_urls("business", "")
    assert result == [
        "https://api.business.githubcopilot.com",
        "https://api.githubcopilot.com",
    ]


def test_build_candidates_custom_url():
    result = build_copilot_candidate_base_urls("individual", "https://custom.example.com/")
    assert result == ["https://custom.example.com"]


def test_build_candidates_deduplicates():
    """当 account_type 为 githubcopilot 时不应重复."""
    result = build_copilot_candidate_base_urls("githubcopilot", "")
    # 第一个候选和 fallback 可能相同，应去重
    assert len(result) >= 1
    assert len(result) == len(set(result))


def test_normalize_base_url_removes_trailing_slash():
    assert _normalize_base_url("https://example.com/") == "https://example.com"
    assert _normalize_base_url("https://example.com//") == "https://example.com"
    assert _normalize_base_url("https://example.com") == "https://example.com"


def test_resolve_empty_account_type_falls_back_to_individual():
    """空 account_type 回退到 individual."""
    assert resolve_copilot_base_url("", "") == "https://api.individual.githubcopilot.com"
