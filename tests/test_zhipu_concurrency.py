"""Zhipu 每模型并发限制专项测试.

验证 ``ModelConcurrencyLimiter`` 与 ``ZhipuVendor`` 集成后的并发控制行为：
  - 默认 ``concurrency.default=3`` 时同一模型最多 3 个并发
  - 超出上限时按 FIFO 排队，槽位释放后才唤醒
  - 不同模型彼此独立，互不阻塞
  - 异常路径下 Semaphore 仍能释放，避免泄漏
  - 流式请求与非流式请求共享同一信号量
  - 与 429 重试机制兼容（重试期间持续占用槽位）
  - ``concurrency=None`` 时禁用限制（向后兼容）
"""

from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from coding.proxy.config.schema import (
    ModelMappingRule,
    ZhipuConcurrencyConfig,
    ZhipuConfig,
)
from coding.proxy.routing.model_mapper import ModelMapper
from coding.proxy.vendors.concurrency import ModelConcurrencyLimiter
from coding.proxy.vendors.native_anthropic import NativeAnthropicVendor
from coding.proxy.vendors.zhipu import ZhipuVendor

# ─── 测试工具 ───────────────────────────────────────────────


def _make_mapper() -> ModelMapper:
    """构造标准三模型映射的 ModelMapper."""
    return ModelMapper(
        [
            ModelMappingRule(
                pattern="claude-sonnet-.*",
                target="glm-5v-turbo",
                is_regex=True,
                vendors=["zhipu"],
            ),
            ModelMappingRule(
                pattern="claude-opus-.*",
                target="glm-5.1",
                is_regex=True,
                vendors=["zhipu"],
            ),
            ModelMappingRule(
                pattern="claude-haiku-.*",
                target="glm-4.5-air",
                is_regex=True,
                vendors=["zhipu"],
            ),
        ]
    )


def _make_vendor(
    concurrency: ZhipuConcurrencyConfig | None = None,
    api_key: str = "test-zhipu-key",
) -> ZhipuVendor:
    """构造一个 ZhipuVendor，默认启用并发限制（default=3）."""
    cfg_kwargs: dict = {"api_key": api_key}
    if concurrency is not None:
        cfg_kwargs["concurrency"] = concurrency
    return ZhipuVendor(ZhipuConfig(**cfg_kwargs), _make_mapper())


def _make_200_response() -> httpx.Response:
    body = json.dumps(
        {
            "id": "msg_test",
            "type": "message",
            "role": "assistant",
            "content": [{"type": "text", "text": "ok"}],
            "model": "glm-5.1",
            "usage": {"input_tokens": 1, "output_tokens": 1},
        }
    ).encode()
    return httpx.Response(
        status_code=200,
        content=body,
        headers={"content-type": "application/json"},
        request=httpx.Request(
            "POST", "https://open.bigmodel.cn/api/anthropic/v1/messages"
        ),
    )


def _make_429_response() -> httpx.Response:
    return httpx.Response(
        status_code=429,
        content=b'{"error":{"type":"rate_limit_error","message":"slow down"}}',
        headers={},
        request=httpx.Request(
            "POST", "https://open.bigmodel.cn/api/anthropic/v1/messages"
        ),
    )


# ─── 配置层测试 ─────────────────────────────────────────────


class TestZhipuConcurrencyConfig:
    """ZhipuConcurrencyConfig 配置模型行为."""

    def test_defaults(self) -> None:
        cfg = ZhipuConcurrencyConfig()
        assert cfg.default == 3
        assert cfg.models == {}

    def test_get_limit_falls_back_to_default(self) -> None:
        cfg = ZhipuConcurrencyConfig(default=5)
        assert cfg.get_limit("glm-5.1") == 5
        assert cfg.get_limit("any-unknown-model") == 5

    def test_get_limit_uses_per_model_override(self) -> None:
        cfg = ZhipuConcurrencyConfig(default=3, models={"glm-5v-turbo": 1})
        assert cfg.get_limit("glm-5v-turbo") == 1
        assert cfg.get_limit("glm-5.1") == 3  # 未覆盖时回退 default

    def test_default_must_be_positive(self) -> None:
        with pytest.raises(ValueError):
            ZhipuConcurrencyConfig(default=0)

    def test_zhipu_config_default_concurrency(self) -> None:
        cfg = ZhipuConfig()
        assert cfg.concurrency is not None
        assert cfg.concurrency.default == 3


# ─── ModelConcurrencyLimiter 单元测试 ──────────────────────


