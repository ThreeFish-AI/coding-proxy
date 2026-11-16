"""订阅状态聚合测试 — 被动信号解析、三家 fetcher、TTL 服务与端点."""

from __future__ import annotations

import httpx
import pytest
from fastapi.testclient import TestClient

from coding.proxy.config.schema import AnthropicConfig, FailoverConfig, ProxyConfig
from coding.proxy.routing.subscription_signals import (
    capture_anthropic_ratelimit,
    get_anthropic_snapshot,
    parse_unified_ratelimit,
    reset_anthropic_snapshot,
)
from coding.proxy.server.app import create_app
from coding.proxy.server.subscription_status import (
    AntigravityUsageFetcher,
    SubscriptionError,
    SubscriptionStatusService,
    ZhipuUsageFetcher,
)
from coding.proxy.vendors.anthropic import AnthropicVendor
from coding.proxy.vendors.token_manager import (
    TokenAcquireError,
    TokenErrorKind,
)

_UNIFIED_HEADERS = {
    "anthropic-ratelimit-unified-5h-utilization": "0.42",
    "anthropic-ratelimit-unified-5h-reset": "1731750000",
    "anthropic-ratelimit-unified-7d-utilization": "0.18",
    "anthropic-ratelimit-unified-7d-reset": "1732200000",
    "anthropic-ratelimit-unified-status": "allowed",
}


@pytest.fixture(autouse=True)
def _isolated_signal_store():
    reset_anthropic_snapshot()
    yield
    reset_anthropic_snapshot()


# ── 被动信号解析 ─────────────────────────────────────────────


def test_parse_unified_ratelimit_full_headers():
    snap = parse_unified_ratelimit(_UNIFIED_HEADERS)
    assert snap is not None
    assert snap.five_hour_percent == pytest.approx(42.0)
    assert snap.five_hour_reset_epoch == 1731750000
    assert snap.seven_day_percent == pytest.approx(18.0)
    assert snap.status == "allowed"


def test_parse_unified_ratelimit_case_insensitive():
    snap = parse_unified_ratelimit(
        httpx.Headers({"Anthropic-Ratelimit-Unified-5h-Utilization": "0.5"})
    )
    assert snap is not None
    assert snap.five_hour_percent == pytest.approx(50.0)
    assert snap.seven_day_percent is None


def test_parse_unified_ratelimit_no_unified_headers_returns_none():
    """API key 流量不返回 unified 头，解析为 None（不清空既有快照）."""
    assert parse_unified_ratelimit({"x-request-id": "req_123"}) is None
    assert parse_unified_ratelimit({}) is None


def test_parse_unified_ratelimit_malformed_values_tolerated():
    snap = parse_unified_ratelimit(
        {
            "anthropic-ratelimit-unified-5h-utilization": "not-a-number",
            "anthropic-ratelimit-unified-5h-reset": "oops",
        }
    )
    assert snap is not None
    assert snap.five_hour_percent is None
    assert snap.five_hour_reset_epoch is None


def test_parse_unified_ratelimit_over_utilization_clamped():
    snap = parse_unified_ratelimit(
        {"anthropic-ratelimit-unified-7d-utilization": "1.4"}
    )
    assert snap is not None
    assert snap.seven_day_percent == 100.0


def test_signal_store_capture_and_reset():
    capture_anthropic_ratelimit(_UNIFIED_HEADERS)
    snap = get_anthropic_snapshot()
    assert snap is not None and snap.five_hour_percent == pytest.approx(42.0)
    # 非 unified 头不清空快照
    capture_anthropic_ratelimit({"content-type": "application/json"})
    assert get_anthropic_snapshot() is snap
    reset_anthropic_snapshot()
    assert get_anthropic_snapshot() is None


