"""Level 2 E2E: AntigravityVendor 直接调用 — 验证 GLA 和 v1internal 协议端到端."""

from __future__ import annotations

import json

import pytest


def _print_diagnostics(vendor: object, label: str) -> None:
    diag = vendor.get_diagnostics()
    print(f"\n[E2E DIAG] {label}:")
    for k, v in diag.items():
        if isinstance(v, dict):
            print(f"  {k}: {json.dumps(v, ensure_ascii=False)[:200]}")
        else:
            print(f"  {k}: {v}")


def _is_quota_exhausted(resp: object) -> bool:
    """检查响应是否为配额耗尽（429 RESOURCE_EXHAUSTED）.

    429 表示协议对接正确但配额已用完，测试应标记为预期行为。
    """
    if resp.status_code != 429:
        return False
    error_msg = (resp.error_message or "").lower()
    return "resource" in error_msg or "quota" in error_msg or "exhausted" in error_msg


def _is_scope_error(resp: object) -> bool:
    """检查响应是否为 scope 不足错误."""
    if resp.status_code != 403:
        return False
    return "scope" in (resp.error_message or "").lower()


# ── 标准 GLA 模式 ──


@pytest.mark.e2e
@pytest.mark.asyncio
async def test_gla_non_streaming_text(
    antigravity_vendor: object,
    minimal_request_body: dict,
) -> None:
    """GLA 模式非流式请求 — 验证协议对接正确."""
    resp = await antigravity_vendor.send_message(minimal_request_body, {})
    _print_diagnostics(antigravity_vendor, "GLA non-streaming")

    # 403 scope 不足说明 GLA 端点不适用于当前凭证（正常，需要 v1internal）
    if _is_scope_error(resp):
        pytest.skip("GLA 端点 scope 不足，需要 v1internal 模式")

    # 429 配额耗尽 = 协议对接正确，仅配额问题
    if _is_quota_exhausted(resp):
        print("\n[E2E] GLA non-streaming: 协议对接正确，但配额已耗尽 (429)")
        return

    assert resp.status_code == 200, (
        f"预期 200，实际 {resp.status_code}: {resp.error_message}"
    )

    body = json.loads(resp.raw_body)
    assert body["type"] == "message", f"预期 type=message，实际: {body.get('type')}"
    assert body["role"] == "assistant"
    assert len(body["content"]) > 0, "content 为空"
    assert body["content"][0]["type"] == "text"
    assert body["stop_reason"] in ("end_turn", "max_tokens")
    assert body["usage"]["input_tokens"] > 0, "input_tokens 应 > 0"

    print(
        f"\n[E2E] GLA non-streaming 成功: model={body.get('model')}, "
        f"input={body['usage']['input_tokens']}, output={body['usage']['output_tokens']}, "
        f"stop_reason={body['stop_reason']}"
    )


@pytest.mark.e2e
@pytest.mark.asyncio
async def test_gla_streaming_text(
    antigravity_vendor: object,
    minimal_request_body: dict,
) -> None:
    """GLA 模式流式请求 — 验证 SSE 协议对接."""
    minimal_request_body["stream"] = True

    events: list[str] = []
    content_chunks: list[str] = []
    quota_exhausted = False

    try:
        async for chunk in antigravity_vendor.send_message_stream(
            minimal_request_body, {}
        ):
            text = chunk.decode("utf-8", errors="replace")
            for line in text.split("\n"):
                line = line.strip()
                if line.startswith("event:"):
                    events.append(line[6:].strip())
                elif line.startswith("data:"):
                    try:
                        data = json.loads(line[5:].strip())
                        if data.get("type") == "content_block_delta":
                            delta = data.get("delta", {})
                            if delta.get("type") == "text_delta":
                                content_chunks.append(delta.get("text", ""))
                    except json.JSONDecodeError:
                        pass
    except Exception as exc:
        error_str = str(exc).lower()
        if "403" in error_str and "scope" in error_str:
            pytest.skip("GLA 端点 scope 不足，需要 v1internal 模式")
        if "429" in error_str or "quota" in error_str or "exhausted" in error_str:
            quota_exhausted = True
            print("\n[E2E] GLA streaming: 协议对接正确，但配额已耗尽 (429)")
        else:
            raise

    if not quota_exhausted:
        _print_diagnostics(antigravity_vendor, "GLA streaming")
        assert "message_start" in events, (
            f"缺少 message_start 事件，实际事件: {events[:10]}"
        )
        assert "content_block_delta" in events, "缺少 content_block_delta 事件"
        assert "message_stop" in events, "缺少 message_stop 事件"

        full_text = "".join(content_chunks)
        print(
            f"\n[E2E] GLA streaming 成功: events={len(events)}, content='{full_text[:100]}'"
        )


