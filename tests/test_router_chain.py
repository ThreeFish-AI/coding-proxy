"""N-tier 链式路由器单元测试."""

from __future__ import annotations

from collections.abc import AsyncIterator
from unittest.mock import AsyncMock

import httpx
import pytest

from coding.proxy.config.schema import CopilotConfig, FailoverConfig
from coding.proxy.routing.circuit_breaker import CircuitBreaker
from coding.proxy.routing.quota_guard import QuotaGuard
from coding.proxy.routing.router import RequestRouter
from coding.proxy.routing.tier import VendorTier
from coding.proxy.vendors.base import (
    BaseVendor,
    UsageInfo,
    VendorCapabilities,
    VendorResponse,
)
from coding.proxy.vendors.copilot import CopilotVendor
from coding.proxy.vendors.token_manager import TokenAcquireError

# --- 测试用 Mock 供应商 ---


class FakeVendor(BaseVendor):
    """可配置行为的假供应商."""

    def __init__(
        self,
        name: str = "fake",
        response: VendorResponse | None = None,
        stream_chunks: list[bytes] | None = None,
        raise_on_call: Exception | None = None,
    ) -> None:
        super().__init__("http://fake", 30000)
        self._name = name
        self._response = response or VendorResponse()
        self._stream_chunks = stream_chunks or []
        self._raise_on_call = raise_on_call
        self.call_count = 0

    def get_name(self) -> str:
        return self._name

    async def _prepare_request(self, request_body, headers):
        return request_body, headers

    async def send_message(self, request_body, headers) -> VendorResponse:
        self.call_count += 1
        if self._raise_on_call:
            raise self._raise_on_call
        return self._response

    async def send_message_stream(self, request_body, headers) -> AsyncIterator[bytes]:
        self.call_count += 1
        if self._raise_on_call:
            raise self._raise_on_call
        for chunk in self._stream_chunks:
            yield chunk

    async def close(self) -> None:
        pass


def _body() -> dict:
    return {"model": "claude-sonnet-4-20250514", "messages": []}


def _headers() -> dict:
    return {"authorization": "Bearer test"}


# --- route_message 测试 ---


@pytest.mark.asyncio
async def test_route_message_primary_success():
    """首层成功 → 直接返回."""
    b0 = FakeVendor(
        "primary",
        VendorResponse(
            status_code=200, usage=UsageInfo(input_tokens=10, output_tokens=5)
        ),
    )
    b1 = FakeVendor("fallback")
    router = RequestRouter(
        [
            VendorTier(vendor=b0, circuit_breaker=CircuitBreaker()),
            VendorTier(vendor=b1),
        ]
    )
    resp = await router.route_message(_body(), _headers())
    assert resp.status_code == 200
    assert b0.call_count == 1
    assert b1.call_count == 0


@pytest.mark.asyncio
async def test_route_message_failover_to_tier1():
    """首层 429 → 次层接管."""
    b0 = FakeVendor(
        "primary",
        VendorResponse(
            status_code=429,
            error_type="rate_limit_error",
            error_message="Rate limited",
        ),
    )
    # 为 primary 配置 failover
    from coding.proxy.config.schema import FailoverConfig

    b0._failover_config = FailoverConfig()

    b1 = FakeVendor(
        "copilot", VendorResponse(status_code=200, usage=UsageInfo(input_tokens=20))
    )
    b2 = FakeVendor("zhipu")

    router = RequestRouter(
        [
            VendorTier(vendor=b0, circuit_breaker=CircuitBreaker()),
            VendorTier(vendor=b1, circuit_breaker=CircuitBreaker()),
            VendorTier(vendor=b2),
        ]
    )
    resp = await router.route_message(_body(), _headers())
    assert resp.status_code == 200
    assert b0.call_count == 1
    assert b1.call_count == 1
    assert b2.call_count == 0


@pytest.mark.asyncio
async def test_route_message_read_error_failover_to_tier1():
    """首层 ReadError → 次层接管."""
    b0 = FakeVendor("primary", raise_on_call=httpx.ReadError("boom"))
    b1 = FakeVendor(
        "copilot", VendorResponse(status_code=200, usage=UsageInfo(input_tokens=20))
    )

    router = RequestRouter(
        [
            VendorTier(vendor=b0, circuit_breaker=CircuitBreaker()),
            VendorTier(vendor=b1),
        ]
    )

    resp = await router.route_message(_body(), _headers())

    assert resp.status_code == 200
    assert b0.call_count == 1
    assert b1.call_count == 1


@pytest.mark.asyncio
async def test_route_message_failover_to_terminal():
    """前两层都失败 → 终端层接管."""
    from coding.proxy.config.schema import FailoverConfig

    b0 = FakeVendor(
        "primary",
        VendorResponse(
            status_code=429, error_type="rate_limit_error", error_message="limit"
        ),
    )
    b0._failover_config = FailoverConfig()

    b1 = FakeVendor(
        "copilot",
        VendorResponse(
            status_code=503, error_type="overloaded_error", error_message="overloaded"
        ),
    )
    b1._failover_config = FailoverConfig()

    b2 = FakeVendor(
        "zhipu", VendorResponse(status_code=200, usage=UsageInfo(input_tokens=30))
    )

    router = RequestRouter(
        [
            VendorTier(vendor=b0, circuit_breaker=CircuitBreaker()),
            VendorTier(vendor=b1, circuit_breaker=CircuitBreaker()),
            VendorTier(vendor=b2),
        ]
    )
    resp = await router.route_message(_body(), _headers())
    assert resp.status_code == 200
    assert b0.call_count == 1
    assert b1.call_count == 1
    assert b2.call_count == 1