async def test_anthropic_vendor_hook_captures_on_success():
    """AnthropicVendor 透传链路旁路采集 unified 头（非流式 + 流式）."""
    vendor = AnthropicVendor(AnthropicConfig(), FailoverConfig())
    vendor._client = httpx.AsyncClient(
        base_url="https://api.anthropic.com",
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200,
                json={"id": "msg_1", "usage": {"input_tokens": 1, "output_tokens": 1}},
                headers=_UNIFIED_HEADERS,
            )
        ),
    )
    try:
        await vendor.send_message({"messages": []}, {"authorization": "Bearer x"})
        snap = get_anthropic_snapshot()
        assert snap is not None
        assert snap.five_hour_percent == pytest.approx(42.0)

        reset_anthropic_snapshot()
        chunks = [
            chunk
            async for chunk in vendor.send_message_stream(
                {"messages": [], "stream": True}, {"authorization": "Bearer x"}
            )
        ]
        assert chunks
        assert get_anthropic_snapshot() is not None
    finally:
        await vendor.close()


# ── GLM fetcher ──────────────────────────────────────────────


def _zhipu_fetcher(handler) -> ZhipuUsageFetcher:
    return ZhipuUsageFetcher(
        "test-key",
        "https://open.bigmodel.cn/api/anthropic",
        transport=httpx.MockTransport(handler),
    )


async def test_zhipu_fetcher_official_shape():
    """官方插件形态：data.level + limits[]（unit/number 编码窗口）."""

    def handler(request: httpx.Request) -> httpx.Response:
        assert (
            str(request.url) == "https://open.bigmodel.cn/api/monitor/usage/quota/limit"
        )
        # 裸 key 鉴权（无 Bearer 前缀）
        assert request.headers["authorization"] == "test-key"
        return httpx.Response(
            200,
            json={
                "code": 200,
                "success": True,
                "data": {
                    "level": "pro",
                    "limits": [
                        {
                            "type": "TOKENS_LIMIT",
                            "percentage": 42,
                            "unit": 3,
                            "number": 5,
                            "nextResetTime": 1731750000000,
                        },
                        {
                            "type": "TOKENS_LIMIT",
                            "percentage": 10,
                            "unit": 6,
                            "number": 1,
                            "nextResetTime": 1732200000000,
                        },
                        {"type": "TIME_LIMIT", "usage": 3, "remaining": 97},
                    ],
                },
            },
        )

    result = await _zhipu_fetcher(handler).fetch()
    assert result["plan"] == "Pro"
    assert [w["id"] for w in result["windows"]] == ["5h", "weekly"]
    assert result["windows"][0]["used_percent"] == pytest.approx(42.0)
    assert result["windows"][0]["reset_epoch"] == 1731750000  # ms → s
    assert result["windows"][1]["label"] == "每周"


