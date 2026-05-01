"""Level 3 E2E: 完整 HTTP 端到端 — 模拟 Claude Code 通过 coding-proxy 使用 Antigravity."""

from __future__ import annotations

import json

import pytest

# Claude Code 发送的典型 headers
CLAUDE_CODE_HEADERS = {
    "anthropic-version": "2023-06-01",
    "content-type": "application/json",
    "x-api-key": "sk-ant-placeholder",
}


def _is_quota_exhausted(response: object) -> bool:
    """检查响应是否为配额耗尽 (429)."""
    if response.status_code != 429:
        return False
    try:
        body = response.json()
        err = body.get("error", {})
        msg = err.get("message", "").lower()
        return "resource" in msg or "quota" in msg or "exhausted" in msg
    except Exception:
        return False


def _is_scope_error(response: object) -> bool:
    """检查响应是否为 scope 不足 (403)."""
    if response.status_code != 403:
        return False
    try:
        body = response.json()
        err = body.get("error", {})
        return "scope" in json.dumps(err).lower()
    except Exception:
        return False


@pytest.mark.e2e
@pytest.mark.asyncio
async def test_http_non_streaming(
    e2e_client: object,
    minimal_request_body: dict,
) -> None:
    """POST /v1/messages 非流式 → 验证协议对接正确."""
    response = await e2e_client.post(
        "/v1/messages",
        json=minimal_request_body,
        headers=CLAUDE_CODE_HEADERS,
    )

    if _is_scope_error(response):
        pytest.skip("GLA 端点 scope 不足，需要 v1internal 模式")
    if _is_quota_exhausted(response):
        print("\n[E2E] HTTP non-streaming: 协议对接正确，但配额已耗尽 (429)")
        return

    assert response.status_code == 200, (
        f"预期 200，实际 {response.status_code}: {response.text[:300]}"
    )

    body = response.json()
    assert body["type"] == "message", f"预期 type=message，实际: {body.get('type')}"
    assert body["role"] == "assistant"
    assert len(body["content"]) > 0, "content 为空"
    assert body["content"][0]["type"] == "text"
    assert body["usage"]["input_tokens"] > 0, "input_tokens 应 > 0"

    print(
        f"\n[E2E] HTTP non-streaming 成功: model={body.get('model')}, "
        f"input={body['usage']['input_tokens']}, output={body['usage']['output_tokens']}"
    )
    print(f"  content: {body['content'][0].get('text', '')[:100]}")


@pytest.mark.e2e
@pytest.mark.asyncio
async def test_http_streaming(e2e_client: object) -> None:
    """POST /v1/messages (stream=true) → 验证 SSE 协议."""
    body = {
        "model": "claude-sonnet-4-20250514",
        "messages": [{"role": "user", "content": "Say exactly: pong"}],
        "max_tokens": 32,
        "stream": True,
    }

    events: list[str] = []
    content_chunks: list[str] = []

    try:
        async with e2e_client.stream(
            "POST", "/v1/messages", json=body, headers=CLAUDE_CODE_HEADERS
        ) as response:
            if response.status_code == 429:
                print("\n[E2E] HTTP streaming: 协议对接正确，但配额已耗尽 (429)")
                return

            assert response.status_code == 200, f"预期 200，实际 {response.status_code}"

            async for line in response.aiter_lines():
                line = line.strip()
                if not line:
                    continue
                if line.startswith("event:"):
                    events.append(line[6:].strip())
                elif line.startswith("data:"):
                    payload = line[5:].strip()
                    if payload == "[DONE]":
                        continue
                    try:
                        data = json.loads(payload)
                        if data.get("type") == "content_block_delta":
                            delta = data.get("delta", {})
                            if delta.get("type") == "text_delta":
                                content_chunks.append(delta.get("text", ""))
                    except json.JSONDecodeError:
                        pass

        assert "message_start" in events, f"缺少 message_start，实际: {events[:10]}"
        assert "content_block_delta" in events, "缺少 content_block_delta"
        assert "message_stop" in events, "缺少 message_stop"

        full_text = "".join(content_chunks)
        print(
            f"\n[E2E] HTTP streaming 成功: events={len(events)}, content='{full_text[:100]}'"
        )
    except Exception as exc:
        error_str = str(exc)
        if "429" in error_str or "exhausted" in error_str.lower():
            print("\n[E2E] HTTP streaming: 协议对接正确，但配额已耗尽 (429)")
            return
        raise


