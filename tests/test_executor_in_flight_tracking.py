"""Executor 层 track_in_flight 包裹行为验证.

验证 ``_RouteExecutor`` 在调用 ``vendor.send_message[_stream]`` 前
进入 ``vendor.track_in_flight(mapped_model)`` 上下文，在调用结束（包括异常）
后正确退出（释放槽位）。
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from coding.proxy.compat.canonical import (
    CompatibilityDecision,
    CompatibilityStatus,
)
from coding.proxy.routing.executor import _RouteExecutor
from coding.proxy.routing.session_manager import RouteSessionManager
from coding.proxy.routing.tier import VendorTier
from coding.proxy.routing.usage_recorder import UsageRecorder
from coding.proxy.vendors.base import (
    BaseVendor,
    RequestCapabilities,
    UsageInfo,
    VendorCapabilities,
    VendorResponse,
)


class _TrackingProbe:
    """共享状态：记录 track_in_flight enter/exit 时序与 send 调用顺序."""

    def __init__(self) -> None:
        self.events: list[str] = []
        self.in_flight: int = 0
        self.peak_in_flight: int = 0

    def track_factory(self, vendor_name: str):
        @asynccontextmanager
        async def _track(mapped_model: str):
            self.events.append(f"enter:{vendor_name}:{mapped_model}")
            self.in_flight += 1
            self.peak_in_flight = max(self.peak_in_flight, self.in_flight)
            try:
                yield
            finally:
                self.in_flight -= 1
                self.events.append(f"exit:{vendor_name}:{mapped_model}")

        def _factory(mapped_model: str):
            return _track(mapped_model)

        return _factory


def _mock_vendor_with_probe(
    probe: _TrackingProbe, name: str = "test", **caps_kwargs
) -> BaseVendor:
    """创建带 track_in_flight 探针的 mock vendor."""
    vendor = MagicMock(spec=BaseVendor)
    vendor.get_name.return_value = name
    vendor.map_model.return_value = f"{name}-mapped"
    caps = VendorCapabilities(**caps_kwargs)
    vendor.get_capabilities.return_value = caps
    vendor.get_compatibility_profile.return_value = MagicMock()
    vendor.make_compatibility_decision.return_value = CompatibilityDecision(
        status=CompatibilityStatus.NATIVE,
    )
    vendor.get_compat_trace.return_value = None

    def _supports_request(_caps: RequestCapabilities):
        return True, []

    vendor.supports_request.side_effect = _supports_request
    vendor.check_health = AsyncMock(return_value=True)
    vendor.close = AsyncMock()
    vendor.set_compat_context = MagicMock()

    # 关键：track_in_flight 委托给 probe
    vendor.track_in_flight = MagicMock(side_effect=probe.track_factory(name))

    # send_message 默认返回成功
    async def _send_message(_body, _headers):
        probe.events.append(f"send:{name}")
        return VendorResponse(
            status_code=200,
            raw_body=b"{}",
            usage=UsageInfo(input_tokens=1, output_tokens=1),
        )

    vendor.send_message = AsyncMock(side_effect=_send_message)

    # send_message_stream 默认产出空流
    async def _send_stream(_body, _headers):
        probe.events.append(f"stream_start:{name}")
        yield b'data: {"type":"message_start"}\n\n'
        probe.events.append(f"stream_end:{name}")

    vendor.send_message_stream = MagicMock(side_effect=_send_stream)
    return vendor


def _make_executor(vendor: BaseVendor) -> _RouteExecutor:
    tier = VendorTier(vendor=vendor)
    return _RouteExecutor(
        router=MagicMock(),
        tiers=[tier],
        usage_recorder=UsageRecorder(),
        session_manager=RouteSessionManager(),
    )


class TestExecuteMessageInFlightTracking:
    """非流式调用的 track_in_flight 包裹行为."""

    @pytest.mark.asyncio
    async def test_track_enter_before_send_exit_after(self):
        """success path: enter → send → exit 顺序."""
        probe = _TrackingProbe()
        vendor = _mock_vendor_with_probe(probe, name="kimi")
        exec_inst = _make_executor(vendor)

        resp = await exec_inst.execute_message({"model": "claude-test"}, {})
        assert resp.status_code == 200

        assert probe.events == [
            "enter:kimi:kimi-mapped",
            "send:kimi",
            "exit:kimi:kimi-mapped",
        ]
        vendor.track_in_flight.assert_called_once_with("kimi-mapped")

    @pytest.mark.asyncio
    async def test_track_exits_on_http_error(self):
        """异常路径：track_in_flight 仍执行 exit."""
        probe = _TrackingProbe()
        vendor = _mock_vendor_with_probe(probe, name="kimi")
        vendor.send_message = AsyncMock(side_effect=httpx.ConnectError("down"))
        exec_inst = _make_executor(vendor)

        with pytest.raises(httpx.ConnectError):
            await exec_inst.execute_message({"model": "claude-test"}, {})

        # enter 与 exit 都应被记录（finally 触发）
        assert "enter:kimi:kimi-mapped" in probe.events
        assert "exit:kimi:kimi-mapped" in probe.events
        # exit 应在 enter 之后
        assert probe.events.index("exit:kimi:kimi-mapped") > probe.events.index(
            "enter:kimi:kimi-mapped"
        )

    @pytest.mark.asyncio
    async def test_concurrent_message_calls_track_each(self):
        """多并发请求每个都触发 enter/exit；in_flight 峰值正确."""
        import asyncio

        probe = _TrackingProbe()
        vendor = _mock_vendor_with_probe(probe, name="copilot")

        async def slow_send(_body, _headers):
            probe.events.append("send:copilot")
            await asyncio.sleep(0.05)
            return VendorResponse(
                status_code=200,
                raw_body=b"{}",
                usage=UsageInfo(input_tokens=1, output_tokens=1),
            )

        vendor.send_message = AsyncMock(side_effect=slow_send)
        exec_inst = _make_executor(vendor)

        tasks = [
            asyncio.create_task(exec_inst.execute_message({"model": "claude-test"}, {}))
            for _ in range(5)
        ]
        results = await asyncio.gather(*tasks)
        assert all(r.status_code == 200 for r in results)
        # 5 个 enter + 5 个 send + 5 个 exit
        assert sum(1 for e in probe.events if e.startswith("enter:")) == 5
        assert sum(1 for e in probe.events if e.startswith("exit:")) == 5
        # 并发期间 in_flight 峰值应达到 5（monitor 模式不限流）
        assert probe.peak_in_flight == 5


class TestExecuteStreamInFlightTracking:
    """流式调用的 track_in_flight 包裹行为."""

    @pytest.mark.asyncio
    async def test_stream_track_enter_exit(self):
        """成功流式：enter → stream → exit 顺序."""
        probe = _TrackingProbe()
        vendor = _mock_vendor_with_probe(probe, name="doubao")
        exec_inst = _make_executor(vendor)

        chunks = []
        async for chunk, name in exec_inst.execute_stream({"model": "claude-test"}, {}):
            chunks.append((chunk, name))

        assert len(chunks) >= 1
        # 应包含 enter:doubao:doubao-mapped 与 exit:doubao:doubao-mapped
        assert "enter:doubao:doubao-mapped" in probe.events
        assert "exit:doubao:doubao-mapped" in probe.events
        # exit 在 stream_end 之后
        assert probe.events.index("exit:doubao:doubao-mapped") > probe.events.index(
            "stream_end:doubao"
        )

    @pytest.mark.asyncio
    async def test_stream_track_exits_on_error(self):
        """流式异常退出时 track exit 仍触发."""
        probe = _TrackingProbe()
        vendor = _mock_vendor_with_probe(probe, name="minimax")

        async def error_stream(_body, _headers):
            yield b'data: {"type":"message_start"}\n\n'
            raise httpx.HTTPStatusError(
                "500",
                request=httpx.Request("POST", "https://example.com"),
                response=httpx.Response(500),
            )

        vendor.send_message_stream = MagicMock(side_effect=error_stream)
        exec_inst = _make_executor(vendor)

        with pytest.raises(httpx.HTTPStatusError):
            async for _ in exec_inst.execute_stream({"model": "claude-test"}, {}):
                pass

        assert "enter:minimax:minimax-mapped" in probe.events
        assert "exit:minimax:minimax-mapped" in probe.events
