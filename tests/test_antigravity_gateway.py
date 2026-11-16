"""``antigravity_gateway`` 透传通道测试.

覆盖：

- 配置：env 覆写优先级、config.default.yaml 与 Pydantic 内置默认双写同步；
- v1internal 信封解析：请求信封（model / requestId → session_key）、
  非流式响应（裸 / ``{response: ...}`` 包裹两形态）、SSE 流式累加；
- 路径分类：冒号方法与 ``%3A`` 编码形态；
- 端到端（httpx.MockTransport + FastAPI TestClient）：字节级透传
  （body / query / 认证头 / hop-by-hop 清洗）、usage 落账参数
  （vendor / client_category / session_key / model）、上游异常 → 502、
  ``enabled=False`` → 404。
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
import yaml
from fastapi import FastAPI
from fastapi.testclient import TestClient

from coding.proxy.antigravity_gateway.config import AntigravityGatewayConfig
from coding.proxy.antigravity_gateway.envelope import (
    V1InternalStreamAccumulator,
    extract_v1internal_usage,
    parse_request_envelope,
)
from coding.proxy.antigravity_gateway.handler import (
    CLIENT_CATEGORY,
    VENDOR_LABEL,
    AntigravityGatewayHandler,
)
from coding.proxy.antigravity_gateway.routes import (
    register_antigravity_gateway_routes,
)

# ── 配置 ───────────────────────────────────────────────────────


def test_config_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(
        "ANTIGRAVITY_GATEWAY_BASE_URL", "https://cloudcode-pa.googleapis.com"
    )
    cfg = AntigravityGatewayConfig()
    assert cfg.base_url == "https://cloudcode-pa.googleapis.com"


def test_config_blank_env_keeps_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANTIGRAVITY_GATEWAY_BASE_URL", "   ")
    cfg = AntigravityGatewayConfig()
    assert cfg.base_url == "https://daily-cloudcode-pa.googleapis.com"


def test_default_config_yaml_matches_pydantic_builtin() -> None:
    """config.default.yaml 与 Pydantic 内置默认双写同步守护（同 native_api 模式）."""
    yaml_path = (
        Path(__file__).parent.parent
        / "src"
        / "coding"
        / "proxy"
        / "config"
        / "config.default.yaml"
    )
    data = yaml.safe_load(yaml_path.read_text(encoding="utf-8"))
    section = data["antigravity_gateway"]
    cfg = AntigravityGatewayConfig()
    assert section["enabled"] is cfg.enabled
    assert section["base_url"] == cfg.base_url
    assert section["timeout_ms"] == cfg.timeout_ms
    assert section["connect_timeout_ms"] == cfg.connect_timeout_ms


# ── 信封解析 ───────────────────────────────────────────────────


def _envelope_body(
    *,
    model: str = "gemini-3.8-flash",
    request_id: str = "agent/antigravity/abc123def4567890/7",
) -> bytes:
    return json.dumps(
        {
            "project": "aicode-consumers",
            "requestId": request_id,
            "model": model,
            "userAgent": "antigravity",
            "requestType": "agent",
            "request": {
                "contents": [],
                "systemInstruction": {
                    "parts": [
                        {
                            "text": "<user_information>Conversation ID: 3f2a1b0c-9d8e-4f7a-b6c5-d4e3f2a1b0c9</user_information>"
                        }
                    ]
                },
                "sessionId": "-3750763034362895579",
            },
        }
    ).encode()


def test_parse_request_envelope() -> None:
    env = parse_request_envelope(_envelope_body())
    assert env.matched is True
    assert env.model == "gemini-3.8-flash"
    assert env.session_key == "3f2a1b0c-9d8e-4f7a-b6c5-d4e3f2a1b0c9"
    assert env.request_id == "agent/antigravity/abc123def4567890/7"


def test_parse_request_envelope_non_envelope_body() -> None:
    env = parse_request_envelope(b'{"foo": 1}')
    assert env.matched is False
    assert env.model == ""
    assert env.session_key == ""
    env = parse_request_envelope(b"not-json")
    assert env.matched is False
    env = parse_request_envelope(None)
    assert env.matched is False


def test_parse_request_envelope_non_agent_request_id() -> None:
    # Conversation ID 标签（①级）优先于 requestId 派生，requestId 形态无关
    env = parse_request_envelope(_envelope_body(request_id="other/shape/1"))
    assert env.matched is True
    assert env.session_key == "3f2a1b0c-9d8e-4f7a-b6c5-d4e3f2a1b0c9"


def test_extract_v1internal_usage_wrapped() -> None:
    body = {
        "response": {
            "responseId": "resp-1",
            "modelVersion": "gemini-3.8-flash",
            "usageMetadata": {
                "promptTokenCount": 11,
                "candidatesTokenCount": 7,
                "cachedContentTokenCount": 3,
                "thoughtsTokenCount": 5,
                "totalTokenCount": 26,
            },
        },
        "traceId": "trace-1",
    }
    r = extract_v1internal_usage(body)
    assert r.input_tokens == 11
    assert r.output_tokens == 7
    assert r.cache_read_tokens == 3
    assert r.request_id == "resp-1"
    assert r.model_served == "gemini-3.8-flash"
    assert r.extra_usage == {"thoughts_tokens": 5, "total_token_count": 26}
    assert r.evidence_kind == "v1internal_usage_metadata"


def test_extract_v1internal_usage_bare() -> None:
    body = {
        "responseId": "resp-2",
        "modelVersion": "gemini-4-argon",
        "usageMetadata": {"promptTokenCount": 4, "candidatesTokenCount": 2},
    }
    r = extract_v1internal_usage(body)
    assert r.input_tokens == 4
    assert r.output_tokens == 2
    assert r.model_served == "gemini-4-argon"


def test_extract_v1internal_usage_no_usage() -> None:
    r = extract_v1internal_usage({"responseId": "r", "modelVersion": "m"})
    assert r.input_tokens == 0
    assert r.request_id == "r"


def test_stream_accumulator_wrapped_frames() -> None:
    acc = V1InternalStreamAccumulator()
    frames = [
        {"response": {"responseId": "resp-s", "candidates": []}, "traceId": "t0"},
        {
            "response": {
                "modelVersion": "gemini-3.8-flash",
                "usageMetadata": {
                    "promptTokenCount": 42,
                    "candidatesTokenCount": 13,
                    "thoughtsTokenCount": 17,
                },
            },
            "traceId": "t1",
        },
    ]
    acc.feed("\n\n".join(f"data: {json.dumps(f)}" for f in frames).encode() + b"\n\n")
    extraction, _evidence = acc.finalize(vendor=VENDOR_LABEL, model_served="fallback")
    assert extraction.input_tokens == 42
    assert extraction.output_tokens == 13
    assert extraction.extra_usage == {"thoughts_tokens": 17}
    assert extraction.request_id == "resp-s"
    assert extraction.model_served == "gemini-3.8-flash"


def test_stream_accumulator_bare_and_garbage() -> None:
    acc = V1InternalStreamAccumulator()
    acc.feed(b"data: [DONE]\n\n")
    acc.feed(b"data: not-json\n\n")
    acc.feed(
        b"data: "
        + json.dumps(
            {
                "usageMetadata": {"promptTokenCount": 5, "candidatesTokenCount": 6},
                "responseId": "rb",
                "modelVersion": "m2",
            }
        ).encode()
        + b"\n\n"
    )
    extraction, _ = acc.finalize(vendor=VENDOR_LABEL, model_served="")
    assert extraction.input_tokens == 5
    assert extraction.output_tokens == 6
    assert extraction.model_served == "m2"


def test_stream_accumulator_event_split_across_chunks() -> None:
    """事件跨网络 chunk 边界切断不丢 usage；末尾无空行由 finalize 冲刷."""
    payload = {
        "response": {
            "responseId": "rs",
            "modelVersion": "gemini-4-carbon",
            "usageMetadata": {"promptTokenCount": 88, "candidatesTokenCount": 33},
        },
        "traceId": "ts",
    }
    event = ("data: " + json.dumps(payload, ensure_ascii=False) + "\n\n").encode()
    acc = V1InternalStreamAccumulator()
    acc.feed(event[:17])
    acc.feed(event[17:])
    extraction, _ = acc.finalize(vendor=VENDOR_LABEL, model_served="")
    assert extraction.input_tokens == 88
    assert extraction.output_tokens == 33

    acc2 = V1InternalStreamAccumulator()
    third = max(1, len(event) // 3)
    acc2.feed(event[:third])
    acc2.feed(event[third : 2 * third])
    acc2.feed(event[2 * third :].rstrip(b"\n\n"))
    extraction2, _ = acc2.finalize(vendor=VENDOR_LABEL, model_served="")
    assert extraction2.input_tokens == 88


# ── 路径分类 ───────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("/v1internal:generateContent", "generate_content"),
        ("v1internal:streamGenerateContent", "generate_content"),
        ("/v1internal:countTokens", "count_tokens"),
        ("/v1internal:fetchAvailableModels", "model.list"),
        ("/v1internal:loadCodeAssist", "load_code_assist"),
        ("/v1internal:listExperiments", "list_experiments"),
        ("/v1internal%3AgenerateContent", "generate_content"),
        ("/v1internal:unknownVerb", "unknown"),
        ("/something/else", "unknown"),
    ],
)
def test_classify_v1internal_path(path: str, expected: str) -> None:
    from coding.proxy.antigravity_gateway.operation import classify_v1internal_path

    assert classify_v1internal_path("POST", path) == expected


# ── 端到端（MockTransport + TestClient + stub recorder） ───────


class _StubRecorder:
    """捕获 record() 调用参数的 UsageRecorder 替身."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    @staticmethod
    def build_usage_info(usage: dict[str, Any]) -> dict[str, Any]:
        return usage

    async def record(self, **kwargs: Any) -> None:
        self.calls.append(kwargs)