@pytest.mark.asyncio
async def test_route_message_connection_error_failover():
    """连接异常 → 故障转移到下一层."""
    b0 = FakeVendor("primary", raise_on_call=httpx.ConnectError("connection refused"))
    b1 = FakeVendor("fallback", VendorResponse(status_code=200))

    router = RequestRouter(
        [
            VendorTier(vendor=b0, circuit_breaker=CircuitBreaker()),
            VendorTier(vendor=b1),
        ]
    )
    resp = await router.route_message(_body(), _headers())
    assert resp.status_code == 200
    assert b0.call_count == 1
    assert b1.call_count == 1


@pytest.mark.asyncio
async def test_route_message_last_tier_raises():
    """终端层也失败 → 抛出异常."""
    b0 = FakeVendor("primary", raise_on_call=httpx.ConnectError("refused"))
    b1 = FakeVendor("fallback", raise_on_call=httpx.ConnectError("also refused"))

    router = RequestRouter(
        [
            VendorTier(vendor=b0, circuit_breaker=CircuitBreaker()),
            VendorTier(vendor=b1),
        ]
    )
    with pytest.raises(httpx.ConnectError):
        await router.route_message(_body(), _headers())


@pytest.mark.asyncio
async def test_circuit_open_skips_tier():
    """CB OPEN 的层被跳过."""
    b0 = FakeVendor("primary", VendorResponse(status_code=200))
    b1 = FakeVendor("fallback", VendorResponse(status_code=200))

    cb0 = CircuitBreaker(failure_threshold=1)
    cb0.record_failure()  # OPEN

    router = RequestRouter(
        [
            VendorTier(vendor=b0, circuit_breaker=cb0),
            VendorTier(vendor=b1),
        ]
    )
    resp = await router.route_message(_body(), _headers())
    assert resp.status_code == 200
    assert b0.call_count == 0  # 被跳过
    assert b1.call_count == 1


@pytest.mark.asyncio
async def test_quota_exceeded_skips_tier():
    """QG EXCEEDED 的层被跳过."""
    b0 = FakeVendor("primary", VendorResponse(status_code=200))
    b1 = FakeVendor("fallback", VendorResponse(status_code=200))

    qg = QuotaGuard(
        enabled=True,
        token_budget=100,
        window_seconds=3600,
        probe_interval_seconds=99999,
    )
    qg.notify_cap_error()

    router = RequestRouter(
        [
            VendorTier(vendor=b0, circuit_breaker=CircuitBreaker(), quota_guard=qg),
            VendorTier(vendor=b1),
        ]
    )
    resp = await router.route_message(_body(), _headers())
    assert resp.status_code == 200
    assert b0.call_count == 0
    assert b1.call_count == 1


@pytest.mark.asyncio
async def test_all_non_terminal_skipped_reaches_terminal():
    """所有非终端层不可用 → 直达终端."""
    b0 = FakeVendor("primary")
    b1 = FakeVendor("copilot")
    b2 = FakeVendor("zhipu", VendorResponse(status_code=200))

    cb0 = CircuitBreaker(failure_threshold=1)
    cb0.record_failure()

    cb1 = CircuitBreaker(failure_threshold=1)
    cb1.record_failure()

    router = RequestRouter(
        [
            VendorTier(vendor=b0, circuit_breaker=cb0),
            VendorTier(vendor=b1, circuit_breaker=cb1),
            VendorTier(vendor=b2),
        ]
    )
    resp = await router.route_message(_body(), _headers())
    assert resp.status_code == 200
    assert b0.call_count == 0
    assert b1.call_count == 0
    assert b2.call_count == 1


@pytest.mark.asyncio
async def test_last_tier_always_tried_even_if_unavailable():
    """最后一层即使不可用也会尝试（终端保障）."""
    b0 = FakeVendor("primary", raise_on_call=httpx.ConnectError("refused"))
    # 终端层无 CB/QG，始终被执行
    b1 = FakeVendor("fallback", VendorResponse(status_code=200))

    router = RequestRouter(
        [
            VendorTier(vendor=b0, circuit_breaker=CircuitBreaker()),
            VendorTier(vendor=b1),
        ]
    )
    await router.route_message(_body(), _headers())
    assert b1.call_count == 1


# --- route_stream 测试 ---


@pytest.mark.asyncio
async def test_route_stream_primary_success():
    """流式：首层成功."""
    chunks = [b"data: {}\n\n", b"data: [DONE]\n\n"]
    b0 = FakeVendor("primary", stream_chunks=chunks)
    b1 = FakeVendor("fallback")

    router = RequestRouter(
        [
            VendorTier(vendor=b0, circuit_breaker=CircuitBreaker()),
            VendorTier(vendor=b1),
        ]
    )

    collected = []
    async for chunk, name in router.route_stream(_body(), _headers()):
        collected.append((chunk, name))

    assert len(collected) == 2
    assert collected[0][1] == "primary"
    assert b1.call_count == 0


@pytest.mark.asyncio
async def test_route_stream_failover():
    """流式：首层异常 → 次层接管."""
    b0 = FakeVendor("primary", raise_on_call=httpx.ConnectError("refused"))
    b1 = FakeVendor("fallback", stream_chunks=[b"data: ok\n\n"])

    router = RequestRouter(
        [
            VendorTier(vendor=b0, circuit_breaker=CircuitBreaker()),
            VendorTier(vendor=b1),
        ]
    )

    collected = []
    async for chunk, name in router.route_stream(_body(), _headers()):
        collected.append((chunk, name))

    assert len(collected) == 1
    assert collected[0][1] == "fallback"