class TestModelConcurrencyLimiter:
    """ModelConcurrencyLimiter 基础行为."""

    @pytest.mark.asyncio
    async def test_lazy_semaphore_creation(self) -> None:
        limiter = ModelConcurrencyLimiter(ZhipuConcurrencyConfig(default=2))
        sem_a = limiter._get_semaphore("model-a")
        sem_b = limiter._get_semaphore("model-b")
        # 不同模型独立 semaphore
        assert sem_a is not sem_b
        # 相同模型复用 semaphore
        assert limiter._get_semaphore("model-a") is sem_a

    @pytest.mark.asyncio
    async def test_acquire_blocks_when_full(self) -> None:
        limiter = ModelConcurrencyLimiter(ZhipuConcurrencyConfig(default=2))

        # 占满 2 个槽位
        sem1 = await limiter.acquire("glm-5.1")
        sem2 = await limiter.acquire("glm-5.1")
        assert sem1 is sem2  # 同一 semaphore

        # 第 3 次 acquire 必须阻塞
        task = asyncio.create_task(limiter.acquire("glm-5.1"))
        await asyncio.sleep(0.05)
        assert not task.done(), "第三个请求应在排队等待"

        # 释放一个槽位后，等待者被唤醒
        sem1.release()
        await asyncio.sleep(0.05)
        assert task.done()
        (await task).release()
        sem2.release()

    @pytest.mark.asyncio
    async def test_per_model_independent(self) -> None:
        limiter = ModelConcurrencyLimiter(
            ZhipuConcurrencyConfig(default=1, models={"glm-5.1": 1})
        )
        # 占满 glm-5.1
        sem_51 = await limiter.acquire("glm-5.1")
        # glm-5v-turbo 仍可立即获取
        sem_5v = await asyncio.wait_for(limiter.acquire("glm-5v-turbo"), timeout=0.5)
        assert sem_51 is not sem_5v
        sem_51.release()
        sem_5v.release()

    def test_diagnostics_snapshot(self) -> None:
        limiter = ModelConcurrencyLimiter(ZhipuConcurrencyConfig(default=3))
        # 触发 semaphore 创建
        limiter._get_semaphore("glm-5.1")
        snap = limiter.get_diagnostics()
        assert "glm-5.1" in snap
        assert snap["glm-5.1"]["limit"] == 3
        assert snap["glm-5.1"]["available"] == 3
        assert snap["glm-5.1"]["in_use"] == 0


# ─── ZhipuVendor 集成测试：非流式 ────────────────────────────


