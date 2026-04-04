"""copilot_models.py 模型解析纯函数与诊断数据类单元测试."""

import time

import pytest

from coding.proxy.vendors.copilot_models import (
    CopilotExchangeDiagnostics,
    CopilotModelCatalog,
    _select_copilot_model as select_copilot_model,
    _copilot_model_family as copilot_model_family,
    _copilot_model_major as copilot_model_major,
    _copilot_model_version_rank as copilot_model_version_rank,
    normalize_copilot_requested_model,
)


# ── normalize_copilot_requested_model ────────────────────


def test_normalize_claude_sonnet():
    assert normalize_copilot_requested_model("claude-sonnet-4-20250514") == "claude-sonnet-4"
    assert normalize_copilot_requested_model("claude-sonnet-4.5-20250514") == "claude-sonnet-4"


def test_normalize_claude_opus():
    assert normalize_copilot_requested_model("claude-opus-4-20250514") == "claude-opus-4"


def test_normalize_claude_haiku():
    assert normalize_copilot_requested_model("claude-haiku-4-5-20251001") == "claude-haiku-4"


def test_normalize_passthrough_non_claude():
    assert normalize_copilot_requested_model("gpt-5.2") == "gpt-5.2"


def test_normalize_empty_string():
    assert normalize_copilot_requested_model("") == ""


def test_normalize_whitespace_only():
    assert normalize_copilot_requested_model("   ") == ""


# ── copilot_model_family ─────────────────────────────────


def test_family_sonnet():
    assert copilot_model_family("claude-sonnet-4-20250514") == "claude-sonnet"


def test_family_opus():
    assert copilot_model_family("claude-opus-4.6") == "claude-opus"


def test_family_haiku():
    assert copilot_model_family("claude-haiku-4.5") == "claude-haiku"


def test_family_non_claude_passthrough():
    assert copilot_model_family("gpt-4") == "gpt-4"


# ── copilot_model_major ───────────────────────────────────


def test_major_version_extraction():
    assert copilot_model_major("claude-sonnet-4-20250514") == 4
    assert copilot_model_major("claude-opus-4.6") == 4
    assert copilot_model_major("claude-haiku-4-5") == 4


def test_major_none_for_no_version():
    assert copilot_model_major("claude-sonnet") is None
    assert copilot_model_major("unknown-model") is None


# ── copilot_model_version_rank ────────────────────────────


def test_version_rank_simple():
    assert copilot_model_version_rank("model-1.2.3") == (1, 2, 3)


def test_version_rank_single():
    assert copilot_model_version_rank("model-4") == (4,)


def test_version_rank_empty():
    assert copilot_model_version_rank("model") == ()


# ── select_copilot_model ──────────────────────────────────


def test_select_prefers_same_family_highest_version():
    selected, reason = select_copilot_model(
        "claude-sonnet-4-20250514",
        ["claude-sonnet-4.5", "claude-sonnet-4.6", "claude-opus-4.6"],
    )
    assert selected == "claude-sonnet-4.6"
    assert reason == "same_family_highest_version"


def test_select_does_not_cross_family():
    selected, reason = select_copilot_model(
        "claude-sonnet-4-20250514",
        ["claude-opus-4.6"],
    )
    assert selected is None
    assert reason == "no_same_family_model_available"


def test_select_exact_match():
    selected, reason = select_copilot_model(
        "claude-sonnet-4.6",
        ["claude-sonnet-4.6", "claude-opus-4.6"],
    )
    assert selected == "claude-sonnet-4.6"
    assert reason == "exact_requested_model"


def test_select_normalized_match():
    selected, reason = select_copilot_model(
        "claude-sonnet-4-20250514",
        ["claude-sonnet-4"],
    )
    assert selected == "claude-sonnet-4"
    assert reason == "normalized_requested_model"


def test_select_empty_available():
    selected, reason = select_copilot_model("claude-sonnet-4", [])
    assert selected is None
    assert reason == "available_models_empty"


# ── CopilotExchangeDiagnostics ────────────────────────────


def test_exchange_diagnostics_defaults():
    diag = CopilotExchangeDiagnostics()
    assert diag.raw_shape == ""
    assert diag.token_field == ""
    assert diag.expires_in_seconds == 0
    d = diag.to_dict()
    assert d == {}


def test_exchange_diagnostics_to_dict():
    now = int(time.time())
    diag = CopilotExchangeDiagnostics(
        raw_shape="token_refresh_in",
        token_field="token",
        expires_in_seconds=1800,
        expires_at_unix=now + 1800,
        capabilities={"chat_enabled": True},
        updated_at_unix=now,
    )
    d = diag.to_dict()
    assert d["raw_shape"] == "token_refresh_in"
    assert d["token_field"] == "token"
    assert d["expires_in_seconds"] == 1800
    assert d["capabilities"]["chat_enabled"] is True
    assert "ttl_seconds" in d
    assert d["ttl_seconds"] <= 1800


# ── CopilotModelCatalog ────────────────────────────────────


def test_model_catalog_defaults():
    catalog = CopilotModelCatalog()
    assert catalog.available_models == []
    assert catalog.fetched_at_unix == 0
    assert catalog.age_seconds() is None


def test_model_catalog_age_seconds():
    catalog = CopilotModelCatalog(
        available_models=["m1", "m2"],
        fetched_at_unix=int(time.time()) - 60,
    )
    age = catalog.age_seconds()
    assert age is not None
    assert age >= 59  # 允许 1 秒误差
    assert age <= 61


def test_model_catalog_never_fetched():
    catalog = CopilotModelCatalog(fetched_at_unix=0)
    assert catalog.age_seconds() is None