@pytest.mark.asyncio
async def test_route_stream_read_error_failover():
    """流式：首层 ReadError → 次层接管."""
    b0 = FakeVendor("primary", raise_on_call=httpx.ReadError("boom"))
    b1 = FakeVendor("fallback", stream_chunks=[b"data: ok\n\n"])

    router = RequestRouter(
        [
            VendorTier(vendor=b0, circuit_breaker=CircuitBreaker()),
            VendorTier(vendor=b1),
        ]
    )

    collected = []
    async for chunk, name in router.route_stream(_body(), _headers()):
        collected.append((chunk, name))

    assert len(collected) == 1
    assert collected[0][1] == "fallback"


@pytest.mark.asyncio
async def test_route_stream_all_fail_raises():
    """流式：所有层失败 → 抛出异常."""
    b0 = FakeVendor("primary", raise_on_call=httpx.ConnectError("refused"))
    b1 = FakeVendor("fallback", raise_on_call=httpx.ConnectError("also refused"))

    router = RequestRouter(
        [
            VendorTier(vendor=b0, circuit_breaker=CircuitBreaker()),
            VendorTier(vendor=b1),
        ]
    )

    with pytest.raises(httpx.ConnectError):
        async for _ in router.route_stream(_body(), _headers()):
            pass


# --- 构造与关闭 ---


def test_router_requires_at_least_one_tier():
    with pytest.raises(ValueError, match="至少需要一个供应商层级"):
        RequestRouter([])


@pytest.mark.asyncio
async def test_router_close_calls_all_vendors():
    b0 = FakeVendor("a")
    b1 = FakeVendor("b")
    b0.close = AsyncMock()
    b1.close = AsyncMock()

    router = RequestRouter(
        [
            VendorTier(vendor=b0),
            VendorTier(vendor=b1),
        ]
    )
    await router.close()
    b0.close.assert_awaited_once()
    b1.close.assert_awaited_once()


def test_router_tiers_property():
    tiers = [VendorTier(vendor=FakeVendor("a")), VendorTier(vendor=FakeVendor("b"))]
    router = RequestRouter(tiers)
    assert router.tiers is tiers


# --- 4-tier 路由链测试 ---


@pytest.mark.asyncio
async def test_four_tier_failover_chain():
    """4-tier 完整降级：anthropic→copilot→antigravity→zhipu."""
    from coding.proxy.config.schema import FailoverConfig

    b0 = FakeVendor(
        "anthropic",
        VendorResponse(
            status_code=429, error_type="rate_limit_error", error_message="limit"
        ),
    )
    b0._failover_config = FailoverConfig()

    b1 = FakeVendor(
        "copilot",
        VendorResponse(
            status_code=503, error_type="overloaded_error", error_message="overloaded"
        ),
    )
    b1._failover_config = FailoverConfig()

    b2 = FakeVendor(
        "antigravity",
        VendorResponse(
            status_code=403, error_type="api_error", error_message="forbidden"
        ),
    )
    b2._failover_config = FailoverConfig()

    b3 = FakeVendor(
        "zhipu", VendorResponse(status_code=200, usage=UsageInfo(input_tokens=50))
    )

    router = RequestRouter(
        [
            VendorTier(vendor=b0, circuit_breaker=CircuitBreaker()),
            VendorTier(vendor=b1, circuit_breaker=CircuitBreaker()),
            VendorTier(vendor=b2, circuit_breaker=CircuitBreaker()),
            VendorTier(vendor=b3),
        ]
    )
    resp = await router.route_message(_body(), _headers())
    assert resp.status_code == 200
    assert b0.call_count == 1
    assert b1.call_count == 1
    assert b2.call_count == 1
    assert b3.call_count == 1


@pytest.mark.asyncio
async def test_four_tier_antigravity_succeeds():
    """4-tier：前两层失败，antigravity 成功."""
    from coding.proxy.config.schema import FailoverConfig

    b0 = FakeVendor(
        "anthropic",
        VendorResponse(
            status_code=429, error_type="rate_limit_error", error_message="limit"
        ),
    )
    b0._failover_config = FailoverConfig()

    b1 = FakeVendor(
        "copilot",
        VendorResponse(
            status_code=429, error_type="rate_limit_error", error_message="limit"
        ),
    )
    b1._failover_config = FailoverConfig()

    b2 = FakeVendor(
        "antigravity",
        VendorResponse(
            status_code=200, usage=UsageInfo(input_tokens=40, output_tokens=20)
        ),
    )
    b3 = FakeVendor("zhipu")

    router = RequestRouter(
        [
            VendorTier(vendor=b0, circuit_breaker=CircuitBreaker()),
            VendorTier(vendor=b1, circuit_breaker=CircuitBreaker()),
            VendorTier(vendor=b2, circuit_breaker=CircuitBreaker()),
            VendorTier(vendor=b3),
        ]
    )
    resp = await router.route_message(_body(), _headers())
    assert resp.status_code == 200
    assert b0.call_count == 1
    assert b1.call_count == 1
    assert b2.call_count == 1
    assert b3.call_count == 0