class TestZhipuVendorNonStreamConcurrency:
    """非流式 send_message 的并发限制行为."""

    @pytest.mark.asyncio
    async def test_limits_parallel_requests(self) -> None:
        """concurrency.default=2 时，3 个并发请求中只有 2 个同时执行."""
        vendor = _make_vendor(ZhipuConcurrencyConfig(default=2))
        active = 0
        peak = 0
        gate = asyncio.Event()

        async def mock_post(*_, **__) -> httpx.Response:
            nonlocal active, peak
            active += 1
            peak = max(peak, active)
            # 等待外部释放，保证并发观测窗口
            await gate.wait()
            active -= 1
            return _make_200_response()

        with patch.object(vendor, "_get_client") as mock_client:
            client = AsyncMock()
            client.post = mock_post
            mock_client.return_value = client

            tasks = [
                asyncio.create_task(
                    vendor.send_message(
                        {"model": "claude-opus-4-6", "messages": []},
                        {},
                    )
                )
                for _ in range(3)
            ]
            # 等待两个请求进入 active 状态
            for _ in range(40):
                if active >= 2:
                    break
                await asyncio.sleep(0.01)

            assert active == 2, "应有恰好 2 个请求在执行（第 3 个排队）"
            gate.set()
            results = await asyncio.gather(*tasks)
            assert all(r.status_code == 200 for r in results)
            assert peak == 2, "并发峰值不应超过 2"

    @pytest.mark.asyncio
    async def test_per_model_independent(self) -> None:
        """不同模型的槽位互不影响."""
        cfg = ZhipuConcurrencyConfig(
            default=3,
            models={"glm-5v-turbo": 1, "glm-5.1": 1},
        )
        vendor = _make_vendor(cfg)
        gate = asyncio.Event()
        seen_models: list[str] = []

        async def mock_post(*_args, **kwargs) -> httpx.Response:
            body = kwargs.get("json", {})
            seen_models.append(body.get("model", ""))
            await gate.wait()
            return _make_200_response()

        with patch.object(vendor, "_get_client") as mock_client:
            client = AsyncMock()
            client.post = mock_post
            mock_client.return_value = client

            # claude-opus → glm-5.1, claude-sonnet → glm-5v-turbo，
            # 分属两个独立信号量，应同时执行
            task_opus = asyncio.create_task(
                vendor.send_message(
                    {"model": "claude-opus-4-6", "messages": []},
                    {},
                )
            )
            task_sonnet = asyncio.create_task(
                vendor.send_message(
                    {"model": "claude-sonnet-4-6", "messages": []},
                    {},
                )
            )
            for _ in range(40):
                if len(seen_models) >= 2:
                    break
                await asyncio.sleep(0.01)

            assert len(seen_models) == 2, "两个不同模型应并发执行"
            assert set(seen_models) == {"glm-5.1", "glm-5v-turbo"}
            gate.set()
            await asyncio.gather(task_opus, task_sonnet)

    @pytest.mark.asyncio
    async def test_semaphore_released_on_exception(self) -> None:
        """上游抛异常时 Semaphore 仍应释放，后续请求不阻塞."""
        vendor = _make_vendor(ZhipuConcurrencyConfig(default=1))
        call_count = 0

        async def mock_post(*_, **__) -> httpx.Response:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise RuntimeError("upstream boom")
            return _make_200_response()

        with patch.object(vendor, "_get_client") as mock_client:
            client = AsyncMock()
            client.post = mock_post
            mock_client.return_value = client

            with pytest.raises(RuntimeError):
                await vendor.send_message(
                    {"model": "claude-opus-4-6", "messages": []},
                    {},
                )

            # 槽位应已释放，第二次请求可正常完成
            resp = await asyncio.wait_for(
                vendor.send_message(
                    {"model": "claude-opus-4-6", "messages": []},
                    {},
                ),
                timeout=1.0,
            )
            assert resp.status_code == 200

    @pytest.mark.asyncio
    async def test_429_retry_holds_slot(self) -> None:
        """429 重试期间持续占用槽位，重试结束后释放."""
        vendor = _make_vendor(ZhipuConcurrencyConfig(default=1))
        call_count = 0

        async def mock_post(*_, **__) -> httpx.Response:
            nonlocal call_count
            call_count += 1
            if call_count <= 2:
                return _make_429_response()
            return _make_200_response()

        with (
            patch.object(vendor, "_get_client") as mock_client,
            patch("asyncio.sleep", new_callable=AsyncMock),
        ):
            client = AsyncMock()
            client.post = mock_post
            mock_client.return_value = client

            resp = await vendor.send_message(
                {"model": "claude-opus-4-6", "messages": []},
                {},
            )
            assert resp.status_code == 200
            assert call_count == 3  # 两次 429 + 一次成功，且共用同一槽位

    @pytest.mark.asyncio
    async def test_no_concurrency_when_config_is_none(self) -> None:
        """concurrency=None 时禁用并发限制，行为与旧版完全一致."""
        # 强制构造一个 concurrency=None 的 ZhipuConfig（绕过默认工厂）
        cfg = ZhipuConfig(api_key="key")
        cfg = cfg.model_copy(update={"concurrency": None})
        vendor = ZhipuVendor(cfg, _make_mapper())
        assert vendor._concurrency_limiter is None

        gate = asyncio.Event()
        active = 0
        peak = 0

        async def mock_post(*_, **__) -> httpx.Response:
            nonlocal active, peak
            active += 1
            peak = max(peak, active)
            await gate.wait()
            active -= 1
            return _make_200_response()

        with patch.object(vendor, "_get_client") as mock_client:
            client = AsyncMock()
            client.post = mock_post
            mock_client.return_value = client

            tasks = [
                asyncio.create_task(
                    vendor.send_message(
                        {"model": "claude-opus-4-6", "messages": []},
                        {},
                    )
                )
                for _ in range(5)
            ]
            for _ in range(40):
                if active >= 5:
                    break
                await asyncio.sleep(0.01)

            assert peak == 5, "无并发限制时应全部并行"
            gate.set()
            await asyncio.gather(*tasks)


# ─── ZhipuVendor 集成测试：流式 ──────────────────────────────


