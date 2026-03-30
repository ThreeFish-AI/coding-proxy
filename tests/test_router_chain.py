"""N-tier 链式路由器单元测试."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, AsyncIterator
from unittest.mock import AsyncMock

import httpx
import pytest

from coding.proxy.backends.base import BackendResponse, BaseBackend, UsageInfo
from coding.proxy.routing.circuit_breaker import CircuitBreaker
from coding.proxy.routing.quota_guard import QuotaGuard
from coding.proxy.routing.router import RequestRouter
from coding.proxy.routing.tier import BackendTier


# --- 测试用 Mock 后端 ---


class FakeBackend(BaseBackend):
    """可配置行为的假后端."""

    def __init__(
        self,
        name: str = "fake",
        response: BackendResponse | None = None,
        stream_chunks: list[bytes] | None = None,
        raise_on_call: Exception | None = None,
    ) -> None:
        super().__init__("http://fake", 30000)
        self._name = name
        self._response = response or BackendResponse()
        self._stream_chunks = stream_chunks or []
        self._raise_on_call = raise_on_call
        self.call_count = 0

    def get_name(self) -> str:
        return self._name

    def _prepare_request(self, request_body, headers):
        return request_body, headers

    async def send_message(self, request_body, headers) -> BackendResponse:
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
    b0 = FakeBackend("primary", BackendResponse(status_code=200, usage=UsageInfo(input_tokens=10, output_tokens=5)))
    b1 = FakeBackend("fallback")
    router = RequestRouter([
        BackendTier(backend=b0, circuit_breaker=CircuitBreaker()),
        BackendTier(backend=b1),
    ])
    resp = await router.route_message(_body(), _headers())
    assert resp.status_code == 200
    assert b0.call_count == 1
    assert b1.call_count == 0


@pytest.mark.asyncio
async def test_route_message_failover_to_tier1():
    """首层 429 → 次层接管."""
    b0 = FakeBackend(
        "primary",
        BackendResponse(
            status_code=429,
            error_type="rate_limit_error",
            error_message="Rate limited",
        ),
    )
    # 为 primary 配置 failover
    from coding.proxy.config.schema import FailoverConfig
    b0._failover_config = FailoverConfig()

    b1 = FakeBackend("copilot", BackendResponse(status_code=200, usage=UsageInfo(input_tokens=20)))
    b2 = FakeBackend("zhipu")

    router = RequestRouter([
        BackendTier(backend=b0, circuit_breaker=CircuitBreaker()),
        BackendTier(backend=b1, circuit_breaker=CircuitBreaker()),
        BackendTier(backend=b2),
    ])
    resp = await router.route_message(_body(), _headers())
    assert resp.status_code == 200
    assert b0.call_count == 1
    assert b1.call_count == 1
    assert b2.call_count == 0


@pytest.mark.asyncio
async def test_route_message_failover_to_terminal():
    """前两层都失败 → 终端层接管."""
    from coding.proxy.config.schema import FailoverConfig

    b0 = FakeBackend("primary", BackendResponse(status_code=429, error_type="rate_limit_error", error_message="limit"))
    b0._failover_config = FailoverConfig()

    b1 = FakeBackend("copilot", BackendResponse(status_code=503, error_type="overloaded_error", error_message="overloaded"))
    b1._failover_config = FailoverConfig()

    b2 = FakeBackend("zhipu", BackendResponse(status_code=200, usage=UsageInfo(input_tokens=30)))

    router = RequestRouter([
        BackendTier(backend=b0, circuit_breaker=CircuitBreaker()),
        BackendTier(backend=b1, circuit_breaker=CircuitBreaker()),
        BackendTier(backend=b2),
    ])
    resp = await router.route_message(_body(), _headers())
    assert resp.status_code == 200
    assert b0.call_count == 1
    assert b1.call_count == 1
    assert b2.call_count == 1


@pytest.mark.asyncio
async def test_route_message_connection_error_failover():
    """连接异常 → 故障转移到下一层."""
    b0 = FakeBackend("primary", raise_on_call=httpx.ConnectError("connection refused"))
    b1 = FakeBackend("fallback", BackendResponse(status_code=200))

    router = RequestRouter([
        BackendTier(backend=b0, circuit_breaker=CircuitBreaker()),
        BackendTier(backend=b1),
    ])
    resp = await router.route_message(_body(), _headers())
    assert resp.status_code == 200
    assert b0.call_count == 1
    assert b1.call_count == 1


@pytest.mark.asyncio
async def test_route_message_last_tier_raises():
    """终端层也失败 → 抛出异常."""
    b0 = FakeBackend("primary", raise_on_call=httpx.ConnectError("refused"))
    b1 = FakeBackend("fallback", raise_on_call=httpx.ConnectError("also refused"))

    router = RequestRouter([
        BackendTier(backend=b0, circuit_breaker=CircuitBreaker()),
        BackendTier(backend=b1),
    ])
    with pytest.raises(httpx.ConnectError):
        await router.route_message(_body(), _headers())


@pytest.mark.asyncio
async def test_circuit_open_skips_tier():
    """CB OPEN 的层被跳过."""
    b0 = FakeBackend("primary", BackendResponse(status_code=200))
    b1 = FakeBackend("fallback", BackendResponse(status_code=200))

    cb0 = CircuitBreaker(failure_threshold=1)
    cb0.record_failure()  # OPEN

    router = RequestRouter([
        BackendTier(backend=b0, circuit_breaker=cb0),
        BackendTier(backend=b1),
    ])
    resp = await router.route_message(_body(), _headers())
    assert resp.status_code == 200
    assert b0.call_count == 0  # 被跳过
    assert b1.call_count == 1


@pytest.mark.asyncio
async def test_quota_exceeded_skips_tier():
    """QG EXCEEDED 的层被跳过."""
    b0 = FakeBackend("primary", BackendResponse(status_code=200))
    b1 = FakeBackend("fallback", BackendResponse(status_code=200))

    qg = QuotaGuard(enabled=True, token_budget=100, window_seconds=3600, probe_interval_seconds=99999)
    qg.notify_cap_error()

    router = RequestRouter([
        BackendTier(backend=b0, circuit_breaker=CircuitBreaker(), quota_guard=qg),
        BackendTier(backend=b1),
    ])
    resp = await router.route_message(_body(), _headers())
    assert resp.status_code == 200
    assert b0.call_count == 0
    assert b1.call_count == 1


@pytest.mark.asyncio
async def test_all_non_terminal_skipped_reaches_terminal():
    """所有非终端层不可用 → 直达终端."""
    b0 = FakeBackend("primary")
    b1 = FakeBackend("copilot")
    b2 = FakeBackend("zhipu", BackendResponse(status_code=200))

    cb0 = CircuitBreaker(failure_threshold=1)
    cb0.record_failure()

    cb1 = CircuitBreaker(failure_threshold=1)
    cb1.record_failure()

    router = RequestRouter([
        BackendTier(backend=b0, circuit_breaker=cb0),
        BackendTier(backend=b1, circuit_breaker=cb1),
        BackendTier(backend=b2),
    ])
    resp = await router.route_message(_body(), _headers())
    assert resp.status_code == 200
    assert b0.call_count == 0
    assert b1.call_count == 0
    assert b2.call_count == 1


@pytest.mark.asyncio
async def test_last_tier_always_tried_even_if_unavailable():
    """最后一层即使不可用也会尝试（终端保障）."""
    b0 = FakeBackend("primary", raise_on_call=httpx.ConnectError("refused"))
    # 终端层无 CB/QG，始终被执行
    b1 = FakeBackend("fallback", BackendResponse(status_code=200))

    router = RequestRouter([
        BackendTier(backend=b0, circuit_breaker=CircuitBreaker()),
        BackendTier(backend=b1),
    ])
    resp = await router.route_message(_body(), _headers())
    assert b1.call_count == 1


# --- route_stream 测试 ---


@pytest.mark.asyncio
async def test_route_stream_primary_success():
    """流式：首层成功."""
    chunks = [b"data: {}\n\n", b"data: [DONE]\n\n"]
    b0 = FakeBackend("primary", stream_chunks=chunks)
    b1 = FakeBackend("fallback")

    router = RequestRouter([
        BackendTier(backend=b0, circuit_breaker=CircuitBreaker()),
        BackendTier(backend=b1),
    ])

    collected = []
    async for chunk, name in router.route_stream(_body(), _headers()):
        collected.append((chunk, name))

    assert len(collected) == 2
    assert collected[0][1] == "primary"
    assert b1.call_count == 0


@pytest.mark.asyncio
async def test_route_stream_failover():
    """流式：首层异常 → 次层接管."""
    b0 = FakeBackend("primary", raise_on_call=httpx.ConnectError("refused"))
    b1 = FakeBackend("fallback", stream_chunks=[b"data: ok\n\n"])

    router = RequestRouter([
        BackendTier(backend=b0, circuit_breaker=CircuitBreaker()),
        BackendTier(backend=b1),
    ])

    collected = []
    async for chunk, name in router.route_stream(_body(), _headers()):
        collected.append((chunk, name))

    assert len(collected) == 1
    assert collected[0][1] == "fallback"


@pytest.mark.asyncio
async def test_route_stream_all_fail_raises():
    """流式：所有层失败 → 抛出异常."""
    b0 = FakeBackend("primary", raise_on_call=httpx.ConnectError("refused"))
    b1 = FakeBackend("fallback", raise_on_call=httpx.ConnectError("also refused"))

    router = RequestRouter([
        BackendTier(backend=b0, circuit_breaker=CircuitBreaker()),
        BackendTier(backend=b1),
    ])

    with pytest.raises(httpx.ConnectError):
        async for _ in router.route_stream(_body(), _headers()):
            pass


# --- 构造与关闭 ---


def test_router_requires_at_least_one_tier():
    with pytest.raises(ValueError, match="至少需要一个后端层级"):
        RequestRouter([])


@pytest.mark.asyncio
async def test_router_close_calls_all_backends():
    b0 = FakeBackend("a")
    b1 = FakeBackend("b")
    b0.close = AsyncMock()
    b1.close = AsyncMock()

    router = RequestRouter([
        BackendTier(backend=b0),
        BackendTier(backend=b1),
    ])
    await router.close()
    b0.close.assert_awaited_once()
    b1.close.assert_awaited_once()


def test_router_tiers_property():
    tiers = [BackendTier(backend=FakeBackend("a")), BackendTier(backend=FakeBackend("b"))]
    router = RequestRouter(tiers)
    assert router.tiers is tiers