@pytest.mark.asyncio
async def test_four_tier_all_non_terminal_skipped():
    """4-tier：所有非终端层 CB OPEN → 直达终端."""
    b0 = FakeVendor("anthropic")
    b1 = FakeVendor("copilot")
    b2 = FakeVendor("antigravity")
    b3 = FakeVendor("zhipu", VendorResponse(status_code=200))

    cbs = [CircuitBreaker(failure_threshold=1) for _ in range(3)]
    for cb in cbs:
        cb.record_failure()

    router = RequestRouter(
        [
            VendorTier(vendor=b0, circuit_breaker=cbs[0]),
            VendorTier(vendor=b1, circuit_breaker=cbs[1]),
            VendorTier(vendor=b2, circuit_breaker=cbs[2]),
            VendorTier(vendor=b3),
        ]
    )
    resp = await router.route_message(_body(), _headers())
    assert resp.status_code == 200
    assert b0.call_count == 0
    assert b1.call_count == 0
    assert b2.call_count == 0
    assert b3.call_count == 1


@pytest.mark.asyncio
async def test_four_tier_stream_failover():
    """4-tier 流式：前三层失败 → 终端接管."""
    b0 = FakeVendor("anthropic", raise_on_call=httpx.ConnectError("refused"))
    b1 = FakeVendor("copilot", raise_on_call=httpx.ConnectError("refused"))
    b2 = FakeVendor("antigravity", raise_on_call=httpx.ConnectError("refused"))
    b3 = FakeVendor("zhipu", stream_chunks=[b"data: ok\n\n"])

    router = RequestRouter(
        [
            VendorTier(vendor=b0, circuit_breaker=CircuitBreaker()),
            VendorTier(vendor=b1, circuit_breaker=CircuitBreaker()),
            VendorTier(vendor=b2, circuit_breaker=CircuitBreaker()),
            VendorTier(vendor=b3),
        ]
    )

    collected = []
    async for chunk, name in router.route_stream(_body(), _headers()):
        collected.append((chunk, name))

    assert len(collected) == 1
    assert collected[0][1] == "zhipu"
    assert b0.call_count == 1
    assert b1.call_count == 1
    assert b2.call_count == 1
    assert b3.call_count == 1


@pytest.mark.asyncio
async def test_four_tier_failover_when_copilot_token_acquire_fails():
    """Anthropic 429 后，Copilot token 获取失败仍应降级到 Antigravity."""
    from coding.proxy.config.schema import FailoverConfig

    b0 = FakeVendor(
        "anthropic",
        VendorResponse(
            status_code=429, error_type="rate_limit_error", error_message="limit"
        ),
    )
    b0._failover_config = FailoverConfig()

    b1 = FakeVendor(
        "copilot", raise_on_call=TokenAcquireError("Copilot token 交换返回非预期响应")
    )
    b2 = FakeVendor(
        "antigravity", VendorResponse(status_code=200, usage=UsageInfo(input_tokens=40))
    )
    b3 = FakeVendor("zhipu")

    router = RequestRouter(
        [
            VendorTier(vendor=b0, circuit_breaker=CircuitBreaker()),
            VendorTier(vendor=b1, circuit_breaker=CircuitBreaker()),
            VendorTier(vendor=b2, circuit_breaker=CircuitBreaker()),
            VendorTier(vendor=b3),
        ]
    )

    resp = await router.route_message(_body(), _headers())
    assert resp.status_code == 200
    assert b0.call_count == 1
    assert b1.call_count == 1
    assert b2.call_count == 1
    assert b3.call_count == 0


@pytest.mark.asyncio
async def test_four_tier_stream_failover_when_copilot_token_acquire_fails():
    """流式请求下，Copilot token 获取失败也应继续降级."""
    b0 = FakeVendor("anthropic", raise_on_call=httpx.ConnectError("refused"))
    b1 = FakeVendor(
        "copilot", raise_on_call=TokenAcquireError("Copilot token 交换返回非预期响应")
    )
    b2 = FakeVendor("antigravity", stream_chunks=[b"data: ok\n\n"])
    b3 = FakeVendor("zhipu")

    router = RequestRouter(
        [
            VendorTier(vendor=b0, circuit_breaker=CircuitBreaker()),
            VendorTier(vendor=b1, circuit_breaker=CircuitBreaker()),
            VendorTier(vendor=b2, circuit_breaker=CircuitBreaker()),
            VendorTier(vendor=b3),
        ]
    )

    collected = []
    async for chunk, name in router.route_stream(_body(), _headers()):
        collected.append((chunk, name))

    assert len(collected) == 1
    assert collected[0][1] == "antigravity"


@pytest.mark.asyncio
async def test_stream_failover_to_copilot_even_when_request_has_thinking():
    """Anthropic 429 且请求含 thinking 时，Copilot 仍应通过适配层接管."""
    from coding.proxy.config.schema import FailoverConfig as RouterFailoverConfig

    b0 = FakeVendor(
        "anthropic",
        raise_on_call=httpx.HTTPStatusError(
            "anthropic API error: 429",
            request=httpx.Request("POST", "https://api.anthropic.com/v1/messages"),
            response=httpx.Response(
                429,
                content=b'{"error":{"type":"rate_limit_error","message":"limited"}}',
                headers={"content-type": "application/json"},
                request=httpx.Request("POST", "https://api.anthropic.com/v1/messages"),
            ),
        ),
    )
    b0._failover_config = RouterFailoverConfig()

    copilot = CopilotVendor(CopilotConfig(github_token="ghp_test"), FailoverConfig())
    copilot.check_health = AsyncMock(return_value=True)  # type: ignore[method-assign]

    async def _copilot_stream(_body, _headers):
        yield b'event: message_start\ndata: {"type":"message_start","message":{"id":"msg_1","model":"claude-sonnet-4","usage":{"input_tokens":10}}}\n\n'
        yield b'event: message_stop\ndata: {"type":"message_stop"}\n\n'

    copilot.send_message_stream = _copilot_stream  # type: ignore[method-assign]

    router = RequestRouter(
        [
            VendorTier(vendor=b0, circuit_breaker=CircuitBreaker()),
            VendorTier(vendor=copilot, circuit_breaker=CircuitBreaker()),
        ]
    )

    collected: list[tuple[bytes, str]] = []
    async for chunk, name in router.route_stream(
        {
            **_body(),
            "thinking": {"budget_tokens": 512},
        },
        _headers(),
    ):
        collected.append((chunk, name))

    assert collected
    assert all(name == "copilot" for _, name in collected)