class TestZhipuVendorStreamConcurrency:
    """流式 send_message_stream 的并发限制行为."""

    @pytest.mark.asyncio
    async def test_stream_limits_parallel_requests(self) -> None:
        """流式请求遵循并发限制，超出排队等待."""
        vendor = _make_vendor(ZhipuConcurrencyConfig(default=1))
        active = 0
        peak = 0
        gate = asyncio.Event()

        async def fake_stream(self, _body, _headers):  # noqa: ARG001
            nonlocal active, peak
            active += 1
            peak = max(peak, active)
            try:
                await gate.wait()
                yield b'data: {"type":"message_start"}\n\n'
            finally:
                active -= 1

        async def consume(model: str) -> int:
            chunks: list[bytes] = []
            async for chunk in vendor.send_message_stream(
                {"model": model, "messages": []}, {}
            ):
                chunks.append(chunk)
            return len(chunks)

        with patch.object(NativeAnthropicVendor, "send_message_stream", fake_stream):
            tasks = [asyncio.create_task(consume("claude-opus-4-6")) for _ in range(3)]
            for _ in range(40):
                if active >= 1:
                    break
                await asyncio.sleep(0.01)

            assert active == 1, "concurrency=1 时只允许 1 个流式请求并发"
            gate.set()
            results = await asyncio.gather(*tasks)
            assert all(c >= 1 for c in results)
            assert peak == 1

    @pytest.mark.asyncio
    async def test_stream_releases_slot_on_completion(self) -> None:
        """流式生成器正常耗尽后槽位释放."""
        vendor = _make_vendor(ZhipuConcurrencyConfig(default=1))

        async def fake_stream(self, _body, _headers):  # noqa: ARG001
            yield b'data: {"type":"message_start"}\n\n'
            yield b'data: {"type":"message_stop"}\n\n'

        with patch.object(NativeAnthropicVendor, "send_message_stream", fake_stream):
            # 连续两次流式请求都能完成（说明槽位被释放）
            for _ in range(2):
                chunks = []
                async for chunk in vendor.send_message_stream(
                    {"model": "claude-opus-4-6", "messages": []}, {}
                ):
                    chunks.append(chunk)
                assert len(chunks) == 2

        # 确认 semaphore 当前完全可用
        assert vendor._concurrency_limiter is not None
        sem = vendor._concurrency_limiter._get_semaphore("glm-5.1")
        assert sem._value == 1  # noqa: SLF001

    @pytest.mark.asyncio
    async def test_stream_releases_slot_on_error(self) -> None:
        """流式请求异常退出时槽位仍释放，后续请求不被阻塞."""
        vendor = _make_vendor(ZhipuConcurrencyConfig(default=1))
        call_count = 0

        async def fake_stream(self, _body, _headers):  # noqa: ARG001
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                resp = httpx.Response(
                    status_code=500,
                    content=b'{"error":{"type":"api_error"}}',
                    request=httpx.Request("POST", "https://example.com"),
                )
                raise httpx.HTTPStatusError("500", request=resp.request, response=resp)
                yield b""  # 让函数成为 async generator（不可达）
            yield b'data: {"type":"message_start"}\n\n'

        with patch.object(NativeAnthropicVendor, "send_message_stream", fake_stream):
            with pytest.raises(httpx.HTTPStatusError):
                async for _ in vendor.send_message_stream(
                    {"model": "claude-opus-4-6", "messages": []}, {}
                ):
                    pass

            # 槽位应已释放，第二次请求可正常推进
            chunks = []
            async for chunk in vendor.send_message_stream(
                {"model": "claude-opus-4-6", "messages": []}, {}
            ):
                chunks.append(chunk)
            assert chunks == [b'data: {"type":"message_start"}\n\n']

    @pytest.mark.asyncio
    async def test_stream_and_nonstream_share_semaphore(self) -> None:
        """流式与非流式请求共用同一信号量（按映射后模型分组）."""
        vendor = _make_vendor(ZhipuConcurrencyConfig(default=1))
        gate = asyncio.Event()
        active = 0

        async def fake_stream(self, _body, _headers):  # noqa: ARG001
            nonlocal active
            active += 1
            try:
                await gate.wait()
                yield b'data: {"type":"message_start"}\n\n'
            finally:
                active -= 1

        async def mock_post(*_, **__) -> httpx.Response:
            nonlocal active
            active += 1
            active -= 1
            return _make_200_response()

        with (
            patch.object(NativeAnthropicVendor, "send_message_stream", fake_stream),
            patch.object(vendor, "_get_client") as mock_client,
        ):
            client = AsyncMock()
            client.post = mock_post
            mock_client.return_value = client

            # 启动流式请求并等待它占用槽位
            async def consume_stream() -> None:
                async for _ in vendor.send_message_stream(
                    {"model": "claude-opus-4-6", "messages": []}, {}
                ):
                    pass

            stream_task = asyncio.create_task(consume_stream())
            for _ in range(40):
                if active >= 1:
                    break
                await asyncio.sleep(0.01)
            assert active == 1

            # 非流式请求应被同一信号量阻塞
            nonstream_task = asyncio.create_task(
                vendor.send_message(
                    {"model": "claude-opus-4-6", "messages": []},
                    {},
                )
            )
            await asyncio.sleep(0.05)
            assert not nonstream_task.done(), "非流式请求应等待流式释放槽位"

            # 释放后两者都能完成
            gate.set()
            await asyncio.gather(stream_task, nonstream_task)