@pytest.mark.e2e
@pytest.mark.asyncio
async def test_gla_with_system_prompt(
    antigravity_vendor: object,
    minimal_request_body: dict,
) -> None:
    """GLA 模式带 system prompt 的请求正常."""
    minimal_request_body["system"] = (
        "You are a test assistant. Always respond with exactly one word."
    )
    resp = await antigravity_vendor.send_message(minimal_request_body, {})

    if _is_scope_error(resp):
        pytest.skip("GLA 端点 scope 不足")
    if _is_quota_exhausted(resp):
        print("\n[E2E] GLA with system prompt: 协议对接正确，配额耗尽")
        return

    assert resp.status_code == 200, (
        f"预期 200，实际 {resp.status_code}: {resp.error_message}"
    )
    body = json.loads(resp.raw_body)
    assert body["type"] == "message"
    assert len(body["content"]) > 0

    print(
        f"\n[E2E] GLA with system prompt 成功: content='{body['content'][0].get('text', '')[:80]}'"
    )


@pytest.mark.e2e
@pytest.mark.asyncio
async def test_gla_with_tools(
    antigravity_vendor: object,
    minimal_request_body: dict,
) -> None:
    """GLA 模式带 tools 定义的请求正常往返."""
    minimal_request_body["tools"] = [
        {
            "name": "calculator",
            "description": "Performs arithmetic",
            "input_schema": {
                "type": "object",
                "properties": {"expression": {"type": "string"}},
                "required": ["expression"],
            },
        }
    ]
    minimal_request_body["messages"] = [
        {"role": "user", "content": "What is 2+2? Reply with just the number."}
    ]
    resp = await antigravity_vendor.send_message(minimal_request_body, {})

    if _is_scope_error(resp):
        pytest.skip("GLA 端点 scope 不足")
    if _is_quota_exhausted(resp):
        print("\n[E2E] GLA with tools: 协议对接正确，配额耗尽")
        return

    assert resp.status_code == 200, (
        f"预期 200，实际 {resp.status_code}: {resp.error_message}"
    )
    body = json.loads(resp.raw_body)
    assert body["type"] == "message"
    assert len(body["content"]) > 0

    _print_diagnostics(antigravity_vendor, "GLA with tools")
    print(
        f"\n[E2E] GLA with tools 成功: content_types={[b['type'] for b in body['content']]}"
    )


# ── v1internal 模式 ──