@pytest.mark.asyncio
async def test_stream_semantic_rejection_fails_over_without_opening_circuit():
    """400 invalid_request_error 允许切下一级，但不应污染上游熔断器."""
    request = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    response = httpx.Response(
        400,
        content=(
            b'{"error":{"type":"invalid_request_error",'
            b'"message":"messages.2.content.2.server_tool_use.id: String should match pattern '
            b'\\"^srvtoolu_[a-zA-Z0-9_]+$\\""}}'
        ),
        headers={"content-type": "application/json"},
        request=request,
    )
    b0 = FakeVendor(
        "anthropic",
        raise_on_call=httpx.HTTPStatusError(
            "anthropic API error: 400", request=request, response=response
        ),
    )
    b0._failover_config = FailoverConfig()

    b1 = FakeVendor(
        "zhipu",
        stream_chunks=[
            b'event: message_start\ndata: {"type":"message_start","message":{"id":"msg_1","model":"glm-5.1","usage":{"input_tokens":3}}}\n\n',
            b'event: message_stop\ndata: {"type":"message_stop"}\n\n',
        ],
    )
    cb0 = CircuitBreaker(failure_threshold=1)
    router = RequestRouter(
        [
            VendorTier(vendor=b0, circuit_breaker=cb0),
            VendorTier(vendor=b1),
        ]
    )

    collected: list[tuple[bytes, str]] = []
    async for chunk, name in router.route_stream(_body(), _headers()):
        collected.append((chunk, name))

    assert collected
    assert all(name == "zhipu" for _, name in collected)
    assert cb0.can_execute() is True
    assert b0.call_count == 1
    assert b1.call_count == 1


@pytest.mark.asyncio
async def test_nonstream_semantic_rejection_fails_over_without_opening_circuit():
    """非流式 400 invalid_request_error 同样不计入熔断失败."""
    b0 = FakeVendor(
        "anthropic",
        VendorResponse(
            status_code=400,
            error_type="invalid_request_error",
            error_message="messages.2.content.2.server_tool_use.id: should match pattern",
        ),
    )
    b0._failover_config = FailoverConfig()
    b1 = FakeVendor(
        "zhipu", VendorResponse(status_code=200, usage=UsageInfo(input_tokens=8))
    )
    cb0 = CircuitBreaker(failure_threshold=1)
    router = RequestRouter(
        [
            VendorTier(vendor=b0, circuit_breaker=cb0),
            VendorTier(vendor=b1),
        ]
    )

    resp = await router.route_message(_body(), _headers())

    assert resp.status_code == 200
    assert cb0.can_execute() is True
    assert b0.call_count == 1
    assert b1.call_count == 1


@pytest.mark.asyncio
async def test_incompatible_tool_request_skips_non_compatible_tiers():
    """带 tools 的请求不会静默降级到不兼容的 antigravity/zhipu."""
    b0 = FakeVendor(
        "copilot", VendorResponse(status_code=200, usage=UsageInfo(input_tokens=10))
    )
    b1 = FakeVendor("antigravity", VendorResponse(status_code=200))
    b2 = FakeVendor("zhipu", VendorResponse(status_code=200))

    b1.get_capabilities = lambda: VendorCapabilities(
        supports_tools=False,
        supports_thinking=False,
        supports_images=True,
    )
    b2.get_capabilities = lambda: VendorCapabilities(
        supports_tools=False,
        supports_thinking=False,
        supports_images=True,
        emits_vendor_tool_events=True,
    )

    router = RequestRouter(
        [
            VendorTier(vendor=b0, circuit_breaker=CircuitBreaker()),
            VendorTier(vendor=b1, circuit_breaker=CircuitBreaker()),
            VendorTier(vendor=b2),
        ]
    )

    resp = await router.route_message(
        {
            **_body(),
            "tools": [{"name": "analyze_image"}],
        },
        _headers(),
    )
    assert resp.status_code == 200
    assert b0.call_count == 1
    assert b1.call_count == 0
    assert b2.call_count == 0


# --- model_served 测试 ---


class MappingFakeVendor(FakeVendor):
    """带模型映射的假供应商."""

    def __init__(
        self,
        name: str = "mapping-fake",
        mapped_model: str = "glm-5.1",
        response: VendorResponse | None = None,
        stream_chunks: list[bytes] | None = None,
    ) -> None:
        super().__init__(name=name, response=response, stream_chunks=stream_chunks)
        self._mapped_model = mapped_model

    def map_model(self, model: str) -> str:
        return self._mapped_model


@pytest.mark.asyncio
async def test_route_message_model_served_from_response():
    """非流式：model_served 从响应体提取."""
    logger_mock = AsyncMock()
    resp = VendorResponse(
        status_code=200, usage=UsageInfo(input_tokens=10), model_served="glm-5.1"
    )
    vendor = FakeVendor("zhipu", response=resp)
    router = RequestRouter([VendorTier(vendor=vendor)], token_logger=logger_mock)

    await router.route_message(_body(), _headers())

    logger_mock.log.assert_awaited_once()
    call_kwargs = logger_mock.log.call_args
    assert call_kwargs[1]["model_requested"] == "claude-sonnet-4-20250514"
    assert call_kwargs[1]["model_served"] == "glm-5.1"


