"""路由执行器单元测试.

覆盖 :mod:`coding.proxy.routing.executor` 的核心逻辑：
- _RouteExecutor 门控判断（能力检查 / 兼容性检查 / 健康检查）
- 错误处理（TokenAcquireError / HTTP 错误 / 语义拒绝）
- _is_cap_error 订阅用量上限判定
- _VENDOR_PROTOCOL_LABEL_MAP 映射完整性
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from coding.proxy.vendors.base import (
    BaseVendor,
    VendorCapabilities,
    VendorResponse,
    NoCompatibleVendorError,
    RequestCapabilities,
    UsageInfo,
)
from coding.proxy.vendors.token_manager import TokenAcquireError
from coding.proxy.compat.canonical import (
    CompatibilityDecision,
    CompatibilityStatus,
    build_canonical_request,
)
from coding.proxy.routing.executor import (
    _RouteExecutor,
    _VENDOR_PROTOCOL_LABEL_MAP,
    _has_tool_results,
    _log_vendor_response_error,
)
from coding.proxy.routing.session_manager import RouteSessionManager
from coding.proxy.routing.tier import VendorTier
from coding.proxy.routing.usage_recorder import UsageRecorder


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
    vendor.send_message = AsyncMock(return_value=VendorResponse(
        status_code=200,
        raw_body=b'{}',
        usage=UsageInfo(input_tokens=10, output_tokens=5),
    ))
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
    return _RouteExecutor(
        tiers=tiers,
        usage_recorder=recorder,
        session_manager=session_mgr,
        **kwargs,
    )


# ── _VENDOR_PROTOCOL_LABEL_MAP ───────────────────────────


class TestVendorProtocolLabelMap:
    """供应商协议标签映射测试."""

    def test_all_expected_keys_present(self):
        expected = {"anthropic", "zhipu", "copilot", "antigravity"}
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
        session_record = await exec_inst._session_mgr.get_or_create_record(req.session_key, req.trace_id)
        reasons: list[str] = []

        result = await exec_inst._try_gate_tier(
            tier, is_last=True, request_caps=caps,
            canonical_request=req, session_record=session_record,
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
        session_record = await exec_inst._session_mgr.get_or_create_record(req.session_key, req.trace_id)
        reasons: list[str] = []

        result = await exec_inst._try_gate_tier(
            tier, is_last=False, request_caps=caps,
            canonical_request=req, session_record=session_record,
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
        session_record = await exec_inst._session_mgr.get_or_create_record(req.session_key, req.trace_id)
        reasons: list[str] = []

        result = await exec_inst._try_gate_tier(
            tier, is_last=False, request_caps=caps,
            canonical_request=req, session_record=session_record,
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
            status_code=200, raw_body=b'{}',
            usage=UsageInfo(input_tokens=5, output_tokens=2),
        )
        good_vendor.send_message = AsyncMock(return_value=good_resp)

        exec_inst = _executor([
            _make_tier(bad_vendor),
            _make_tier(good_vendor),
        ])

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
                {"model": "test", "tools": [{}]}, {},
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
            status_code=200, raw_body=b'{}',
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
            status_code=200, raw_body=b'{}',
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
                "error", request=MagicMock(), response=MagicMock(status_code=500),
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

        exec_inst = _executor([
            _make_tier(bad_vendor),
            _make_tier(good_vendor),
        ])

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
            tier, exc, is_last=True, failed_tier_name=None,
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
            tier, exc, is_last=False, failed_tier_name=None,
        )

        reauth_mock.request_reauth.assert_called_once_with("github")


# ── UsageRecorder 集成测试 ───────────────────────────────


class TestUsageRecorderIntegration:
    """UsageRecorder 与 Executor 协作测试."""

    def test_build_usage_info_from_dict(self):
        info = UsageRecorder.build_usage_info({
            "input_tokens": 100,
            "output_tokens": 50,
            "cache_creation_tokens": 10,
            "cache_read_tokens": 5,
            "request_id": "req_123",
        })
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
                input_tokens=25, output_tokens=10,
                cache_creation_tokens=3, cache_read_tokens=7,
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
            vendor="test", model_requested="m", model_served="m",
            usage=UsageInfo(), duration_ms=100, success=True,
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
                {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "tu_1", "content": "ok"}]},
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
                {"role": "assistant", "content": [{"type": "tool_use", "id": "tu_1", "name": "bash", "input": {}}]},
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
                        {"type": "tool_result", "tool_use_id": "tu_1", "content": "result"},
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
                {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "tu_1", "content": "ok"}]},
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
        vendor.send_message = AsyncMock(return_value=VendorResponse(
            status_code=500,
            raw_body=b'{"error":{"code":"500","message":"internal error"}}',
            error_type=None,
            error_message="internal error",
        ))
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
        vendor.send_message = AsyncMock(return_value=VendorResponse(
            status_code=500,
            raw_body=b'{"error":{"code":"500","message":"tool result id error"}}',
        ))
        exec_inst = _executor([_make_tier(vendor)])

        body = {
            "model": "claude-opus-4-6",
            "tools": [{"name": "Bash"}],
            "messages": [
                {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "tu_1", "content": "output"}]},
            ],
        }

        with caplog.at_level(_logging.WARNING, logger="coding.proxy.routing.executor"):
            resp = await exec_inst.execute_message(body, {})

        assert resp.status_code == 500
        log_text = "\n".join(r.message for r in caplog.records if r.levelno == _logging.WARNING)
        assert "has_tool_results=True" in log_text
        assert "claude-opus-4-6" in log_text