@pytest.mark.e2e
@pytest.mark.asyncio
async def test_v1internal_non_streaming(
    antigravity_vendor_v1internal: object,
    minimal_request_body: dict,
) -> None:
    """v1internal 模式非流式请求 — 验证协议对接."""
    resp = await antigravity_vendor_v1internal.send_message(minimal_request_body, {})

    _print_diagnostics(antigravity_vendor_v1internal, "v1internal non-streaming")

    # 429 = 协议对接正确，仅配额问题
    if _is_quota_exhausted(resp):
        diag = antigravity_vendor_v1internal.get_diagnostics()
        print(
            f"\n[E2E] v1internal non-streaming: 协议对接正确 (is_v1internal={diag.get('is_v1internal_mode')})，但配额已耗尽 (429)"
        )
        return

    assert resp.status_code == 200, (
        f"预期 200，实际 {resp.status_code}: {resp.error_message}"
    )
    body = json.loads(resp.raw_body)
    assert body["type"] == "message"
    assert body["role"] == "assistant"
    assert len(body["content"]) > 0

    diag = antigravity_vendor_v1internal.get_diagnostics()
    print(
        f"\n[E2E] v1internal non-streaming 成功: "
        f"is_v1internal={diag.get('is_v1internal_mode')}, "
        f"project_id_source={diag.get('project_id_source')}, "
        f"input={body['usage']['input_tokens']}, output={body['usage']['output_tokens']}"
    )


@pytest.mark.e2e
@pytest.mark.asyncio
async def test_v1internal_streaming(
    antigravity_vendor_v1internal: object,
    minimal_request_body: dict,
) -> None:
    """v1internal 模式流式请求 — 验证 SSE 协议."""
    minimal_request_body["stream"] = True

    events: list[str] = []
    content_chunks: list[str] = []
    quota_exhausted = False

    try:
        async for chunk in antigravity_vendor_v1internal.send_message_stream(
            minimal_request_body, {}
        ):
            text = chunk.decode("utf-8", errors="replace")
            for line in text.split("\n"):
                line = line.strip()
                if line.startswith("event:"):
                    events.append(line[6:].strip())
                elif line.startswith("data:"):
                    try:
                        data = json.loads(line[5:].strip())
                        if data.get("type") == "content_block_delta":
                            delta = data.get("delta", {})
                            if delta.get("type") == "text_delta":
                                content_chunks.append(delta.get("text", ""))
                    except json.JSONDecodeError:
                        pass
    except Exception as exc:
        error_str = str(exc)
        if "429" in error_str:
            quota_exhausted = True
            print("\n[E2E] v1internal streaming: 协议对接正确，但配额已耗尽 (429)")
        else:
            raise

    if not quota_exhausted:
        _print_diagnostics(antigravity_vendor_v1internal, "v1internal streaming")
        assert "message_start" in events, "缺少 message_start"
        assert "content_block_delta" in events, "缺少 content_block_delta"
        assert "message_stop" in events, "缺少 message_stop"

        full_text = "".join(content_chunks)
        print(
            f"\n[E2E] v1internal streaming 成功: events={len(events)}, content='{full_text[:100]}'"
        )


@pytest.mark.e2e
@pytest.mark.asyncio
async def test_project_id_auto_discovery(
    antigravity_vendor_v1internal: object,
    minimal_request_body: dict,
) -> None:
    """首次请求后 v1internal 模式状态和 project_id 发现结果."""
    resp = await antigravity_vendor_v1internal.send_message(minimal_request_body, {})

    diag = antigravity_vendor_v1internal.get_diagnostics()
    source = diag.get("project_id_source", "unknown")
    is_v1 = diag.get("is_v1internal_mode", False)

    print(f"\n[E2E] project_id discovery: source={source}, is_v1internal={is_v1}")

    # v1internal 模式应已启用（由 base_url 配置驱动）
    assert is_v1 is True, "v1internal 模式应已启用"
    assert source in ("discovered", "none", "configured"), (
        f"未知的 project_id_source: {source}"
    )

    # 请求应到达了 API 端点（429 配额耗尽或 200 成功都说明协议对接正确）
    assert resp.status_code in (200, 429), (
        f"预期 200/429，实际 {resp.status_code}: {resp.error_message[:200]}"
    )

    if resp.status_code == 429:
        print("  配额已耗尽 (429)，但协议对接验证正确")
    elif source == "discovered":
        print(f"  discovered_project_id={diag.get('discovered_project_id')}")
    elif source == "none":
        print("  未发现 project_id，v1internal 无需 project_id")