@pytest.mark.asyncio
async def test_route_message_model_served_fallback_when_none():
    """非流式：model_served 为 None 时 fallback 到请求模型名."""
    logger_mock = AsyncMock()
    resp = VendorResponse(status_code=200, usage=UsageInfo(input_tokens=10))
    vendor = FakeVendor("anthropic", response=resp)
    router = RequestRouter([VendorTier(vendor=vendor)], token_logger=logger_mock)

    await router.route_message(_body(), _headers())

    logger_mock.log.assert_awaited_once()
    call_kwargs = logger_mock.log.call_args
    assert call_kwargs[1]["model_requested"] == "claude-sonnet-4-20250514"
    assert call_kwargs[1]["model_served"] == "claude-sonnet-4-20250514"


@pytest.mark.asyncio
async def test_route_message_model_served_fallback_to_map_model():
    """非流式：model_served 为 None 且后端有映射时，fallback 到 map_model()."""
    logger_mock = AsyncMock()
    resp = VendorResponse(status_code=200, usage=UsageInfo(input_tokens=10))
    # model_served 默认为 None，但后端有模型映射
    vendor = MappingFakeVendor(
        name="zhipu",
        mapped_model="glm-4.5-air",
        response=resp,
    )
    router = RequestRouter([VendorTier(vendor=vendor)], token_logger=logger_mock)

    await router.route_message(
        {"model": "claude-haiku-4-5-20251001", "messages": []},
        _headers(),
    )

    logger_mock.log.assert_awaited_once()
    call_kwargs = logger_mock.log.call_args
    assert call_kwargs[1]["model_requested"] == "claude-haiku-4-5-20251001"
    assert call_kwargs[1]["model_served"] == "glm-4.5-air"


@pytest.mark.asyncio
async def test_route_stream_model_served_from_sse():
    """流式：model_served 从 SSE message_start 事件提取."""
    logger_mock = AsyncMock()
    sse_chunk = (
        b"event: message_start\n"
        b'data: {"type":"message_start","message":{"id":"msg_1","model":"glm-5.1","usage":{"input_tokens":10,"output_tokens":0}}}\n\n'
        b"data: [DONE]\n\n"
    )
    vendor = MappingFakeVendor(mapped_model="glm-5.1", stream_chunks=[sse_chunk])
    router = RequestRouter([VendorTier(vendor=vendor)], token_logger=logger_mock)

    collected = []
    async for chunk, name in router.route_stream(_body(), _headers()):
        collected.append(chunk)

    logger_mock.log.assert_awaited_once()
    call_kwargs = logger_mock.log.call_args
    assert call_kwargs[1]["model_requested"] == "claude-sonnet-4-20250514"
    assert call_kwargs[1]["model_served"] == "glm-5.1"


@pytest.mark.asyncio
async def test_route_stream_model_served_fallback_to_map_model():
    """流式：SSE 未提供 model 时，fallback to vendor.map_model()."""
    logger_mock = AsyncMock()
    chunks = [b"data: {}\n\n", b"data: [DONE]\n\n"]
    vendor = MappingFakeVendor(mapped_model="glm-4.5-air", stream_chunks=chunks)
    router = RequestRouter([VendorTier(vendor=vendor)], token_logger=logger_mock)

    async for _ in router.route_stream(
        {"model": "claude-haiku-4-5-20251001", "messages": []},
        _headers(),
    ):
        pass

    logger_mock.log.assert_awaited_once()
    call_kwargs = logger_mock.log.call_args
    assert call_kwargs[1]["model_requested"] == "claude-haiku-4-5-20251001"
    assert call_kwargs[1]["model_served"] == "glm-4.5-air"


@pytest.mark.asyncio
async def test_route_stream_model_served_identity_vendor():
    """流式：无映射后端，model_served 等于请求模型名."""
    logger_mock = AsyncMock()
    chunks = [b"data: {}\n\n", b"data: [DONE]\n\n"]
    vendor = FakeVendor("anthropic", stream_chunks=chunks)
    router = RequestRouter([VendorTier(vendor=vendor)], token_logger=logger_mock)

    async for _ in router.route_stream(_body(), _headers()):
        pass

    logger_mock.log.assert_awaited_once()
    call_kwargs = logger_mock.log.call_args
    assert call_kwargs[1]["model_requested"] == "claude-sonnet-4-20250514"
    assert call_kwargs[1]["model_served"] == "claude-sonnet-4-20250514"


@pytest.mark.asyncio
async def test_route_stream_copilot_logs_cache_evidence():
    """Copilot 流式请求应额外写入 cache evidence 记录."""
    logger_mock = AsyncMock()
    chunk = (
        b"event: message_start\n"
        b'data: {"type":"message_start","message":{"id":"msg_cache","model":"claude-sonnet-4","usage":{"input_tokens":25}}}\n\n'
        b"event: message_delta\n"
        b'data: {"type":"message_delta","delta":{"stop_reason":"end_turn","stop_sequence":null},"usage":{"output_tokens":3,"cache_read_input_tokens":12}}\n\n'
        b"data: [DONE]\n\n"
    )
    vendor = FakeVendor("copilot", stream_chunks=[chunk])
    router = RequestRouter([VendorTier(vendor=vendor)], token_logger=logger_mock)

    async for _ in router.route_stream(_body(), _headers()):
        pass

    logger_mock.log.assert_awaited_once()
    logger_mock.log_evidence.assert_awaited()
    evidence_kwargs = logger_mock.log_evidence.call_args[1]
    assert evidence_kwargs["vendor"] == "copilot"
    assert evidence_kwargs["request_id"] == "msg_cache"
    assert evidence_kwargs["parsed_cache_read_tokens"] == 12
    assert evidence_kwargs["cache_signal_present"] is True


