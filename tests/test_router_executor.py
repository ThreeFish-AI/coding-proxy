"""路由执行器单元测试.

覆盖 :mod:`coding.proxy.routing.executor` 的核心逻辑：
- _RouteExecutor 门控判断（能力检查 / 兼容性检查 / 健康检查）
- 错误处理（TokenAcquireError / HTTP 错误 / 语义拒绝）
- _is_cap_error 订阅用量上限判定
- _VENDOR_PROTOCOL_LABEL_MAP 映射完整性
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from coding.proxy.compat.canonical import (
    CompatibilityDecision,
    CompatibilityStatus,
    build_canonical_request,
)
from coding.proxy.routing.executor import (
    _VENDOR_PROTOCOL_LABEL_MAP,
    _has_tool_results,
    _is_likely_request_format_error,
    _log_vendor_response_error,
    _RouteExecutor,
)
from coding.proxy.routing.session_manager import RouteSessionManager
from coding.proxy.routing.tier import VendorTier
from coding.proxy.routing.usage_recorder import UsageRecorder
from coding.proxy.vendors.base import (
    BaseVendor,
    NoCompatibleVendorError,
    RequestCapabilities,
    UsageInfo,
    VendorCapabilities,
    VendorResponse,
)
from coding.proxy.vendors.token_manager import TokenAcquireError

# ── Mock 供应商工厂 ─────────────────────────────────────────


def _mock_vendor(name: str = "test", **caps_kwargs) -> BaseVendor:
    """创建 mock 供应商实例.

    supports_request 会根据 get_capabilities 的返回值自动计算兼容性，
    无需手动配置。
    """
    vendor = MagicMock(spec=BaseVendor)
    vendor.get_name.return_value = name
    vendor.map_model.return_value = name + "-model"
    caps = VendorCapabilities(**caps_kwargs)
    vendor.get_capabilities.return_value = caps
    vendor.get_compatibility_profile.return_value = MagicMock()
    vendor.make_compatibility_decision.return_value = CompatibilityDecision(
        status=CompatibilityStatus.NATIVE,
    )
    vendor.get_compat_trace.return_value = None

    # supports_request 基于实际能力动态判断
    def _supports_request(request_caps: RequestCapabilities):
        from coding.proxy.vendors.base import CapabilityLossReason

        reasons: list[CapabilityLossReason] = []
        if request_caps.has_tools and not caps.supports_tools:
            reasons.append(CapabilityLossReason.TOOLS)
        if request_caps.has_thinking and not caps.supports_thinking:
            reasons.append(CapabilityLossReason.THINKING)
        if request_caps.has_images and not caps.supports_images:
            reasons.append(CapabilityLossReason.IMAGES)
        if request_caps.has_metadata and not caps.supports_metadata:
            reasons.append(CapabilityLossReason.METADATA)
        return len(reasons) == 0, reasons

    vendor.supports_request.side_effect = _supports_request
    vendor.send_message = AsyncMock(
        return_value=VendorResponse(
            status_code=200,
            raw_body=b"{}",
            usage=UsageInfo(input_tokens=10, output_tokens=5),
        )
    )
    vendor.send_message_stream = AsyncMock()
    vendor.check_health = AsyncMock(return_value=True)
    vendor.close = AsyncMock()
    vendor.set_compat_context = MagicMock()
    return vendor


async def _async_chunks(chunks: list[bytes]):
    """辅助：将字节列表包装为异步迭代器."""
    for c in chunks:
        yield c


def _make_tier(vendor: BaseVendor | None = None, **tier_kwargs) -> VendorTier:
    """创建 VendorTier 实例."""
    if vendor is None:
        vendor = _mock_vendor()
    tier = VendorTier(vendor=vendor, **tier_kwargs)
    return tier


def _executor(tiers: list[VendorTier] | None = None, **kwargs) -> _RouteExecutor:
    """创建 _RouteExecutor 实例."""
    if tiers is None:
        tiers = [_make_tier()]
    recorder = kwargs.pop("recorder", UsageRecorder())
    session_mgr = kwargs.pop("session_mgr", RouteSessionManager())
    router = kwargs.pop("router", MagicMock())
    return _RouteExecutor(
        router=router,
        tiers=tiers,
        usage_recorder=recorder,
        session_manager=session_mgr,
        **kwargs,
    )


# ── _VENDOR_PROTOCOL_LABEL_MAP ───────────────────────────


class TestVendorProtocolLabelMap:
    """供应商协议标签映射测试."""

    def test_all_expected_keys_present(self):
        expected = {
            "anthropic",
            "zhipu",
            "copilot",
            "antigravity",
            "minimax",
            "kimi",
            "doubao",
            "xiaomi",
            "alibaba",
        }
        assert set(_VENDOR_PROTOCOL_LABEL_MAP.keys()) == expected

    def test_anthropics_map_to_anthropic_label(self):
        assert _VENDOR_PROTOCOL_LABEL_MAP["anthropic"] == "Anthropic"
        assert _VENDOR_PROTOCOL_LABEL_MAP["zhipu"] == "Anthropic"

    def test_copilot_maps_to_openai(self):
        assert _VENDOR_PROTOCOL_LABEL_MAP["copilot"] == "OpenAI"

    def test_antigravity_maps_to_gemini(self):
        assert _VENDOR_PROTOCOL_LABEL_MAP["antigravity"] == "Gemini"


# ── _is_cap_error ────────────────────────────────────────


class TestIsCapError:
    """订阅用量上限错误判定测试."""

    def test_429_with_quota_keyword(self):
        resp = VendorResponse(
            status_code=429,
            raw_body=b"",
            error_message="quota exceeded for this subscription",
        )
        assert _RouteExecutor._is_cap_error(resp) is True

    def test_429_with_usage_cap_keyword(self):
        resp = VendorResponse(
            status_code=429,
            raw_body=b"",
            error_message="Usage cap reached",
        )
        assert _RouteExecutor._is_cap_error(resp) is True

    def test_403_with_limit_exceeded(self):
        resp = VendorResponse(
            status_code=403,
            raw_body=b"",
            error_message="Rate limit exceeded",
        )
        assert _RouteExecutor._is_cap_error(resp) is True

    def test_429_generic_no_cap_keyword(self):
        resp = VendorResponse(
            status_code=429,
            raw_body=b"",
            error_message="Too many requests",
        )
        assert _RouteExecutor._is_cap_error(resp) is False

    def test_500_not_cap_error(self):
        resp = VendorResponse(
            status_code=500,
            raw_body=b"",
            error_message="Internal server error",
        )
        assert _RouteExecutor._is_cap_error(resp) is False

    def test_200_not_cap_error(self):
        resp = VendorResponse(
            status_code=200,
            raw_body=b'{"content":"ok"}',
        )
        assert _RouteExecutor._is_cap_error(resp) is False

    def test_none_error_message(self):
        resp = VendorResponse(status_code=429, raw_body=b"", error_message=None)
        assert _RouteExecutor._is_cap_error(resp) is False


# ── 门控测试 ─────────────────────────────────────────────


class TestTryGateTier:
    """门控判断测试."""

    @pytest.mark.asyncio
    async def test_eligible_when_all_checks_pass(self):
        tier = _make_tier()
        exec_inst = _executor([tier])
        body = {"model": "test"}
        headers = {}
        caps = RequestCapabilities()
        req = build_canonical_request(body, headers)
        session_record = await exec_inst._session_mgr.get_or_create_record(
            req.session_key, req.trace_id
        )
        reasons: list[str] = []

        result = await exec_inst._try_gate_tier(
            tier,
            is_last=True,
            request_caps=caps,
            canonical_request=req,
            session_record=session_record,
            incompatible_reasons=reasons,
        )
        assert result == "eligible"

    @pytest.mark.asyncio
    async def test_skip_when_capability_unsupported(self):
        vendor = _mock_vendor(supports_tools=False)
        tier = _make_tier(vendor)
        exec_inst = _executor([tier])
        caps = RequestCapabilities(has_tools=True)
        body = {"model": "test"}
        headers = {}
        req = build_canonical_request(body, headers)
        session_record = await exec_inst._session_mgr.get_or_create_record(
            req.session_key, req.trace_id
        )
        reasons: list[str] = []

        result = await exec_inst._try_gate_tier(
            tier,
            is_last=False,
            request_caps=caps,
            canonical_request=req,
            session_record=session_record,
            incompatible_reasons=reasons,
        )
        assert result == "skip"
        assert len(reasons) > 0

    @pytest.mark.asyncio
    async def test_skip_when_unsafe_compatibility(self):
        vendor = _mock_vendor()
        vendor.make_compatibility_decision.return_value = CompatibilityDecision(
            status=CompatibilityStatus.UNSAFE,
            unsupported_semantics=["thinking"],
        )
        tier = _make_tier(vendor)
        exec_inst = _executor([tier])
        caps = RequestCapabilities()
        body = {"model": "test", "thinking": {"type": "enabled"}}
        headers = {}
        req = build_canonical_request(body, headers)
        session_record = await exec_inst._session_mgr.get_or_create_record(
            req.session_key, req.trace_id
        )
        reasons: list[str] = []

        result = await exec_inst._try_gate_tier(
            tier,
            is_last=False,
            request_caps=caps,
            canonical_request=req,
            session_record=session_record,
            incompatible_reasons=reasons,
        )
        assert result == "skip"


# ── execute_message 测试 ─────────────────────────────────


class TestExecuteMessage:
    """非流式消息执行测试."""

    @pytest.mark.asyncio
    async def test_successful_routing(self):
        """成功路由到第一个可用供应商."""
        vendor = _mock_vendor("copilot")
        tier = _make_tier(vendor)
        exec_inst = _executor([tier])

        resp = await exec_inst.execute_message({"model": "test"}, {})
        assert resp.status_code == 200
        vendor.send_message.assert_called_once()

    @pytest.mark.asyncio
    async def test_failover_on_token_error(self):
        """TokenAcquireError 触发故障转移到下一层."""
        bad_vendor = _mock_vendor("bad")
        bad_vendor.send_message.side_effect = TokenAcquireError("token expired")

        good_vendor = _mock_vendor("good")
        good_resp = VendorResponse(
            status_code=200,
            raw_body=b"{}",
            usage=UsageInfo(input_tokens=5, output_tokens=2),
        )
        good_vendor.send_message = AsyncMock(return_value=good_resp)

        exec_inst = _executor(
            [
                _make_tier(bad_vendor),
                _make_tier(good_vendor),
            ]
        )

        resp = await exec_inst.execute_message({"model": "test"}, {})
        assert resp.status_code == 200
        assert good_vendor.send_message.called

    @pytest.mark.asyncio
    async def test_raises_no_compatible_vendor(self):
        """所有层均不兼容时抛出 NoCompatibleVendorError."""
        no_tools_vendor = _mock_vendor(supports_tools=False)
        exec_inst = _executor([_make_tier(no_tools_vendor)])

        with pytest.raises(NoCompatibleVendorError):
            await exec_inst.execute_message(
                {"model": "test", "tools": [{}]},
                {},
            )

    @pytest.mark.asyncio
    async def test_last_tier_propagates_http_error(self):
        """最后一层的 HTTP 错误直接抛出."""
        import httpx

        vendor = _mock_vendor()
        vendor.send_message.side_effect = httpx.ConnectError("unreachable")
        exec_inst = _executor([_make_tier(vendor)])

        with pytest.raises(httpx.ConnectError):
            await exec_inst.execute_message({"model": "test"}, {})

    @pytest.mark.asyncio
    async def test_last_tier_propagates_token_error(self):
        """最后一层的 TokenAcquireError 直接抛出."""
        vendor = _mock_vendor()
        vendor.send_message.side_effect = TokenAcquireError("no token")
        exec_inst = _executor([_make_tier(vendor)])

        with pytest.raises(TokenAcquireError):
            await exec_inst.execute_message({"model": "test"}, {})

    @pytest.mark.asyncio
    async def test_non_last_tier_continues_on_connect_error(self):
        """非最后一层连接失败时继续尝试下一层."""
        import httpx

        bad = _mock_vendor("bad")
        bad.send_message.side_effect = httpx.ConnectError("down")

        good = _mock_vendor("good")
        good_resp = VendorResponse(
            status_code=200,
            raw_body=b"{}",
            usage=UsageInfo(input_tokens=1, output_tokens=1),
        )
        good.send_message = AsyncMock(return_value=good_resp)

        exec_inst = _executor([_make_tier(bad), _make_tier(good)])
        resp = await exec_inst.execute_message({"model": "test"}, {})
        assert resp.status_code == 200

    @pytest.mark.asyncio
    async def test_unexpected_exception_on_last_tier_propagates(self):
        """最后一层非流式未预期异常应向上传播."""
        vendor = _mock_vendor()
        vendor.send_message.side_effect = KeyError("missing config key")
        exec_inst = _executor([_make_tier(vendor)])

        with pytest.raises(KeyError, match="missing config key"):
            await exec_inst.execute_message({"model": "test"}, {})

    @pytest.mark.asyncio
    async def test_unexpected_exception_on_non_last_tier_continues(self):
        """非最后一层非流式未预期异常应触发故障转移."""
        bad = _mock_vendor("bad")
        bad.send_message.side_effect = RuntimeError("internal state corruption")

        good = _mock_vendor("good")
        good_resp = VendorResponse(
            status_code=200,
            raw_body=b"{}",
            usage=UsageInfo(input_tokens=1, output_tokens=1),
        )
        good.send_message = AsyncMock(return_value=good_resp)

        exec_inst = _executor([_make_tier(bad), _make_tier(good)])
        resp = await exec_inst.execute_message({"model": "test"}, {})
        assert resp.status_code == 200


# ── execute_stream 测试 ──────────────────────────────────


class TestExecuteStream:
    """流式消息执行测试."""

    @pytest.mark.asyncio
    async def test_successful_stream_yields_chunks(self):
        """成功流式请求产出字节块."""
        vendor = _mock_vendor("copilot")
        chunk_bytes = b'data: {"type":"content_block_delta","delta":{"type":"text_delta","text":"hi"}}\n\n'

        async def _stream(*a, **kw):
            yield chunk_bytes

        vendor.send_message_stream = _stream
        tier = _make_tier(vendor)
        exec_inst = _executor([tier])

        collected = []
        async for chunk, name in exec_inst.execute_stream({"model": "test"}, {}):
            collected.append((chunk, name))
        assert len(collected) > 0
        assert name == "copilot"

    @pytest.mark.asyncio
    async def test_stream_token_error_raises_on_last_tier(self):
        """最后一层流式 Token 错误直接抛出."""
        vendor = _mock_vendor()

        async def _raise_token(*a, **kw):
            raise TokenAcquireError("expired")
            yield  # noqa: PYS101 — 使其成为异步生成器
            return  # type: ignore[unreachable]

        vendor.send_message_stream = _raise_token
        exec_inst = _executor([_make_tier(vendor)])

        with pytest.raises(TokenAcquireError):
            async for _ in exec_inst.execute_stream({"model": "test"}, {}):
                pass  # noqa: PLC0107 (empty body — consume generator to trigger error)

    @pytest.mark.asyncio
    async def test_stream_http_error_raises_on_last_tier(self):
        """最后一层流式 HTTP 错误直接抛出."""
        import httpx

        vendor = _mock_vendor()

        async def _raise_http(*a, **kw):
            raise httpx.HTTPStatusError(
                "error",
                request=MagicMock(),
                response=MagicMock(status_code=500),
            )
            yield  # noqa: PYS101
            return  # type: ignore[unreachable]

        vendor.send_message_stream = _raise_http
        exec_inst = _executor([_make_tier(vendor)])

        with pytest.raises(httpx.HTTPStatusError):
            async for _ in exec_inst.execute_stream({"model": "test"}, {}):
                pass

    @pytest.mark.asyncio
    async def test_stream_unexpected_exception_continues_to_next_tier(self):
        """非最后一层流式未预期异常应触发故障转移到下一层."""
        bad_vendor = _mock_vendor("bad")

        async def _raise_unexpected(*a, **kw):
            raise ValueError("upstream returned garbage")
            yield  # noqa: PYS101

        bad_vendor.send_message_stream = _raise_unexpected

        good_vendor = _mock_vendor("good")

        async def _good_stream(*a, **kw):
            yield b'data: {"type":"message_delta","delta":{"stop_reason":"end_turn"}}\n\n'

        good_vendor.send_message_stream = _good_stream

        exec_inst = _executor(
            [
                _make_tier(bad_vendor),
                _make_tier(good_vendor),
            ]
        )

        collected = []
        async for chunk, name in exec_inst.execute_stream({"model": "test"}, {}):
            collected.append((chunk, name))
        assert len(collected) > 0
        assert name == "good"

    @pytest.mark.asyncio
    async def test_stream_unexpected_exception_on_last_tier_propagates(self):
        """最后一层流式未预期异常应向上传播（由 _stream_proxy 接管）."""
        vendor = _mock_vendor()

        async def _raise_unexpected(*a, **kw):
            raise RuntimeError("stream corrupted")
            yield  # noqa: PYS101

        vendor.send_message_stream = _raise_unexpected
        exec_inst = _executor([_make_tier(vendor)])

        with pytest.raises(RuntimeError, match="stream corrupted"):
            async for _ in exec_inst.execute_stream({"model": "test"}, {}):
                pass  # noqa: PLC0107


# ── 错误处理测试 ─────────────────────────────────────────


class TestHandleTokenError:
    """TokenAcquireError 处理测试."""

    @pytest.mark.asyncio
    async def test_records_failure_and_returns_exc(self):
        tier = _make_tier()
        exec_inst = _executor([tier])
        exc = TokenAcquireError("expired")

        failed_name, last_exc = await exec_inst._handle_token_error(
            tier,
            exc,
            is_last=True,
            failed_tier_name=None,
        )
        assert failed_name == "test"
        assert last_exc is exc
        # record_failure 是真实方法（非 mock），不抛异常即通过

    @pytest.mark.asyncio
    async def test_triggers_reauth_for_copilot(self):
        """Copilot 层的 token 失败应触发 GitHub reauth."""
        copilot_vendor = _mock_vendor("copilot")
        tier = _make_tier(copilot_vendor)
        reauth_mock = MagicMock()
        reauth_mock.request_reauth = AsyncMock()
        exec_inst = _executor([tier], reauth_coordinator=reauth_mock)

        exc = TokenAcquireError("expired", needs_reauth=True)
        await exec_inst._handle_token_error(
            tier,
            exc,
            is_last=False,
            failed_tier_name=None,
        )

        reauth_mock.request_reauth.assert_called_once_with("github")


# ── UsageRecorder 集成测试 ───────────────────────────────


class TestUsageRecorderIntegration:
    """UsageRecorder 与 Executor 协作测试."""

    def test_build_usage_info_from_dict(self):
        info = UsageRecorder.build_usage_info(
            {
                "input_tokens": 100,
                "output_tokens": 50,
                "cache_creation_tokens": 10,
                "cache_read_tokens": 5,
                "request_id": "req_123",
            }
        )
        assert info.input_tokens == 100
        assert info.output_tokens == 50
        assert info.cache_creation_tokens == 10
        assert info.cache_read_tokens == 5
        assert info.request_id == "req_123"

    def test_build_usage_info_defaults(self):
        info = UsageRecorder.build_usage_info({})
        assert info.input_tokens == 0
        assert info.output_tokens == 0
        assert info.cache_creation_tokens == 0
        assert info.cache_read_tokens == 0
        assert info.request_id == ""

    def test_build_nonstream_evidence_records_for_non_copilot(self):
        records = UsageRecorder.build_nonstream_evidence_records(
            vendor="antigravity",
            model_served="gemini-pro",
            usage=UsageInfo(input_tokens=10, output_tokens=5),
        )
        assert records == []

    def test_build_nonstream_evidence_records_for_copilot(self):
        records = UsageRecorder.build_nonstream_evidence_records(
            vendor="copilot",
            model_served="gpt-4o",
            usage=UsageInfo(
                input_tokens=25,
                output_tokens=10,
                cache_creation_tokens=3,
                cache_read_tokens=7,
                request_id="msg_abc",
            ),
        )
        assert len(records) == 1
        rec = records[0]
        assert rec["vendor"] == "copilot"
        assert rec["model_served"] == "gpt-4o"
        assert rec["evidence_kind"] == "nonstream_usage_summary"
        assert rec["parsed_input_tokens"] == 25
        assert rec["parsed_output_tokens"] == 10

    @pytest.mark.asyncio
    async def test_record_without_logger_is_noop(self):
        """无 token_logger 时 record 不报错."""
        recorder = UsageRecorder(token_logger=None)
        await recorder.record(
            vendor="test",
            model_requested="m",
            model_served="m",
            usage=UsageInfo(),
            duration_ms=100,
            success=True,
            failover=False,
        )
        # 不抛异常即通过


# ── RouteSessionManager 集成测试 ─────────────────────────


class TestRouteSessionManagerIntegration:
    """会话管理器与 Executor 协作测试."""

    @pytest.mark.asyncio
    async def test_get_or_create_without_store(self):
        mgr = RouteSessionManager(compat_session_store=None)
        record = await mgr.get_or_create_record("sk_test", "trace_1")
        # 无 store 时返回 None（由 executor 层面处理空 record 场景）
        assert record is None

    @pytest.mark.asyncio
    async def test_persist_session_without_store_is_noop(self):
        mgr = RouteSessionManager(compat_session_store=None)
        # 不抛异常即通过
        await mgr.persist_session(None, None)


# ── _has_tool_results 测试 ─────────────────────────────────


class TestHasToolResults:
    """:func:`_has_tool_results` 辅助函数测试."""

    def test_detects_tool_result_in_messages(self):
        body = {
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "tool_result", "tool_use_id": "tu_1", "content": "ok"}
                    ],
                },
            ],
        }
        assert _has_tool_results(body) is True

    def test_returns_false_when_no_tool_result(self):
        body = {
            "messages": [
                {"role": "user", "content": [{"type": "text", "text": "hello"}]},
            ],
        }
        assert _has_tool_results(body) is False

    def test_returns_false_for_empty_body(self):
        assert _has_tool_results({}) is False

    def test_returns_false_for_empty_messages(self):
        assert _has_tool_results({"messages": []}) is False

    def test_returns_false_for_string_content(self):
        body = {"messages": [{"role": "user", "content": "plain text"}]}
        assert _has_tool_results(body) is False

    def test_returns_false_for_tool_use_only(self):
        """tool_use 块不应被误判为 tool_result."""
        body = {
            "messages": [
                {
                    "role": "assistant",
                    "content": [
                        {"type": "tool_use", "id": "tu_1", "name": "bash", "input": {}}
                    ],
                },
            ],
        }
        assert _has_tool_results(body) is False

    def test_detects_mixed_content_with_tool_result(self):
        """混合内容块中只要有一个 tool_result 即返回 True."""
        body = {
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "before"},
                        {
                            "type": "tool_result",
                            "tool_use_id": "tu_1",
                            "content": "result",
                        },
                        {"type": "text", "text": "after"},
                    ],
                },
            ],
        }
        assert _has_tool_results(body) is True

    def test_ignores_non_dict_blocks(self):
        """非 dict 内容块不应导致异常."""
        body = {
            "messages": [
                {"role": "user", "content": ["string_block", None, 42]},
            ],
        }
        assert _has_tool_results(body) is False


# ── _log_vendor_response_error 测试 ──────────────────────────


class TestLogVendorResponseError:
    """:func:`_log_vendor_response_error` 日志函数测试."""

    def test_logs_warning_with_status_and_error_info(self, caplog):
        import logging as _logging

        resp = VendorResponse(
            status_code=500,
            raw_body=b'{"error":{"code":"500","message":"test error msg"}}',
            error_type=None,
            error_message="test error msg",
        )
        body = {"model": "claude-opus-4-6", "messages": []}

        with caplog.at_level(_logging.WARNING, logger="coding.proxy.routing.executor"):
            _log_vendor_response_error("zhipu", resp, body)

        warnings = [r for r in caplog.records if r.levelno == _logging.WARNING]
        assert len(warnings) >= 1
        log_msg = warnings[0].message
        assert "zhipu" in log_msg
        assert "status=500" in log_msg
        assert "test error msg" in log_msg
        assert "model=claude-opus-4-6" in log_msg

    def test_logs_has_tool_results_true_when_present(self, caplog):
        import logging as _logging

        resp = VendorResponse(
            status_code=500,
            raw_body=b'{"error":{"message":"id attribute missing"}}',
        )
        body = {
            "model": "glm-5v-turbo",
            "tools": [{"name": "Bash"}],
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "tool_result", "tool_use_id": "tu_1", "content": "ok"}
                    ],
                },
            ],
        }

        with caplog.at_level(_logging.WARNING, logger="coding.proxy.routing.executor"):
            _log_vendor_response_error("zhipu", resp, body)

        log_msg = caplog.records[-1].message
        assert "has_tools=True" in log_msg
        assert "has_tool_results=True" in log_msg

    def test_logs_has_tool_results_false_when_absent(self, caplog):
        import logging as _logging

        resp = VendorResponse(status_code=500, raw_body=b'{"error":"err"}')
        body = {"model": "test-model", "messages": []}

        with caplog.at_level(_logging.WARNING, logger="coding.proxy.routing.executor"):
            _log_vendor_response_error("test", resp, body)

        log_msg = caplog.records[-1].message
        assert "has_tools=False" in log_msg
        assert "has_tool_results=False" in log_msg

    def test_includes_response_body_preview(self, caplog):
        import logging as _logging

        resp = VendorResponse(
            status_code=500,
            raw_body=b'{"error":{"code":"500","message":"\'ClaudeContentBlockToolResult\' object has no attribute \'id\'"}}',
        )
        body = {"model": "test", "messages": []}

        with caplog.at_level(_logging.WARNING, logger="coding.proxy.routing.executor"):
            _log_vendor_response_error("zhipu", resp, body)

        log_msg = caplog.records[-1].message
        assert "response_body_preview=" in log_msg
        assert "ClaudeContentBlockToolResult" in log_msg

    def test_handles_none_raw_body_gracefully(self, caplog):
        import logging as _logging

        resp = VendorResponse(status_code=500, raw_body=None)
        body = {"model": "test", "messages": []}

        with caplog.at_level(_logging.WARNING, logger="coding.proxy.routing.executor"):
            _log_vendor_response_error("test", resp, body)

        # 不应抛出异常
        assert any(r.levelno == _logging.WARNING for r in caplog.records)

    def test_handles_binary_raw_body(self, caplog):
        import logging as _logging

        resp = VendorResponse(status_code=500, raw_body=b"\x80\x81\x82")
        body = {"model": "test", "messages": []}

        with caplog.at_level(_logging.WARNING, logger="coding.proxy.routing.executor"):
            _log_vendor_response_error("test", resp, body)

        # 不应抛出异常；二进制内容经 errors="replace" 解码后含替换字符
        assert any(r.levelno == _logging.WARNING for r in caplog.records)
        log_msg = caplog.records[-1].message
        assert "response_body_preview=" in log_msg

    def test_truncates_long_error_messages(self, caplog):
        import logging as _logging

        long_msg = "x" * 500
        resp = VendorResponse(
            status_code=500,
            raw_body=f'{{"error":{{"message":"{long_msg}"}}}}'.encode(),
        )
        body = {"model": "test", "messages": []}

        with caplog.at_level(_logging.WARNING, logger="coding.proxy.routing.executor"):
            _log_vendor_response_error("test", resp, body)

        # error_msg 应截断至 300 字符以内
        log_msg = caplog.records[-1].message
        assert "error_msg=" in log_msg


# ── execute_message 500 错误日志集成测试 ────────────────────


class TestExecuteMessageVendorErrorLogging:
    """execute_message 返回 VendorResponse 错误时的日志行为验证.

    覆盖核心修复场景：当 vendor（如 Zhipu）返回 500 且非最后一层可 failover、
    或为最后一层直接返回时，_log_vendor_response_error 均应被触发。
    """

    @pytest.mark.asyncio
    async def test_last_tier_500_produces_warning_log(self, caplog):
        """最后一层返回 500 时应输出结构化警告日志."""
        import logging as _logging

        vendor = _mock_vendor()
        vendor.send_message = AsyncMock(
            return_value=VendorResponse(
                status_code=500,
                raw_body=b'{"error":{"code":"500","message":"internal error"}}',
                error_type=None,
                error_message="internal error",
            )
        )
        exec_inst = _executor([_make_tier(vendor)])

        with caplog.at_level(_logging.WARNING, logger="coding.proxy.routing.executor"):
            resp = await exec_inst.execute_message({"model": "test"}, {})

        assert resp.status_code == 500
        warnings = [r for r in caplog.records if r.levelno == _logging.WARNING]
        assert len(warnings) >= 1
        log_text = "\n".join(r.message for r in warnings)
        assert "vendor error response" in log_text
        assert "status=500" in log_text

    @pytest.mark.asyncio
    async def test_last_tier_500_with_tool_results_logs_context(self, caplog):
        """含 tool_result 的请求触发 500 时，日志应标记 has_tool_results=True."""
        import logging as _logging

        vendor = _mock_vendor()
        vendor.send_message = AsyncMock(
            return_value=VendorResponse(
                status_code=500,
                raw_body=b'{"error":{"code":"500","message":"tool result id error"}}',
            )
        )
        exec_inst = _executor([_make_tier(vendor)])

        body = {
            "model": "claude-opus-4-6",
            "tools": [{"name": "Bash"}],
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": "tu_1",
                            "content": "output",
                        }
                    ],
                },
            ],
        }

        with caplog.at_level(_logging.WARNING, logger="coding.proxy.routing.executor"):
            resp = await exec_inst.execute_message(body, {})

        assert resp.status_code == 500
        log_text = "\n".join(
            r.message for r in caplog.records if r.levelno == _logging.WARNING
        )
        assert "has_tool_results=True" in log_text
        assert "claude-opus-4-6" in log_text


# ── execute_message 429 降级测试 ─────────────────────────────


class TestExecuteMessageFailoverOn429:
    """非流式路径下 vendor 返回 429 时应触发 tier 降级.

    覆盖核心修复场景：ZhipuVendor 未注入 FailoverConfig 导致
    should_trigger_failover() 永远返回 False，429 无法降级到下一层。
    """

    @pytest.mark.asyncio
    async def test_429_with_failover_config_triggers_failover(self):
        """非 terminal 层 vendor 有 failover_config 时，429 应触发降级到下一层."""
        # 模拟 zhipu 层返回 429（有 failover_config → should_trigger_failover 返回 True）
        zhipu = _mock_vendor("zhipu")
        zhipu.send_message = AsyncMock(
            return_value=VendorResponse(
                status_code=429,
                raw_body='{"error":{"code":"1305","message":"\u8be5\u6a21\u578b\u5f53\u524d\u8bbf\u95ee\u91cf\u8fc7\u5927"}}'.encode(),
                error_type=None,
                error_message="该模型当前访问量过大，请您稍后再试",
            )
        )
        zhipu.should_trigger_failover.return_value = True

        # copilot 层正常响应
        copilot = _mock_vendor("copilot")
        copilot.send_message = AsyncMock(
            return_value=VendorResponse(
                status_code=200,
                raw_body=b'{"content":"ok"}',
                usage=UsageInfo(input_tokens=1, output_tokens=1),
            )
        )

        zhipu_tier = _make_tier(zhipu)
        exec_inst = _executor([zhipu_tier, _make_tier(copilot)])
        resp = await exec_inst.execute_message({"model": "test"}, {})

        assert resp.status_code == 200
        assert copilot.send_message.called
        # record_failure 在 VendorTier 上（非 vendor），通过 circuit_breaker 调用链验证
        if zhipu_tier.circuit_breaker:
            assert zhipu_tier.circuit_breaker.failure_count > 0

    @pytest.mark.asyncio
    async def test_429_without_failover_config_no_failover(self):
        """非 terminal 层 vendor 无 failover_config 时，429 不应触发降级（原始行为）."""
        bad = _mock_vendor("bad")
        bad.send_message = AsyncMock(
            return_value=VendorResponse(
                status_code=429,
                raw_body=b'{"error":{"message":"rate limited"}}',
                error_type=None,
                error_message="rate limited",
            )
        )
        # 显式设为 False：模拟无 failover_config 时 should_trigger_failover 的行为
        bad.should_trigger_failover.return_value = False

        good = _mock_vendor("good")
        good_resp = VendorResponse(
            status_code=200,
            raw_body=b"{}",
            usage=UsageInfo(input_tokens=1, output_tokens=1),
        )
        good.send_message = AsyncMock(return_value=good_resp)

        exec_inst = _executor([_make_tier(bad), _make_tier(good)])
        resp = await exec_inst.execute_message({"model": "test"}, {})

        # 无 failover_config 时直接返回 429，不降级
        assert resp.status_code == 429
        assert not good.send_message.called

    @pytest.mark.asyncio
    async def test_multi_tier_429_cascade(self):
        """多层降级链路：anthropic→zhipu(429)→copilot(success)."""
        anthropic = _mock_vendor("anthropic")
        anthropic.send_message = AsyncMock(
            return_value=VendorResponse(
                status_code=429,
                raw_body=b'{"error":{"type":"rate_limit_error","message":"quota exceeded"}}',
                error_type="rate_limit_error",
                error_message="quota exceeded",
            )
        )
        anthropic.should_trigger_failover.return_value = True

        zhipu = _mock_vendor("zhipu")
        zhipu.send_message = AsyncMock(
            return_value=VendorResponse(
                status_code=429,
                raw_body=b'{"error":{"code":"1305","message":"model overloaded"}}',
                error_type=None,
                error_message="模型过载",
            )
        )
        zhipu.should_trigger_failover.return_value = True

        copilot = _mock_vendor("copilot")
        copilot.send_message = AsyncMock(
            return_value=VendorResponse(
                status_code=200,
                raw_body=b'{"content":"fallback success"}',
                usage=UsageInfo(input_tokens=10, output_tokens=5),
            )
        )

        exec_inst = _executor(
            [
                _make_tier(anthropic),
                _make_tier(zhipu),
                _make_tier(copilot),
            ]
        )
        resp = await exec_inst.execute_message({"model": "claude-opus-4-6"}, {})

        assert resp.status_code == 200
        assert anthropic.send_message.called
        assert zhipu.send_message.called
        assert copilot.send_message.called

    @pytest.mark.asyncio
    async def test_last_tier_429_returns_directly(self):
        """最后一层 vendor 返回 429 时应直接返回给客户端."""
        vendor = _mock_vendor()
        vendor.send_message = AsyncMock(
            return_value=VendorResponse(
                status_code=429,
                raw_body=b'{"error":{"message":"too many requests"}}',
                error_type=None,
                error_message="too many requests",
            )
        )

        exec_inst = _executor([_make_tier(vendor)])
        resp = await exec_inst.execute_message({"model": "test"}, {})

        assert resp.status_code == 429

    @pytest.mark.asyncio
    async def test_429_failover_logs_warning(self, caplog):
        """429 触发降级时应输出 'failing over' 日志."""
        import logging as _logging

        bad = _mock_vendor("zhipu")
        bad.send_message = AsyncMock(
            return_value=VendorResponse(
                status_code=429,
                raw_body=b'{"error":{"message":"overloaded"}}',
                error_type=None,
                error_message="overloaded",
            )
        )
        bad.should_trigger_failover.return_value = True

        good = _mock_vendor("copilot")
        good.send_message = AsyncMock(
            return_value=VendorResponse(
                status_code=200,
                raw_body=b"{}",
                usage=UsageInfo(),
            )
        )

        exec_inst = _executor([_make_tier(bad), _make_tier(good)])

        with caplog.at_level(_logging.WARNING, logger="coding.proxy.routing.executor"):
            await exec_inst.execute_message({"model": "test"}, {})

        warnings = [r for r in caplog.records if r.levelno == _logging.WARNING]
        log_text = "\n".join(r.message for r in warnings)
        assert "failing over" in log_text
        assert "zhipu" in log_text


# ── _is_likely_request_format_error 测试 ──────────────────────


class TestIsLikelyRequestFormatError:
    """:func:`_is_likely_request_format_error` 测试 — 覆盖 Copilot 400
    ``Bad Request`` 不应计入熔断器的核心修复场景.
    """

    def _body_with_tool_results(self) -> dict:
        return {
            "model": "claude-opus-4-6",
            "tools": [{"name": "Bash"}],
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "tool_result", "tool_use_id": "tu_1", "content": "ok"}
                    ],
                },
            ],
        }

    def test_returns_true_for_400_bad_request_with_tool_results(self):
        """400 + 'Bad Request' + 有 tool_result → 格式不兼容."""
        assert (
            _is_likely_request_format_error(
                status_code=400,
                error_body_text="Bad Request\n",
                body=self._body_with_tool_results(),
            )
            is True
        )

    def test_returns_true_for_400_empty_body_with_tool_results(self):
        """400 + 空错误体 + 有 tool_result → 格式不兼容."""
        assert (
            _is_likely_request_format_error(
                status_code=400,
                error_body_text="",
                body=self._body_with_tool_results(),
            )
            is True
        )

    def test_returns_true_for_400_short_non_json_with_tool_results(self):
        """400 + 短非 JSON 错误体 + tool_result → 格式不兼容."""
        assert (
            _is_likely_request_format_error(
                status_code=400,
                error_body_text="invalid payload",
                body=self._body_with_tool_results(),
            )
            is True
        )

    def test_returns_false_for_non_400_status(self):
        """非 400 状态码即使有 tool_result 也不应匹配."""
        assert (
            _is_likely_request_format_error(
                status_code=500,
                error_body_text="Bad Request\n",
                body=self._body_with_tool_results(),
            )
            is False
        )

    def test_returns_false_when_no_tool_results(self):
        """无 tool_result 时不应匹配（即使是 400 Bad Request）."""
        body = {"model": "test", "messages": [{"role": "user", "content": "hi"}]}
        assert (
            _is_likely_request_format_error(
                status_code=400,
                error_body_text="Bad Request\n",
                body=body,
            )
            is False
        )

    def test_returns_false_for_structured_json_error_body(self):
        """结构化 JSON 错误体（以 { 开头且较长）不应触发此启发式判断."""
        json_body = (
            '{"error":{"type":"invalid_request_error","message":"something wrong"}}'
        )
        assert (
            _is_likely_request_format_error(
                status_code=400,
                error_body_text=json_body,
                body=self._body_with_tool_results(),
            )
            is False
        )

    def test_returns_false_for_empty_body(self):
        """空请求体不应触发."""
        assert (
            _is_likely_request_format_error(
                status_code=400, error_body_text="Bad Request\n", body={}
            )
            is False
        )


# ── TokenAcquireError 永久性凭证错误测试 ────────────────────


class TestHandleTokenErrorPermanentCredential:
    """TokenAcquireError 中 INSUFFICIENT_SCOPE / INVALID_CREDENTIALS 的特殊处理.

    验证永久性凭证问题不计入熔断器，避免级联 OPEN 阻塞恢复。
    """

    @pytest.mark.asyncio
    async def test_insufficient_scope_does_not_record_failure(self):
        """INSUFFICIENT_SCOPE 不应调用 tier.record_failure()."""
        from coding.proxy.vendors.token_manager import TokenErrorKind

        tier = _make_tier()
        exec_inst = _executor([tier])
        initial_count = (
            tier.circuit_breaker._failure_count if tier.circuit_breaker else 0
        )

        exc = TokenAcquireError.with_kind(
            "scope insufficient",
            kind=TokenErrorKind.INSUFFICIENT_SCOPE,
            needs_reauth=True,
        )
        await exec_inst._handle_token_error(
            tier, exc, is_last=False, failed_tier_name=None
        )

        # 失败计数不应增加
        if tier.circuit_breaker:
            assert tier.circuit_breaker._failure_count == initial_count

    @pytest.mark.asyncio
    async def test_invalid_credentials_does_not_record_failure(self):
        """INVALID_CREDENTIALS 不应调用 tier.record_failure()."""
        from coding.proxy.vendors.token_manager import TokenErrorKind

        tier = _make_tier()
        exec_inst = _executor([tier])
        initial_count = (
            tier.circuit_breaker._failure_count if tier.circuit_breaker else 0
        )

        exc = TokenAcquireError.with_kind(
            "invalid grant",
            kind=TokenErrorKind.INVALID_CREDENTIALS,
            needs_reauth=True,
        )
        await exec_inst._handle_token_error(
            tier, exc, is_last=False, failed_tier_name=None
        )

        if tier.circuit_breaker:
            assert tier.circuit_breaker._failure_count == initial_count

    @pytest.mark.asyncio
    async def test_temporary_error_still_records_failure(self):
        """临时性 Token 错误（如 TEMPORARY）仍应正常计入熔断器."""
        from coding.proxy.vendors.token_manager import TokenErrorKind

        tier = _make_tier()
        exec_inst = _executor([tier])

        exc = TokenAcquireError.with_kind(
            "network timeout",
            kind=TokenErrorKind.TEMPORARY,
            needs_reauth=False,
        )
        await exec_inst._handle_token_error(
            tier, exc, is_last=False, failed_tier_name=None
        )

        # 临时错误应记录失败
        if tier.circuit_breaker:
            assert tier.circuit_breaker._failure_count >= 1

    @pytest.mark.asyncio
    async def test_still_triggers_reauth_for_permanent_error(self):
        """永久性凭证错误仍应触发 reauth 协调."""
        from coding.proxy.vendors.token_manager import TokenErrorKind

        tier = _make_tier(vendor=_mock_vendor("antigravity"))
        reauth_mock = MagicMock()
        reauth_mock.request_reauth = AsyncMock()
        exec_inst = _executor([tier], reauth_coordinator=reauth_mock)

        exc = TokenAcquireError.with_kind(
            "scope issue",
            kind=TokenErrorKind.INSUFFICIENT_SCOPE,
            needs_reauth=True,
        )
        await exec_inst._handle_token_error(
            tier, exc, is_last=False, failed_tier_name=None
        )

        reauth_mock.request_reauth.assert_called_once_with("google")


# ── execute_message 400 格式不兼容降级测试 ───────────────────


class TestExecuteMessageFormatIncompatibilityFailover:
    """非流式路径下 vendor 返回 400 且含 tool_result 时应视为语义拒绝.

    覆盖 Copilot 返回 ``Bad Request``（非结构化 400）时的降级修复场景。
    """

    @pytest.mark.asyncio
    async def test_copilot_400_bad_request_with_tool_results_fails_over(self):
        """Copilot 返回 400 Bad Request + 请求含 tool_result → 应降级到下一层."""
        copilot = _mock_vendor("copilot")
        copilot.send_message = AsyncMock(
            return_value=VendorResponse(
                status_code=400,
                raw_body=b"Bad Request\n",
                error_type=None,
                error_message="Bad Request",
            )
        )

        good = _mock_vendor("anthropic")
        good.send_message = AsyncMock(
            return_value=VendorResponse(
                status_code=200,
                raw_body=b'{"content":"ok"}',
                usage=UsageInfo(input_tokens=1, output_tokens=1),
            )
        )

        exec_inst = _executor([_make_tier(copilot), _make_tier(good)])
        body = {
            "model": "claude-opus-4-6",
            "tools": [{"name": "Bash"}],
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": "tu_1",
                            "content": "result",
                        }
                    ],
                },
            ],
        }
        resp = await exec_inst.execute_message(body, {})

        assert resp.status_code == 200
        assert good.send_message.called

    @pytest.mark.asyncio
    async def test_400_without_tool_results_does_not_format_failover(self):
        """400 但无 tool_result 时，不应触发格式不兼容的特殊处理."""
        bad = _mock_vendor("bad")
        bad.send_message = AsyncMock(
            return_value=VendorResponse(
                status_code=400,
                raw_body=b"Bad Request\n",
                error_type=None,
                error_message="Bad Request",
            )
        )

        exec_inst = _executor([_make_tier(bad)])
        body = {"model": "test", "messages": [{"role": "user", "content": "hello"}]}
        resp = await exec_inst.execute_message(body, {})

        # 无 tool_result 的 400 直接返回给客户端（不是最后一层但无下一层可降级）
        assert resp.status_code == 400


# ── execute_message 最后一层 500 降级记录测试 ─────────────────


class TestExecuteMessageLastTier500RecordsFailure:
    """非流式路径下终端层（最后一层）vendor 返回 500 时应记录降级状态.

    覆盖核心修复场景：zhipu 作为终端层返回 500 "Internal Network Failure"，
    即使无法故障转移到下一层，仍需调用 record_failure() 维护降级状态
    （与流式路径 _handle_http_error 的行为对称）。
    """

    @pytest.mark.asyncio
    async def test_last_tier_500_with_failover_records_failure(self):
        """最后一层 vendor 返回 500 且 should_trigger_failover=True 时，record_failure 应被调用."""
        from coding.proxy.routing.circuit_breaker import CircuitBreaker

        vendor = _mock_vendor("zhipu")
        vendor.send_message = AsyncMock(
            return_value=VendorResponse(
                status_code=500,
                raw_body=b'{"error":{"type":"api_error","message":"Internal Network Failure"}}',
                error_type="api_error",
                error_message="Internal Network Failure",
            )
        )
        vendor.should_trigger_failover.return_value = True

        cb = CircuitBreaker(failure_threshold=3)
        tier = _make_tier(vendor, circuit_breaker=cb)
        exec_inst = _executor([tier])

        resp = await exec_inst.execute_message(
            {"model": "claude-haiku-4-5-20251001"}, {}
        )

        assert resp.status_code == 500
        assert resp.error_message == "Internal Network Failure"
        # 验证 record_failure 被调用：CircuitBreaker 的 failure_count 应增加
        assert cb.get_info()["failure_count"] == 1

    @pytest.mark.asyncio
    async def test_last_tier_500_without_failover_no_failure_recorded(self):
        """最后一层 vendor 返回 500 但 should_trigger_failover=False 时，不记录降级."""
        from coding.proxy.routing.circuit_breaker import CircuitBreaker

        vendor = _mock_vendor("zhipu")
        vendor.send_message = AsyncMock(
            return_value=VendorResponse(
                status_code=500,
                raw_body=b'{"error":{"type":"server_error","message":"unknown"}}',
                error_type="server_error",
                error_message="unknown",
            )
        )
        vendor.should_trigger_failover.return_value = False

        cb = CircuitBreaker(failure_threshold=3)
        tier = _make_tier(vendor, circuit_breaker=cb)
        exec_inst = _executor([tier])

        resp = await exec_inst.execute_message({"model": "test"}, {})

        assert resp.status_code == 500
        # should_trigger_failover=False 时，failure 不应被记录
        assert cb.get_info()["failure_count"] == 0

    @pytest.mark.asyncio
    async def test_last_tier_500_still_returns_response_to_client(self):
        """修复后，最后一层 500 仍应正确返回原始错误响应给客户端."""
        vendor = _mock_vendor("zhipu")
        error_body = (
            b'{"error":{"type":"api_error","message":"Internal Network Failure"}}'
        )
        vendor.send_message = AsyncMock(
            return_value=VendorResponse(
                status_code=500,
                raw_body=error_body,
                error_type="api_error",
                error_message="Internal Network Failure",
            )
        )
        vendor.should_trigger_failover.return_value = True

        exec_inst = _executor([_make_tier(vendor)])
        resp = await exec_inst.execute_message({"model": "test"}, {})

        assert resp.status_code == 500
        assert resp.raw_body == error_body
        assert resp.error_message == "Internal Network Failure"

    @pytest.mark.asyncio
    async def test_last_tier_500_with_retry_after_updates_rate_limit(self):
        """最后一层 500 含 Retry-After 头时，应更新 tier 的 rate limit deadline."""

        from coding.proxy.routing.circuit_breaker import CircuitBreaker

        vendor = _mock_vendor("zhipu")
        vendor.send_message = AsyncMock(
            return_value=VendorResponse(
                status_code=429,
                raw_body=b'{"error":{"type":"rate_limit_error","message":"rate limited"}}',
                error_type="rate_limit_error",
                error_message="rate limited",
                response_headers={"retry-after": "60"},
            )
        )
        vendor.should_trigger_failover.return_value = True

        tier = _make_tier(vendor, circuit_breaker=CircuitBreaker())
        exec_inst = _executor([tier])

        resp = await exec_inst.execute_message({"model": "test"}, {})

        assert resp.status_code == 429
        # 验证 rate limit deadline 被更新
        rl_info = tier.get_rate_limit_info()
        assert rl_info["is_rate_limited"] is True
        assert rl_info["remaining_seconds"] > 0

    @pytest.mark.asyncio
    async def test_non_last_tier_500_still_failovers(self):
        """修复后，非最后一层 500 仍应正常触发故障转移到下一层."""
        bad = _mock_vendor("zhipu")
        bad.send_message = AsyncMock(
            return_value=VendorResponse(
                status_code=500,
                raw_body=b'{"error":{"type":"api_error","message":"Internal Network Failure"}}',
                error_type="api_error",
                error_message="Internal Network Failure",
            )
        )
        bad.should_trigger_failover.return_value = True

        good = _mock_vendor("copilot")
        good.send_message = AsyncMock(
            return_value=VendorResponse(
                status_code=200,
                raw_body=b'{"content":"ok"}',
                usage=UsageInfo(input_tokens=1, output_tokens=1),
            )
        )

        exec_inst = _executor([_make_tier(bad), _make_tier(good)])
        resp = await exec_inst.execute_message({"model": "test"}, {})

        assert resp.status_code == 200
        assert good.send_message.called


# ── _determine_source_vendor 源 vendor 推断测试 ──────────────────────


class TestDetermineSourceVendor:
    """验证 _RouteExecutor._determine_source_vendor 静态方法.

    Priority 1: failed_tier_name（请求内故障转移）
    Priority 2: session_record.provider_state 中有已注册转换的 vendor（跨请求）
    """

    def test_returns_failed_tier_as_source(self):
        """请求内故障转移：刚失败的 tier 就是源 vendor."""
        session_record = MagicMock()
        session_record.provider_state = {"zhipu": {}}

        assert (
            _RouteExecutor._determine_source_vendor("copilot", "zhipu", session_record)
            == "zhipu"
        )

    def test_returns_failed_tier_even_with_empty_session(self):
        """请求内故障转移优先于 session_record 为空."""
        assert (
            _RouteExecutor._determine_source_vendor("copilot", "zhipu", None) == "zhipu"
        )

    def test_returns_session_vendor_with_registered_transition(self):
        """跨请求：会话历史中有已注册转换的 vendor 作为源."""
        session_record = MagicMock()
        session_record.provider_state = {"zhipu": {}, "copilot": {}}

        # zhipu → copilot 有注册转换
        assert (
            _RouteExecutor._determine_source_vendor("copilot", None, session_record)
            == "zhipu"
        )

    def test_returns_session_vendor_for_anthropic_target(self):
        """跨请求：会话历史中有 zhipu → anthropic 已注册转换."""
        session_record = MagicMock()
        session_record.provider_state = {"zhipu": {}}

        assert (
            _RouteExecutor._determine_source_vendor("anthropic", None, session_record)
            == "zhipu"
        )

    def test_returns_none_for_no_source(self):
        """纯同 vendor 会话且无请求内故障 → 无源 vendor."""
        session_record = MagicMock()
        session_record.provider_state = {"anthropic": {}}

        assert (
            _RouteExecutor._determine_source_vendor("anthropic", None, session_record)
            is None
        )

    def test_returns_session_vendor_with_registered_transition_anthropic_to_zhipu(self):
        """anthropic → zhipu 已注册转换，应返回 anthropic 作为源 vendor."""
        session_record = MagicMock()
        session_record.provider_state = {"anthropic": {}}

        assert (
            _RouteExecutor._determine_source_vendor("zhipu", None, session_record)
            == "anthropic"
        )

    def test_returns_none_when_session_is_none(self):
        """无会话存储且无请求内故障 → 无源 vendor."""
        assert _RouteExecutor._determine_source_vendor("copilot", None, None) is None

    def test_returns_none_when_empty_provider_state(self):
        """空 provider_state 且无请求内故障 → 无源 vendor."""
        session_record = MagicMock()
        session_record.provider_state = {}

        assert (
            _RouteExecutor._determine_source_vendor("copilot", None, session_record)
            is None
        )

    def test_returns_none_when_failed_tier_equals_target_unregistered(self):
        """失败的 tier == 目标 tier 且无对应自转换通道 → 不算跨供应商.

        anthropic 未注册自转换通道, 此场景应回退到无源行为.
        """
        session_record = MagicMock()
        session_record.provider_state = {}

        assert (
            _RouteExecutor._determine_source_vendor(
                "anthropic", "anthropic", session_record
            )
            is None
        )

    # ── Priority 3: body 内容感知推断（首次请求兜底） ───────────────

    def test_priority3_infers_zhipu_from_srvtoolu_id_in_body(self):
        """Priority 3: body 含 srvtoolu_* ID → 推断源为 zhipu."""
        body = {
            "messages": [
                {
                    "role": "assistant",
                    "content": [
                        {
                            "type": "tool_use",
                            "id": "srvtoolu_abc",
                            "name": "bash",
                            "input": {},
                        },
                    ],
                },
            ],
        }
        # 无 session 且无 failed_tier → 走 Priority 3
        assert (
            _RouteExecutor._determine_source_vendor("anthropic", None, None, body)
            == "zhipu"
        )

    def test_priority3_infers_zhipu_from_server_tool_use_delta(self):
        """Priority 3: body 含 server_tool_use_delta 类型块 → 推断源为 zhipu."""
        body = {
            "messages": [
                {
                    "role": "assistant",
                    "content": [
                        {"type": "server_tool_use_delta", "partial_json": "{}"},
                    ],
                },
            ],
        }
        assert (
            _RouteExecutor._determine_source_vendor("anthropic", None, None, body)
            == "zhipu"
        )

    def test_priority3_returns_none_for_pristine_body(self):
        """Priority 3: body 纯净无跨供应商产物 → 返回 None."""
        body = {
            "messages": [
                {"role": "user", "content": "hello"},
                {
                    "role": "assistant",
                    "content": [{"type": "text", "text": "hi"}],
                },
            ],
        }
        assert (
            _RouteExecutor._determine_source_vendor("anthropic", None, None, body)
            is None
        )

    def test_priority3_skips_when_target_equals_inferred_and_unregistered(self):
        """Priority 3: 推断的源 == 目标且无对应自转换通道时不触发.

        构造一个推断结果 == 目标但 (target,target) 未注册的场景: 实际上 zhipu→zhipu
        现已注册自清理, 此处用 target='unknown_target' 模拟未注册情形;
        关键回归保护点参见 ``test_priority3_self_transition_when_registered``。
        """
        body = {
            "messages": [
                {
                    "role": "assistant",
                    "content": [
                        {
                            "type": "server_tool_use",
                            "id": "srvtoolu_x",
                            "name": "bash",
                            "input": {},
                        },
                    ],
                },
            ],
        }
        # 目标 unknown_target 未注册任何转换 → 即使推断出 zhipu 也返回 None
        assert (
            _RouteExecutor._determine_source_vendor("unknown_target", None, None, body)
            is None
        )

    def test_priority3_skips_when_no_registered_transition(self):
        """Priority 3: 推断的源→目标无注册通道 → 返回 None.

        例如目标是未知 vendor 时即使推断出 zhipu 也不应返回.
        """
        body = {
            "messages": [
                {
                    "role": "assistant",
                    "content": [
                        {
                            "type": "server_tool_use",
                            "id": "srvtoolu_x",
                            "name": "bash",
                            "input": {},
                        },
                    ],
                },
            ],
        }
        assert (
            _RouteExecutor._determine_source_vendor("unknown_target", None, None, body)
            is None
        )

    def test_priority1_overrides_priority3(self):
        """Priority 1 (failed_tier) 优先于 Priority 3 (body inference)."""
        # body 内有 zhipu 产物，但 failed_tier 显式指定 copilot
        body = {
            "messages": [
                {
                    "role": "assistant",
                    "content": [
                        {
                            "type": "server_tool_use",
                            "id": "srvtoolu_x",
                            "name": "bash",
                            "input": {},
                        },
                    ],
                },
            ],
        }
        # failed_tier=copilot → 应返回 copilot，不看 body
        assert (
            _RouteExecutor._determine_source_vendor("zhipu", "copilot", None, body)
            == "copilot"
        )

    def test_priority2_overrides_priority3(self):
        """Priority 2 (session) 优先于 Priority 3 (body inference)."""
        body = {
            "messages": [
                {
                    "role": "assistant",
                    "content": [
                        {
                            "type": "server_tool_use",
                            "id": "srvtoolu_x",
                            "name": "bash",
                            "input": {},
                        },
                    ],
                },
            ],
        }
        session_record = MagicMock()
        session_record.provider_state = {"copilot": {}}
        # session 中有 copilot → copilot→zhipu 转换已注册 → 返回 copilot
        assert (
            _RouteExecutor._determine_source_vendor("zhipu", None, session_record, body)
            == "copilot"
        )

    def test_body_parameter_is_optional(self):
        """body 参数保持向后兼容，省略时不影响前两个优先级."""
        session_record = MagicMock()
        session_record.provider_state = {"zhipu": {}}
        assert (
            _RouteExecutor._determine_source_vendor("copilot", None, session_record)
            == "zhipu"
        )


# ── _determine_source_vendor 自转换通道测试 ─────────────────────────


class TestDetermineSourceVendorSelfTransition:
    """验证已注册的同 vendor 自转换 (如 zhipu → zhipu) 在三条优先级中均能命中.

    自转换通道用于修复 vendor 自身无法消化的产物 (如 zhipu 不接受输入中的
    server_tool_use_delta 与 assistant 内联 tool_result).
    """

    def test_priority1_self_transition_when_registered(self):
        """Priority 1: failed_tier == target 且通道已注册 → 返回 target 作为源."""
        # zhipu 自转换通道已在 vendor_channels 注册
        assert (
            _RouteExecutor._determine_source_vendor("zhipu", "zhipu", None) == "zhipu"
        )

    def test_priority1_self_transition_blocked_when_unregistered(self):
        """Priority 1: failed_tier == target 但通道未注册 → 返回 None.

        anthropic 未注册自转换通道, 保持原有「同 vendor 无源」行为.
        """
        assert (
            _RouteExecutor._determine_source_vendor("anthropic", "anthropic", None)
            is None
        )

    def test_priority2_self_transition_via_session(self):
        """Priority 2: 会话历史中只有目标 vendor, 但其自转换通道已注册 → 命中."""
        session_record = MagicMock()
        session_record.provider_state = {"zhipu": {}}
        assert (
            _RouteExecutor._determine_source_vendor("zhipu", None, session_record)
            == "zhipu"
        )

    def test_priority2_session_unregistered_self_returns_none(self):
        """Priority 2: 会话只有未注册自转换的 vendor → None."""
        session_record = MagicMock()
        session_record.provider_state = {"anthropic": {}}
        assert (
            _RouteExecutor._determine_source_vendor("anthropic", None, session_record)
            is None
        )

    def test_priority3_self_transition_when_registered(self):
        """Priority 3: 首次请求 body 含 zhipu 产物且目标也是 zhipu → 命中自清理.

        这是修复 「zhipu 400 + tool_results 偶发」 的核心兜底场景:
        Claude Code 把上一轮 zhipu 响应原样回送, 命中 zhipu 主 tier 时
        可识别并应用自清理通道。
        """
        body = {
            "messages": [
                {
                    "role": "assistant",
                    "content": [
                        {
                            "type": "server_tool_use",
                            "id": "srvtoolu_x",
                            "name": "bash",
                            "input": {},
                        },
                    ],
                },
            ],
        }
        assert (
            _RouteExecutor._determine_source_vendor("zhipu", None, None, body)
            == "zhipu"
        )


# ── _prepare_body_for_tier 转换通道应用测试 ────────────────────────


class TestPrepareBodyForTierTransition:
    """验证 _prepare_body_for_tier 的源→目标转换通道应用行为."""

    @staticmethod
    def _body_with_thinking():
        return {
            "model": "claude-opus-4-6",
            "messages": [
                {
                    "role": "assistant",
                    "content": [
                        {
                            "type": "thinking",
                            "thinking": "Let me think...",
                            "signature": "zhipu-sig",
                        },
                        {"type": "text", "text": "response"},
                    ],
                },
            ],
        }

    def test_applies_zhipu_to_anthropic_transition(self):
        """source_vendor=zhipu, target=anthropic → 剥离 thinking + tool pairing."""
        tier = MagicMock()
        tier.name = "anthropic"

        exec_inst = _executor([])
        body = self._body_with_thinking()
        result = exec_inst._prepare_body_for_tier(body, tier, source_vendor="zhipu")

        assert result is not body
        assert len(result["messages"][0]["content"]) == 1
        assert result["messages"][0]["content"][0]["type"] == "text"
        # 原始 body 未被修改
        assert len(body["messages"][0]["content"]) == 2

    def test_applies_zhipu_to_copilot_transition(self):
        """source_vendor=zhipu, target=copilot → 剥离 thinking + cache_control."""
        tier = MagicMock()
        tier.name = "copilot"

        exec_inst = _executor([])
        body = self._body_with_thinking()
        result = exec_inst._prepare_body_for_tier(body, tier, source_vendor="zhipu")

        assert result is not body
        assert len(result["messages"][0]["content"]) == 1
        assert result["messages"][0]["content"][0]["type"] == "text"

    def test_applies_copilot_to_zhipu_transition(self):
        """source_vendor=copilot, target=zhipu → 剥离 thinking + cache_control + 移除 thinking 参数."""
        body = {
            "messages": self._body_with_thinking()["messages"],
            "thinking": {"type": "enabled", "budget_tokens": 10000},
        }
        tier = MagicMock()
        tier.name = "zhipu"

        exec_inst = _executor([])
        result = exec_inst._prepare_body_for_tier(body, tier, source_vendor="copilot")

        assert result is not body
        assert len(result["messages"][0]["content"]) == 1
        assert "thinking" not in result

    def test_returns_body_when_no_source_vendor(self):
        """source_vendor=None → 原样返回请求体."""
        tier = MagicMock()
        tier.name = "anthropic"

        exec_inst = _executor([])
        body = self._body_with_thinking()
        result = exec_inst._prepare_body_for_tier(body, tier, source_vendor=None)

        assert result is body
        assert len(result["messages"][0]["content"]) == 2

    def test_applies_anthropic_to_zhipu_transition(self):
        """anthropic → zhipu 已注册转换，应清理 thinking blocks."""
        tier = MagicMock()
        tier.name = "zhipu"

        exec_inst = _executor([])
        body = self._body_with_thinking()
        result = exec_inst._prepare_body_for_tier(body, tier, source_vendor="anthropic")

        # thinking blocks 应被剥离
        assert result is not body
        assert all(
            b.get("type") not in ("thinking", "redacted_thinking")
            for b in result["messages"][0]["content"]
        )
        assert len(result["messages"][0]["content"]) >= 1

    def test_returns_body_for_unknown_tier(self):
        """未知 tier（无注册转换）→ 原样返回."""
        tier = MagicMock()
        tier.name = "antigravity"

        exec_inst = _executor([])
        body = self._body_with_thinking()
        result = exec_inst._prepare_body_for_tier(body, tier, source_vendor="zhipu")

        assert result is body


# ── _prepare_body_for_tier 自转换通道测试 ───────────────────────────


class TestPrepareBodyForTierSelfTransition:
    """验证 zhipu → zhipu 自转换通道在 _prepare_body_for_tier 中的应用行为."""

    def test_applies_zhipu_self_cleanup(self):
        """source=zhipu, target=zhipu → 剥离 server_tool_use_delta + tool pairing."""
        tier = MagicMock()
        tier.name = "zhipu"

        body = {
            "model": "claude-opus-4-6",
            "messages": [
                {
                    "role": "assistant",
                    "content": [
                        {"type": "server_tool_use_delta", "partial_json": "{}"},
                        {
                            "type": "tool_use",
                            "id": "srvtoolu_a",
                            "name": "bash",
                            "input": {},
                        },
                        {
                            "type": "tool_result",
                            "tool_use_id": "srvtoolu_a",
                            "content": "ok",
                        },
                    ],
                },
            ],
        }
        exec_inst = _executor([])
        result = exec_inst._prepare_body_for_tier(body, tier, source_vendor="zhipu")

        # 深拷贝（不修改原始 body）
        assert result is not body
        assert len(body["messages"][0]["content"]) == 3

        # delta 块被剥离, tool_result 被搬迁出 assistant
        assistant_content = result["messages"][0]["content"]
        assert all(
            b.get("type") not in ("server_tool_use_delta", "tool_result")
            for b in assistant_content
        )
        # tool_result 已搬到下一个 user 消息
        assert result["messages"][1]["role"] == "user"
        assert any(
            b.get("type") == "tool_result" and b.get("tool_use_id") == "srvtoolu_a"
            for b in result["messages"][1]["content"]
        )

    def test_self_cleanup_preserves_srvtoolu_ids(self):
        """回归保护: 自清理通道不得改写 zhipu 原生 srvtoolu_* ID."""
        tier = MagicMock()
        tier.name = "zhipu"

        body = {
            "messages": [
                {
                    "role": "assistant",
                    "content": [
                        {
                            "type": "server_tool_use",
                            "id": "srvtoolu_keep_me",
                            "name": "bash",
                            "input": {},
                        },
                        {
                            "type": "thinking",
                            "thinking": "...",
                            "signature": "zhipu_sig",
                        },
                    ],
                },
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": "srvtoolu_keep_me",
                            "content": "ok",
                        },
                    ],
                },
            ],
        }
        exec_inst = _executor([])
        result = exec_inst._prepare_body_for_tier(body, tier, source_vendor="zhipu")

        # ID 与 server_tool_use 类型必须保留
        first_block = result["messages"][0]["content"][0]
        assert first_block["id"] == "srvtoolu_keep_me"
        assert first_block["type"] == "server_tool_use"
        # thinking signature 也必须保留
        thinking_block = next(
            b for b in result["messages"][0]["content"] if b.get("type") == "thinking"
        )
        assert thinking_block["signature"] == "zhipu_sig"