async def test_zhipu_fetcher_legacy_shape():
    """社区历史形态：used_percent / reset_time 字段名."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "data": {
                    "level": "lite",
                    "limits": [
                        {
                            "type": "TOKENS_LIMIT",
                            "used_percent": 77.5,
                            "unit": 3,
                            "number": 5,
                            "reset_time": 1731750000,
                        }
                    ],
                }
            },
        )

    result = await _zhipu_fetcher(handler).fetch()
    assert result["plan"] == "Lite"
    assert result["windows"][0]["used_percent"] == pytest.approx(77.5)
    assert result["windows"][0]["reset_epoch"] == 1731750000


async def test_zhipu_fetcher_http_error():
    fetcher = _zhipu_fetcher(lambda request: httpx.Response(401, json={}))
    with pytest.raises(SubscriptionError, match="401"):
        await fetcher.fetch()


async def test_zhipu_fetcher_missing_data():
    fetcher = _zhipu_fetcher(lambda request: httpx.Response(200, json={"code": 0}))
    with pytest.raises(SubscriptionError, match="data"):
        await fetcher.fetch()


# ── Antigravity fetcher ──────────────────────────────────────


def _ag_transport(state: dict) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        state.setdefault("calls", []).append(request.url.path)
        assert request.headers["user-agent"].startswith("Antigravity/")
        assert request.headers["x-client-name"] == "antigravity"
        if request.url.path.endswith(":loadCodeAssist"):
            return httpx.Response(
                200,
                json={
                    "currentTier": {"id": "standard-tier", "name": "Google AI Pro"},
                    "cloudaicompanionProject": "aicode-consumers",
                },
            )
        if request.url.path.endswith(":retrieveUserQuotaSummary"):
            assert '"aicode-consumers"' in request.content.decode()
            return httpx.Response(
                200,
                json={
                    "groups": [
                        {
                            "displayName": "Gemini Models",
                            "buckets": [
                                {
                                    "bucketId": "gemini-5h",
                                    "window": "5h",
                                    "remainingFraction": 0.25,
                                    "resetTime": "2026-11-16T12:00:00Z",
                                },
                                {
                                    "bucketId": "gemini-weekly",
                                    "window": "weekly",
                                    "remainingFraction": 1.0,
                                    "resetTime": "2026-11-20T00:00:00Z",
                                },
                            ],
                        },
                        {
                            "displayName": "Claude and GPT Models",
                            "buckets": [
                                {
                                    "bucketId": "3p-5h",
                                    "window": "5h",
                                    "remainingFraction": 0.0,
                                    "resetTime": "2026-11-16T13:00:00Z",
                                    "disabled": True,
                                },
                            ],
                        },
                    ]
                },
            )
        return httpx.Response(404)

    return httpx.MockTransport(handler)


async def test_antigravity_fetcher_groups_and_tier_cache():
    state: dict = {}
    fetcher = AntigravityUsageFetcher(
        lambda: _async_return("tok"), transport=_ag_transport(state)
    )
    result = await fetcher.fetch()
    assert result["plan"] == "Google AI Pro"
    by_id = {w["id"]: w for w in result["windows"]}
    assert by_id["gemini-5h"]["used_percent"] == pytest.approx(75.0)
    assert by_id["gemini-5h"]["label"] == "Gemini Models 5h"
    assert by_id["gemini-5h"]["reset_epoch"] is not None
    assert by_id["gemini-weekly"]["used_percent"] == pytest.approx(0.0)
    assert by_id["3p-5h"]["exhausted"] is True
    assert by_id["3p-5h"]["used_percent"] == 100.0

    # 第二次 fetch：loadCodeAssist 命中缓存，仅重查配额汇总
    calls_before = list(state["calls"])
    await fetcher.fetch()
    assert state["calls"] == calls_before + ["/v1internal:retrieveUserQuotaSummary"]


async def test_antigravity_fetcher_403_gate():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, json={})

    fetcher = AntigravityUsageFetcher(
        lambda: _async_return("tok"), transport=httpx.MockTransport(handler)
    )
    with pytest.raises(SubscriptionError, match="403"):
        await fetcher.fetch()


async def test_antigravity_fetcher_invalid_credentials_token():
    async def bad_token() -> str:
        raise TokenAcquireError.with_kind(
            "Google refresh_token 已失效",
            kind=TokenErrorKind.INVALID_CREDENTIALS,
            needs_reauth=True,
        )

    fetcher = AntigravityUsageFetcher(
        bad_token, transport=httpx.MockTransport(lambda r: httpx.Response(200, json={}))
    )
    with pytest.raises(SubscriptionError, match="重新登录"):
        await fetcher.fetch()


async def _async_return(value: str) -> str:
    return value


# ── 聚合服务 ─────────────────────────────────────────────────


class _FakeFetcher:
    def __init__(self, result=None, error: Exception | None = None) -> None:
        self.result = result or {"plan": "Fake", "windows": []}
        self.error = error
        self.calls = 0

    async def fetch(self) -> dict:
        self.calls += 1
        if self.error:
            raise self.error
        return dict(self.result)


async def test_service_ttl_cache_and_claude_waiting():
    glm = _FakeFetcher({"plan": "Pro", "windows": []})
    ag = _FakeFetcher(None)
    service = SubscriptionStatusService(
        config=None, router=None, zhipu_fetcher=glm, antigravity_fetcher=ag
    )
    status = await service.get_status()
    keys = [v["key"] for v in status["vendors"]]
    assert keys == ["claude", "glm", "antigravity"]
    assert status["vendors"][0]["status"] == "waiting"  # 无被动快照
    assert status["vendors"][1]["plan"] == "Pro"

    await service.get_status()
    assert glm.calls == 1 and ag.calls == 1  # TTL 内命中缓存

    capture_anthropic_ratelimit(_UNIFIED_HEADERS)
    status = await service.get_status()
    claude = status["vendors"][0]
    assert claude["status"] == "allowed"
    assert [w["id"] for w in claude["windows"]] == ["5h", "weekly"]


async def test_service_error_isolation():
    glm = _FakeFetcher(error=SubscriptionError("HTTP 429"))
    ag = _FakeFetcher({"plan": "Google AI Ultra", "windows": []})
    service = SubscriptionStatusService(
        config=None, router=None, zhipu_fetcher=glm, antigravity_fetcher=ag
    )
    status = await service.get_status()
    glm_entry, ag_entry = status["vendors"][1], status["vendors"][2]
    assert glm_entry["status"] == "error" and glm_entry["error"] == "HTTP 429"
    assert ag_entry["plan"] == "Google AI Ultra"

    # 错误同样入缓存：TTL 内不重试（防刷新风暴打未文档化端点）
    await service.get_status()
    assert glm.calls == 1


async def test_service_not_configured():
    service = SubscriptionStatusService(config=None, router=None)
    status = await service.get_status()
    assert status["vendors"][1]["status"] == "not_configured"
    assert status["vendors"][2]["status"] == "not_configured"


# ── 端点 ─────────────────────────────────────────────────────


def _minimal_app() -> TestClient:
    config = ProxyConfig(
        primary={"enabled": False},
        fallback={"enabled": True},
        database={"path": "/tmp/test-coding-proxy-subscriptions.db"},
    )
    return TestClient(create_app(config))


def test_subscriptions_endpoint_minimal_app():
    with _minimal_app() as client:
        resp = client.get("/api/dashboard/subscriptions")
        assert resp.status_code == 200
        vendors = resp.json()["vendors"]
        keys = [v["key"] for v in vendors]
        assert keys == ["claude", "glm", "antigravity"]
        assert vendors[0]["status"] == "waiting"
        assert vendors[1]["status"] == "not_configured"
        assert vendors[2]["status"] == "not_configured"


def test_subscriptions_endpoint_reflects_passive_snapshot():
    with _minimal_app() as client:
        capture_anthropic_ratelimit(_UNIFIED_HEADERS)
        resp = client.get("/api/dashboard/subscriptions")
        claude = resp.json()["vendors"][0]
        assert claude["windows"][0]["used_percent"] == pytest.approx(42.0)
        assert claude["windows"][0]["reset_epoch"] == 1731750000


# ── 畸形数值边界（NaN/Infinity） ────────────────────────────


def test_epoch_seconds_malformed_values():
    """NaN/Infinity/1e400 等畸形 reset 值不得抛异常，回落 None（窗口跳过倒计时）."""
    from coding.proxy.server.subscription_status import _epoch_seconds

    for bad in ("NaN", "Infinity", "-Infinity", "1e400", "nan", "inf", None, "", "x"):
        assert _epoch_seconds(bad) is None, bad


def test_unified_reset_malformed_values_tolerated():
    """被动链路 reset 头为 inf/nan 时解析为 None，不拖垮快照."""
    snap = parse_unified_ratelimit(
        {
            "anthropic-ratelimit-unified-5h-utilization": "0.5",
            "anthropic-ratelimit-unified-5h-reset": "Infinity",
        }
    )
    assert snap is not None
    assert snap.five_hour_percent == pytest.approx(50.0)
    assert snap.five_hour_reset_epoch is None
    # utilization 为 NaN 时窗口百分比解析为 None
    snap2 = parse_unified_ratelimit(
        {"anthropic-ratelimit-unified-7d-utilization": "NaN"}
    )
    assert snap2.seven_day_percent is None


async def test_zhipu_fetcher_malformed_reset_keeps_window():
    """GLM 畸形 reset 值只丢弃倒计时，不拖垮整家条目."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "data": {
                    "level": "pro",
                    "limits": [
                        {
                            "type": "TOKENS_LIMIT",
                            "percentage": 40,
                            "unit": 3,
                            "number": 5,
                            "nextResetTime": "NaN",
                        }
                    ],
                }
            },
        )

    result = await _zhipu_fetcher(handler).fetch()
    assert result["plan"] == "Pro"
    assert result["windows"][0]["used_percent"] == pytest.approx(40.0)
    assert result["windows"][0]["reset_epoch"] is None