@pytest.mark.asyncio
async def test_route_stream_cache_only_input_does_not_warn(caplog):
    """cache-only 输入信号不应被误判为缺失 usage."""
    logger_mock = AsyncMock()
    chunk = (
        b"event: message_start\n"
        b'data: {"type":"message_start","message":{"id":"msg_cache_only","model":"claude-haiku-4-5-20251001",'
        b'"usage":{"input_tokens":0,"cache_creation_input_tokens":1920,"cache_read_input_tokens":80488}}}\n\n'
        b"event: message_delta\n"
        b'data: {"type":"message_delta","delta":{"stop_reason":"end_turn","stop_sequence":null},"usage":{"output_tokens":368}}\n\n'
        b"data: [DONE]\n\n"
    )
    vendor = FakeVendor("anthropic", stream_chunks=[chunk])
    router = RequestRouter([VendorTier(vendor=vendor)], token_logger=logger_mock)

    caplog.set_level("WARNING")

    async for _ in router.route_stream(_body(), _headers()):
        pass

    assert "missing input usage signals" not in caplog.text
    logger_mock.log.assert_awaited_once()


@pytest.mark.asyncio
async def test_route_stream_missing_input_signals_still_warns(caplog):
    """真正缺失所有输入信号时，仍应保留 WARNING."""
    logger_mock = AsyncMock()
    chunk = (
        b"event: message_delta\n"
        b'data: {"type":"message_delta","delta":{"stop_reason":"end_turn","stop_sequence":null},"usage":{"output_tokens":9}}\n\n'
        b"data: [DONE]\n\n"
    )
    vendor = FakeVendor("anthropic", stream_chunks=[chunk])
    router = RequestRouter([VendorTier(vendor=vendor)], token_logger=logger_mock)

    caplog.set_level("WARNING")

    async for _ in router.route_stream(_body(), _headers()):
        pass

    assert "missing input usage signals" in caplog.text
    logger_mock.log.assert_awaited_once()


@pytest.mark.asyncio
async def test_route_message_non_copilot_does_not_log_evidence():
    """非 Copilot 后端不应写 usage evidence."""
    logger_mock = AsyncMock()
    vendor = FakeVendor(
        "anthropic",
        response=VendorResponse(status_code=200, usage=UsageInfo(input_tokens=10)),
    )
    router = RequestRouter([VendorTier(vendor=vendor)], token_logger=logger_mock)

    await router.route_message(_body(), _headers())

    logger_mock.log.assert_awaited_once()
    logger_mock.log_evidence.assert_not_awaited()


# --- 故障转移语义测试 ---


@pytest.mark.asyncio
async def test_failover_records_source():
    """真正故障转移时 failover_from 记录来源后端."""
    from coding.proxy.config.schema import FailoverConfig

    logger_mock = AsyncMock()
    b0 = FakeVendor(
        "anthropic",
        VendorResponse(
            status_code=429, error_type="rate_limit_error", error_message="limit"
        ),
    )
    b0._failover_config = FailoverConfig()
    b1 = FakeVendor(
        "zhipu", VendorResponse(status_code=200, usage=UsageInfo(input_tokens=10))
    )

    router = RequestRouter(
        [
            VendorTier(vendor=b0, circuit_breaker=CircuitBreaker()),
            VendorTier(vendor=b1),
        ],
        token_logger=logger_mock,
    )

    resp = await router.route_message(_body(), _headers())
    assert resp.status_code == 200

    # zhipu 的记录应为 failover=True, failover_from="anthropic"
    logger_mock.log.assert_awaited_once()
    call_kwargs = logger_mock.log.call_args[1]
    assert call_kwargs["vendor"] == "zhipu"
    assert call_kwargs["failover"] is True
    assert call_kwargs["failover_from"] == "anthropic"


@pytest.mark.asyncio
async def test_circuit_open_not_counted_as_failover():
    """CB OPEN 跳过后的请求不算故障转移."""
    logger_mock = AsyncMock()
    b0 = FakeVendor("anthropic", VendorResponse(status_code=200))
    b1 = FakeVendor(
        "zhipu", VendorResponse(status_code=200, usage=UsageInfo(input_tokens=10))
    )

    cb0 = CircuitBreaker(failure_threshold=1)
    cb0.record_failure()  # anthropic CB OPEN

    router = RequestRouter(
        [
            VendorTier(vendor=b0, circuit_breaker=cb0),
            VendorTier(vendor=b1),
        ],
        token_logger=logger_mock,
    )

    resp = await router.route_message(_body(), _headers())
    assert resp.status_code == 200
    assert b0.call_count == 0  # 被跳过

    # 稳定降级：failover=False, failover_from=None
    logger_mock.log.assert_awaited_once()
    call_kwargs = logger_mock.log.call_args[1]
    assert call_kwargs["failover"] is False
    assert call_kwargs["failover_from"] is None


