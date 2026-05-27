"""ModelConcurrencyController monitor 模式专项测试.

验证 ``config=None`` 时的纯计数行为：
  - acquire 不阻塞，无 limit / available
  - pending 永远为 0
  - set_limit 抛 ValueError
  - 100 并发 in_use 峰值正确
  - get_diagnostics 输出 mode="monitor" + limit/available=None
"""

from __future__ import annotations

import asyncio

import pytest

from coding.proxy.vendors.concurrency import ModelConcurrencyController


class TestMonitorMode:
    """monitor 模式（config=None）基础行为."""

    def test_mode_property(self) -> None:
        ctrl = ModelConcurrencyController(None)
        assert ctrl.mode == "monitor"

    @pytest.mark.asyncio
    async def test_acquire_never_blocks(self) -> None:
        """monitor 模式下任意数量并发都立即获取槽位."""
        ctrl = ModelConcurrencyController(None)
        slot = ctrl._get_or_create_slot("model-x")
        # 即使触发多次 acquire 也不阻塞
        for _ in range(10):
            await slot.acquire()
        assert slot.in_use == 10
        assert slot.pending == 0

    @pytest.mark.asyncio
    async def test_100_concurrent_acquires_no_queue(self) -> None:
        """100 并发请求全部立即拿到槽位，无排队."""
        ctrl = ModelConcurrencyController(None)

        gate = asyncio.Event()
        max_in_use = 0

        async def hold(model: str) -> None:
            nonlocal max_in_use
            async with ctrl.track(model):
                slot = ctrl._get_or_create_slot(model)
                max_in_use = max(max_in_use, slot.in_use)
                await gate.wait()

        tasks = [asyncio.create_task(hold("test-model")) for _ in range(100)]
        # 等所有任务进入 track
        await asyncio.sleep(0.1)
        slot = ctrl._get_or_create_slot("test-model")
        assert slot.in_use == 100, "monitor 模式应允许全部并行"
        assert slot.pending == 0, "monitor 模式 pending 应恒为 0"
        gate.set()
        await asyncio.gather(*tasks)
        # 释放后归零
        assert slot.in_use == 0

    @pytest.mark.asyncio
    async def test_release_after_track(self) -> None:
        """track 上下文退出后 in_use 归零."""
        ctrl = ModelConcurrencyController(None)
        slot = ctrl._get_or_create_slot("m")
        async with ctrl.track("m"):
            assert slot.in_use == 1
        assert slot.in_use == 0

    def test_set_limit_raises_in_monitor(self) -> None:
        """monitor 模式下 set_limit 抛 ValueError."""
        ctrl = ModelConcurrencyController(None)
        with pytest.raises(ValueError, match="monitor-only"):
            ctrl.set_limit("m", 5)

    def test_diagnostics_monitor_shape(self) -> None:
        """monitor 模式 get_diagnostics 输出 mode + limit/available=None."""
        ctrl = ModelConcurrencyController(None)
        ctrl._get_or_create_slot("m")
        snap = ctrl.get_diagnostics()
        assert snap["m"]["mode"] == "monitor"
        assert snap["m"]["limit"] is None
        assert snap["m"]["available"] is None
        assert snap["m"]["in_use"] == 0
        assert snap["m"]["pending"] == 0
        assert snap["m"]["peak_pending_recent"] == 0

    @pytest.mark.asyncio
    async def test_pending_never_increases_in_monitor(self) -> None:
        """monitor 模式即使触发大量并发，pending 永远不增."""
        ctrl = ModelConcurrencyController(None)
        gate = asyncio.Event()

        async def hold() -> None:
            async with ctrl.track("m"):
                await gate.wait()

        tasks = [asyncio.create_task(hold()) for _ in range(20)]
        await asyncio.sleep(0.05)
        slot = ctrl._get_or_create_slot("m")
        assert slot.pending == 0
        assert slot.in_use == 20
        gate.set()
        await asyncio.gather(*tasks)


class TestBaseVendorTrackInFlight:
    """BaseVendor.track_in_flight 默认 monitor 行为."""

    def test_empty_model_returns_noop(self) -> None:
        """空 model name 返回 no-op 上下文，不影响 controller 状态."""
        from coding.proxy.config.schema import AnthropicConfig, FailoverConfig
        from coding.proxy.vendors.anthropic import AnthropicVendor

        vendor = AnthropicVendor(AnthropicConfig(), FailoverConfig())
        ctx = vendor.track_in_flight("")
        # nullcontext 是同步上下文管理器，应有 __enter__/__exit__
        # 我们不进入它，只验证不抛错
        assert ctx is not None

    @pytest.mark.asyncio
    async def test_track_in_flight_increments_in_use(self) -> None:
        """非空 model name → controller.track 进入，in_use 自增."""
        from coding.proxy.config.schema import AnthropicConfig, FailoverConfig
        from coding.proxy.vendors.anthropic import AnthropicVendor

        vendor = AnthropicVendor(AnthropicConfig(), FailoverConfig())
        async with vendor.track_in_flight("claude-test"):
            slot = vendor._concurrency_controller._get_or_create_slot("claude-test")
            assert slot.in_use == 1
        # 退出后归零
        slot = vendor._concurrency_controller._get_or_create_slot("claude-test")
        assert slot.in_use == 0

    def test_update_concurrency_default_is_monitor_only(self) -> None:
        """BaseVendor 默认 monitor → update_concurrency 抛 ValueError."""
        from coding.proxy.config.schema import AnthropicConfig, FailoverConfig
        from coding.proxy.vendors.anthropic import AnthropicVendor

        vendor = AnthropicVendor(AnthropicConfig(), FailoverConfig())
        with pytest.raises(ValueError, match="monitor-only"):
            vendor.update_concurrency("m", 5)

    def test_get_diagnostics_includes_concurrency_after_use(self) -> None:
        """track_in_flight 用过后 get_diagnostics 输出 concurrency 字段."""
        from coding.proxy.config.schema import AnthropicConfig, FailoverConfig
        from coding.proxy.vendors.anthropic import AnthropicVendor

        vendor = AnthropicVendor(AnthropicConfig(), FailoverConfig())
        # 触发 slot 创建
        vendor._concurrency_controller._get_or_create_slot("claude-test")
        diag = vendor.get_diagnostics()
        assert "concurrency" in diag
        assert "claude-test" in diag["concurrency"]
        assert diag["concurrency"]["claude-test"]["mode"] == "monitor"