# ── Antigravity fetcher 回退构建 ─────────────────────────────


def test_antigravity_fallback_builder_from_token_store():
    """vendor 未进链路时，用 config + token store 合成独立 token manager."""
    from types import SimpleNamespace

    from coding.proxy.server.subscription_status import _build_antigravity_fetcher

    entry = SimpleNamespace(
        vendor="antigravity",
        enabled=True,
        client_id="",
        client_secret="",
        refresh_token="",
    )
    config = SimpleNamespace(vendors=[entry])
    store = SimpleNamespace(
        get=lambda key: (
            SimpleNamespace(refresh_token="1//abc", scope="")
            if key == "google"
            else SimpleNamespace(refresh_token="", scope="")
        )
    )
    fetcher = _build_antigravity_fetcher(None, config, store)
    assert isinstance(fetcher, AntigravityUsageFetcher)
    # 配额查询 host 默认对齐 gateway 漂移敏感默认值
    assert fetcher._quota_summary_url.startswith(
        "https://daily-cloudcode-pa.googleapis.com/v1internal:retrieveUserQuotaSummary"
    )

    # 回退路径按凭证可得性判定，不看 entry.enabled（resolver 会为未配置用户合成
    # enabled=False 默认条目，若按 enabled 门控，gateway 用户永远「未启用」）
    disabled = SimpleNamespace(**{**vars(entry), "enabled": False})
    disabled_fetcher = _build_antigravity_fetcher(
        None, SimpleNamespace(vendors=[disabled]), store
    )
    assert isinstance(disabled_fetcher, AntigravityUsageFetcher)
    # config.antigravity_gateway.base_url 覆写被尊重
    override_cfg = SimpleNamespace(
        vendors=[entry],
        antigravity_gateway=SimpleNamespace(
            base_url="https://cloudcode-pa.googleapis.com"
        ),
    )
    fetcher2 = _build_antigravity_fetcher(None, override_cfg, store)
    assert fetcher2._quota_summary_url.startswith(
        "https://cloudcode-pa.googleapis.com/v1internal:retrieveUserQuotaSummary"
    )

    # token store 无凭证 → None
    empty_store = SimpleNamespace(
        get=lambda key: SimpleNamespace(refresh_token="", scope="")
    )
    assert _build_antigravity_fetcher(None, config, empty_store) is None
    # 缺 config/store 也 → None（仅 vendor 通道）
    assert _build_antigravity_fetcher(None, None, None) is None