@pytest.mark.asyncio
async def test_multi_tier_failover_tracks_source():
    """多级故障转移记录最近失败来源."""
    from coding.proxy.config.schema import FailoverConfig

    logger_mock = AsyncMock()
    b0 = FakeVendor(
        "anthropic",
        VendorResponse(
            status_code=429, error_type="rate_limit_error", error_message="limit"
        ),
    )
    b0._failover_config = FailoverConfig()
    b1 = FakeVendor(
        "copilot",
        VendorResponse(
            status_code=503, error_type="overloaded_error", error_message="overloaded"
        ),
    )
    b1._failover_config = FailoverConfig()
    b2 = FakeVendor(
        "zhipu", VendorResponse(status_code=200, usage=UsageInfo(input_tokens=30))
    )

    router = RequestRouter(
        [
            VendorTier(vendor=b0, circuit_breaker=CircuitBreaker()),
            VendorTier(vendor=b1, circuit_breaker=CircuitBreaker()),
            VendorTier(vendor=b2),
        ],
        token_logger=logger_mock,
    )

    resp = await router.route_message(_body(), _headers())
    assert resp.status_code == 200

    logger_mock.log.assert_awaited_once()
    call_kwargs = logger_mock.log.call_args[1]
    assert call_kwargs["failover"] is True
    assert call_kwargs["failover_from"] == "copilot"  # 最近失败的 tier


@pytest.mark.asyncio
async def test_stream_failover_records_source():
    """流式故障转移记录来源."""
    logger_mock = AsyncMock()
    b0 = FakeVendor("anthropic", raise_on_call=httpx.ConnectError("refused"))
    b1 = FakeVendor("zhipu", stream_chunks=[b"data: ok\n\n"])

    router = RequestRouter(
        [
            VendorTier(vendor=b0, circuit_breaker=CircuitBreaker()),
            VendorTier(vendor=b1),
        ],
        token_logger=logger_mock,
    )

    async for _ in router.route_stream(_body(), _headers()):
        pass

    logger_mock.log.assert_awaited_once()
    call_kwargs = logger_mock.log.call_args[1]
    assert call_kwargs["failover"] is True
    assert call_kwargs["failover_from"] == "anthropic"


@pytest.mark.asyncio
async def test_stream_circuit_open_not_failover():
    """流式：CB OPEN 跳过后不算故障转移."""
    logger_mock = AsyncMock()
    b0 = FakeVendor("anthropic", stream_chunks=[b"data: ok\n\n"])
    b1 = FakeVendor("zhipu", stream_chunks=[b"data: ok\n\n"])

    cb0 = CircuitBreaker(failure_threshold=1)
    cb0.record_failure()

    router = RequestRouter(
        [
            VendorTier(vendor=b0, circuit_breaker=cb0),
            VendorTier(vendor=b1),
        ],
        token_logger=logger_mock,
    )

    async for _ in router.route_stream(_body(), _headers()):
        pass

    logger_mock.log.assert_awaited_once()
    call_kwargs = logger_mock.log.call_args[1]
    assert call_kwargs["failover"] is False
    assert call_kwargs["failover_from"] is None


@pytest.mark.asyncio
async def test_primary_success_no_failover():
    """首层成功：无故障转移."""
    logger_mock = AsyncMock()
    b0 = FakeVendor(
        "anthropic", VendorResponse(status_code=200, usage=UsageInfo(input_tokens=10))
    )

    router = RequestRouter(
        [
            VendorTier(vendor=b0, circuit_breaker=CircuitBreaker()),
        ],
        token_logger=logger_mock,
    )

    await router.route_message(_body(), _headers())

    logger_mock.log.assert_awaited_once()
    call_kwargs = logger_mock.log.call_args[1]
    assert call_kwargs["failover"] is False
    assert call_kwargs["failover_from"] is None


# --- Rate Limit Deadline 集成测试 ---


@pytest.mark.asyncio
async def test_rate_limit_deadline_prevents_premature_probe():
    """CB HALF_OPEN 但 rate limit deadline 未到期 → 不探测，直达终端."""
    import time

    b0 = FakeVendor("anthropic", VendorResponse(status_code=200))
    b1 = FakeVendor("zhipu", VendorResponse(status_code=200))

    cb0 = CircuitBreaker(failure_threshold=1, recovery_timeout_seconds=0)
    cb0.record_failure()  # → OPEN → 立即 HALF_OPEN (recovery=0)

    tier0 = VendorTier(vendor=b0, circuit_breaker=cb0)
    # 设置一个远未到期的 deadline
    tier0._rate_limit_deadline = time.monotonic() + 300

    router = RequestRouter([tier0, VendorTier(vendor=b1)])
    resp = await router.route_message(_body(), _headers())

    assert resp.status_code == 200
    assert b0.call_count == 0  # deadline 阻止了探测
    assert b1.call_count == 1  # 降级到终端


@pytest.mark.asyncio
async def test_rate_limit_deadline_allows_probe_after_expiry():
    """rate limit deadline 已过期 → 允许探测，恢复正常路由."""
    import time

    b0 = FakeVendor(
        "anthropic", VendorResponse(status_code=200, usage=UsageInfo(input_tokens=10))
    )
    b1 = FakeVendor("zhipu", VendorResponse(status_code=200))

    cb0 = CircuitBreaker(failure_threshold=1, recovery_timeout_seconds=0)
    cb0.record_failure()  # → OPEN → 立即 HALF_OPEN (recovery=0)

    tier0 = VendorTier(vendor=b0, circuit_breaker=cb0)
    # 设置已过期的 deadline
    tier0._rate_limit_deadline = time.monotonic() - 1

    router = RequestRouter([tier0, VendorTier(vendor=b1)])
    resp = await router.route_message(_body(), _headers())

    assert resp.status_code == 200
    assert b0.call_count == 1  # deadline 过期，允许探测
    assert b1.call_count == 0  # 探测成功，无需降级