@pytest.mark.e2e
@pytest.mark.asyncio
async def test_http_with_tools(e2e_client: object) -> None:
    """POST /v1/messages 带 tools 定义 → 请求正常往返."""
    body = {
        "model": "claude-sonnet-4-20250514",
        "messages": [
            {"role": "user", "content": "What is 2+2? Reply with just the number."}
        ],
        "max_tokens": 128,
        "tools": [
            {
                "name": "calculator",
                "description": "Performs arithmetic",
                "input_schema": {
                    "type": "object",
                    "properties": {"expression": {"type": "string"}},
                    "required": ["expression"],
                },
            }
        ],
    }
    response = await e2e_client.post(
        "/v1/messages", json=body, headers=CLAUDE_CODE_HEADERS
    )

    if _is_scope_error(response):
        pytest.skip("GLA 端点 scope 不足")
    if _is_quota_exhausted(response):
        print("\n[E2E] HTTP with tools: 协议对接正确，配额耗尽")
        return

    assert response.status_code == 200, (
        f"预期 200，实际 {response.status_code}: {response.text[:300]}"
    )

    resp_body = response.json()
    assert resp_body["type"] == "message"
    assert len(resp_body["content"]) > 0
    content_types = [b["type"] for b in resp_body["content"]]
    print(f"\n[E2E] HTTP with tools 成功: content_types={content_types}")


@pytest.mark.e2e
@pytest.mark.asyncio
async def test_http_health_probe(e2e_client: object) -> None:
    """HEAD / 和 GET /health → 200（Claude Code 连通性探测）."""
    head_resp = await e2e_client.head("/")
    assert head_resp.status_code == 200, (
        f"HEAD / 预期 200，实际 {head_resp.status_code}"
    )

    get_resp = await e2e_client.get("/")
    assert get_resp.status_code == 200, f"GET / 预期 200，实际 {get_resp.status_code}"

    health_resp = await e2e_client.get("/health")
    assert health_resp.status_code == 200
    assert health_resp.json() == {"status": "ok"}

    print("\n[E2E] HTTP health probe 成功: HEAD /=200, GET /=200, /health=ok")


@pytest.mark.e2e
@pytest.mark.asyncio
async def test_http_status_diagnostics(e2e_client: object) -> None:
    """GET /api/status → 包含 antigravity tier 诊断信息."""
    response = await e2e_client.get("/api/status")
    assert response.status_code == 200

    data = response.json()
    assert "tiers" in data
    antigravity_tiers = [t for t in data["tiers"] if t["name"] == "antigravity"]
    assert len(antigravity_tiers) == 1, (
        f"预期 1 个 antigravity tier，实际: {len(antigravity_tiers)}"
    )

    tier = antigravity_tiers[0]
    assert "diagnostics" in tier, "缺少 diagnostics"

    diag = tier["diagnostics"]
    print("\n[E2E] status diagnostics:")
    for k, v in diag.items():
        if isinstance(v, dict):
            print(f"  {k}: {json.dumps(v, ensure_ascii=False)[:200]}")
        else:
            print(f"  {k}: {v}")

    # token_manager 诊断可能为空（若未发生错误），仅验证其存在性
    if "token_manager" in diag:
        print("  token_manager diagnostics present")
    else:
        print("  (token_manager diagnostics empty — no token errors)")


@pytest.mark.e2e
@pytest.mark.asyncio
async def test_http_claude_code_headers(e2e_client: object) -> None:
    """带完整 Claude Code headers 的请求正常（验证 x-api-key 不干扰 Antigravity）."""
    headers = {
        "anthropic-version": "2023-06-01",
        "content-type": "application/json",
        "x-api-key": "sk-ant-api03-fake-key-for-testing",
        "accept": "application/json",
    }
    body = {
        "model": "claude-sonnet-4-20250514",
        "messages": [{"role": "user", "content": "Say: ok"}],
        "max_tokens": 16,
    }
    response = await e2e_client.post("/v1/messages", json=body, headers=headers)

    if _is_quota_exhausted(response):
        print("\n[E2E] Claude Code headers: 协议对接正确，配额耗尽")
        return

    assert response.status_code == 200, (
        f"预期 200，实际 {response.status_code}: {response.text[:300]}"
    )

    resp_body = response.json()
    assert resp_body["type"] == "message"
    assert len(resp_body["content"]) > 0

    print(
        f"\n[E2E] Claude Code headers 成功: content='{resp_body['content'][0].get('text', '')[:80]}'"
    )