def _make_app(
    route,
    *,
    config: AntigravityGatewayConfig | None = None,
) -> Iterator[tuple[TestClient, list[httpx.Request], _StubRecorder]]:
    captured: list[httpx.Request] = []
    recorder = _StubRecorder()

    def _inner(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return route(request)

    cfg = config or AntigravityGatewayConfig(
        enabled=True, base_url="https://daily-cloudcode-pa.googleapis.com"
    )
    handler = AntigravityGatewayHandler(
        cfg,
        usage_recorder=recorder,  # type: ignore[arg-type]
        transport=httpx.MockTransport(_inner),
    )
    app = FastAPI()
    app.state.antigravity_handler = handler
    register_antigravity_gateway_routes(app, handler)

    with TestClient(app) as client:
        yield client, captured, recorder


def test_nonstream_passthrough_and_record() -> None:
    upstream_body = {
        "response": {
            "responseId": "resp-e2e",
            "modelVersion": "gemini-3.8-flash",
            "usageMetadata": {"promptTokenCount": 9, "candidatesTokenCount": 4},
        },
        "traceId": "t",
    }

    def route(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=upstream_body)

    for client, captured, recorder in _make_app(route):
        r = client.post(
            "/api/antigravity/v1internal:generateContent?alt=json",
            content=_envelope_body(),
            headers={"authorization": "Bearer ya29.test-token"},
        )
        assert r.status_code == 200
        assert r.json() == upstream_body  # 字节级（JSON 等价）透传

        assert len(captured) == 1
        upstream = captured[0]
        assert (
            str(upstream.url)
            == "https://daily-cloudcode-pa.googleapis.com/v1internal:generateContent?alt=json"
        )
        assert upstream.headers.get("authorization") == "Bearer ya29.test-token"
        assert upstream.headers.get("accept-encoding") == "identity"
        # 请求体未改写
        assert json.loads(upstream.content) == json.loads(_envelope_body())

        assert len(recorder.calls) == 1
        call = recorder.calls[0]
        assert call["vendor"] == VENDOR_LABEL
        assert call["client_category"] == CLIENT_CATEGORY
        assert call["operation"] == "generate_content"
        assert call["session_key"] == "3f2a1b0c-9d8e-4f7a-b6c5-d4e3f2a1b0c9"
        assert call["model_served"] == "gemini-3.8-flash"
        assert call["usage"]["input_tokens"] == 9
        assert call["usage"]["output_tokens"] == 4


def test_stream_passthrough_and_record() -> None:
    frames = [
        b'data: {"response": {"responseId": "resp-s1", "candidates": [{"content": {"parts": [{"text": "hi"}]}}]}, "traceId": "t0"}\n\n',
        b'data: {"response": {"modelVersion": "gemini-3.8-flash", "usageMetadata": {"promptTokenCount": 42, "candidatesTokenCount": 13, "thoughtsTokenCount": 17}}, "traceId": "t1"}\n\n',
        b"data: [DONE]\n\n",
    ]
    sse_body = b"".join(frames)

    def route(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content=sse_body,
            headers={"content-type": "text/event-stream"},
        )

    for client, captured, recorder in _make_app(route):
        with client.stream(
            "POST",
            "/api/antigravity/v1internal:streamGenerateContent?alt=sse",
            content=_envelope_body(),
        ) as resp:
            assert resp.status_code == 200
            received = b"".join(resp.iter_raw())
        assert received == sse_body  # SSE 字节级透传

        assert len(recorder.calls) == 1
        call = recorder.calls[0]
        assert call["operation"] == "generate_content"
        assert call["session_key"] == "3f2a1b0c-9d8e-4f7a-b6c5-d4e3f2a1b0c9"
        assert call["usage"]["input_tokens"] == 42
        assert call["usage"]["output_tokens"] == 13
        assert call["extra_usage"] == {"thoughts_tokens": 17}
        assert call["model_served"] == "gemini-3.8-flash"
        # 流式 evidence 归档非空（usage 帧 → _usage_evidence → log_evidence）
        assert call["evidence_records"], "streaming usage evidence must not be empty"
        assert call["evidence_records"][0]["vendor"] == VENDOR_LABEL


def test_upstream_unreachable_records_failure() -> None:
    def route(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    for client, captured, recorder in _make_app(route):
        r = client.post(
            "/api/antigravity/v1internal:generateContent",
            content=_envelope_body(),
        )
        assert r.status_code == 502
        assert r.json()["error"]["type"] == "api_error"
        assert len(recorder.calls) == 1
        call = recorder.calls[0]
        assert call["success"] is False
        assert call["client_category"] == CLIENT_CATEGORY
        assert call["session_key"] == "3f2a1b0c-9d8e-4f7a-b6c5-d4e3f2a1b0c9"
        assert call["extra_usage"]["failure_reason"]


def test_disabled_returns_404() -> None:
    def route(request: httpx.Request) -> httpx.Response:  # pragma: no cover
        raise AssertionError("upstream must not be reached when disabled")

    cfg = AntigravityGatewayConfig(enabled=False)
    for client, captured, recorder in _make_app(route, config=cfg):
        r = client.post("/api/antigravity/v1internal:generateContent", content=b"{}")
        assert r.status_code == 404
        assert r.json()["error"]["type"] == "not_found"
        assert captured == []
        assert recorder.calls == []


def test_upstream_error_status_passthrough() -> None:
    def route(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, json={"error": {"code": 403, "message": "denied"}})

    for client, captured, recorder in _make_app(route):
        r = client.post(
            "/api/antigravity/v1internal:generateContent", content=_envelope_body()
        )
        assert r.status_code == 403
        assert r.json()["error"]["message"] == "denied"
        assert recorder.calls[0]["success"] is False


def test_non_envelope_request_still_relays() -> None:
    """非信封请求体（如 loadCodeAssist 等其他 v1internal 端点）照常透传."""

    def route(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"metadata": {}})

    for client, captured, recorder in _make_app(route):
        r = client.post(
            "/api/antigravity/v1internal:loadCodeAssist", content=b'{"meta": true}'
        )
        assert r.status_code == 200
        call = recorder.calls[0]
        assert call["session_key"] == ""
        assert call["model_served"] == "unknown"
        assert call["operation"] == "load_code_assist"


def test_parse_request_envelope_session_fallbacks() -> None:
    # 无 Conversation ID → agent/antigravity/{sid}/{seq} 第三段
    env = parse_request_envelope(
        _envelope_body(request_id="agent/antigravity/feedface00112233/9").replace(
            b'"contents": [], "systemInstruction": {"parts": [{"text": "<user_information>Conversation ID: 3f2a1b0c-9d8e-4f7a-b6c5-d4e3f2a1b0c9</user_information>"}]}, "sessionId": "-3750763034362895579"',
            b'"contents": []',
        )
    )
    assert env.session_key == "feedface00112233"
    # 原生抓包形态 agent/{uuid}
    env2 = parse_request_envelope(
        _envelope_body(request_id="agent/8d3f2a10-1234-4abc-9def-aabbccddeeff").replace(
            b'"contents": [], "systemInstruction": {"parts": [{"text": "<user_information>Conversation ID: 3f2a1b0c-9d8e-4f7a-b6c5-d4e3f2a1b0c9</user_information>"}]}, "sessionId": "-3750763034362895579"',
            b'"contents": []',
        )
    )
    assert env2.session_key == "8d3f2a10-1234-4abc-9def-aabbccddeeff"


def test_extract_v1internal_usage_count_tokens() -> None:
    r = extract_v1internal_usage(
        {"response": {"totalTokens": 11}, "traceId": "tc", "metadata": {}}
    )
    assert r.input_tokens == 11
    assert r.evidence_kind == "v1internal_count_tokens"


def test_stream_accumulator_multiline_and_crlf_frames() -> None:
    """真实流存在 pretty-printed 多行 data 帧 + CRLF 行尾（bili #3331）."""
    acc = V1InternalStreamAccumulator()
    payload = {
        "response": {
            "responseId": "resp-m",
            "modelVersion": "gemini-4-argon",
            "usageMetadata": {"promptTokenCount": 77, "candidatesTokenCount": 21},
        },
        "traceId": "tm",
        "metadata": {},
    }
    pretty = json.dumps(payload, indent=2)
    chunk = ("data: " + pretty.replace("\n", "\ndata: ") + "\r\n\r\n").encode()
    acc.feed(chunk)
    extraction, _ = acc.finalize(vendor=VENDOR_LABEL, model_served="")
    assert extraction.input_tokens == 77
    assert extraction.output_tokens == 21
    assert extraction.model_served == "gemini-4-argon"
    assert extraction.request_id == "resp-m"


def test_classify_control_plane_endpoints() -> None:
    from coding.proxy.antigravity_gateway.operation import classify_v1internal_path

    assert classify_v1internal_path("POST", "/v1internal:fetchUserInfo") == "user.info"
    assert (
        classify_v1internal_path("POST", "/v1internal:retrieveUserQuotaSummary")
        == "quota.summary"
    )
    assert (
        classify_v1internal_path("POST", "/v1internal:fetchAdminControls")
        == "admin_controls"
    )
    assert (
        classify_v1internal_path("POST", "/v1internal:writeTrajectoryAcls")
        == "write_trajectory_acls"
    )


@pytest.mark.asyncio
async def test_usage_recorder_evidence_gate_for_antigravity() -> None:
    """client_category='antigravity' 的 evidence 全量归档 gate（真 UsageRecorder 驱动）."""
    from unittest.mock import AsyncMock

    from coding.proxy.model.vendor import UsageInfo
    from coding.proxy.routing.usage_recorder import UsageRecorder

    token_logger = AsyncMock()
    token_logger.log = AsyncMock()
    token_logger.log_evidence = AsyncMock()
    recorder = UsageRecorder(token_logger=token_logger, pricing_table=None)

    evidence = [
        {
            "vendor": VENDOR_LABEL,
            "request_id": "r",
            "model_served": "m",
            "evidence_kind": "v1internal_usage_metadata",
            "raw_usage_json": "{}",
            "parsed_input_tokens": 1,
            "parsed_output_tokens": 2,
            "parsed_cache_creation_tokens": 0,
            "parsed_cache_read_tokens": 0,
            "cache_signal_present": False,
            "source_field_map_json": "{}",
        }
    ]
    await recorder.record(
        vendor=VENDOR_LABEL,
        model_requested="m",
        model_served="m",
        usage=UsageInfo(input_tokens=1, output_tokens=2, request_id="r"),
        duration_ms=5,
        success=True,
        failover=False,
        evidence_records=evidence,
        client_category=CLIENT_CATEGORY,
        operation="generate_content",
        endpoint="/v1internal:generateContent",
    )
    token_logger.log.assert_awaited_once()
    token_logger.log_evidence.assert_awaited_once_with(**evidence[0])


def test_nonstream_aread_failure_records_failure() -> None:
    """非流式读体阶段断连：502 + failure 落账（与 send 阶段同口径）."""

    class _BrokenStream(httpx.AsyncByteStream):
        async def __aiter__(self):  # type: ignore[override]
            yield b'{"response": {"resp'
            raise httpx.ReadError("connection reset mid-body")

    def route(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, stream=_BrokenStream())

    for client, captured, recorder in _make_app(route):
        r = client.post(
            "/api/antigravity/v1internal:generateContent",
            content=_envelope_body(),
        )
        assert r.status_code == 502
        assert r.json()["error"]["type"] == "api_error"
        assert len(recorder.calls) == 1
        call = recorder.calls[0]
        assert call["success"] is False
        assert call["client_category"] == CLIENT_CATEGORY
        assert call["session_key"] == "3f2a1b0c-9d8e-4f7a-b6c5-d4e3f2a1b0c9"
        assert "connection reset" in call["extra_usage"]["failure_reason"]