def test_clamp_percent_nan_rejected_at_fetchers():
    """NaN/Infinity 百分比不进窗口（min/max 位置效应会伪造 100%，取值处拒绝）."""
    from coding.proxy.server.subscription_status import _is_finite_number

    assert not _is_finite_number(float("nan"))
    assert not _is_finite_number(float("inf"))
    assert _is_finite_number(0)
    assert _is_finite_number(100.5)
    assert not _is_finite_number("40")
    assert not _is_finite_number(None)


async def test_zhipu_nan_percentage_window_skipped():
    """GLM percentage=NaN 的窗口整体跳过，不显示伪造的 100%."""

    def handler(request: httpx.Request) -> httpx.Response:
        # 上游真实形态是 JSON 文本里的 NaN 字面量（json.loads 宽容解析为 nan；
        # httpx 的 json= 序列化拒绝 NaN，故用原始 content 模拟）
        return httpx.Response(
            200,
            content=(
                b'{"data": {"level": "pro", "limits": ['
                b'{"type": "TOKENS_LIMIT", "percentage": NaN, "unit": 3, "number": 5}'
                b"]}}"
            ),
            headers={"content-type": "application/json"},
        )

    result = await _zhipu_fetcher(handler).fetch()
    assert result["plan"] == "Pro"
    assert result["windows"] == []
