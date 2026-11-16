"""Dashboard Overview「订阅状态」卡片：HTML 接线、XSS 防护与内联 JS 渲染行为（Node）."""

from __future__ import annotations

import shutil
import subprocess
import textwrap

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from coding.proxy.server.dashboard import register_dashboard_routes

# 与 test_dashboard_config_tab.py 同款 node 可用性守卫
_NODE = shutil.which("node")


@pytest.fixture
def client() -> TestClient:
    app = FastAPI()
    register_dashboard_routes(app)
    return TestClient(app, base_url="http://127.0.0.1:3392")


def test_subscription_card_wired(client: TestClient):
    html = client.get("/dashboard").text
    # 卡片位于 Overview 页签
    assert 'id="subscription-card"' in html
    assert 'id="subscription-list"' in html
    assert "订阅状态" in html
    # 数据链路：refreshOverview 并发拉取 + 独立兜底
    assert "fetchJSON('/api/dashboard/subscriptions').catch" in html
    assert "updateSubscriptions(subs);" in html
    # 三家订阅常量
    assert "SUB_META" in html
    assert "Claude Code 订阅" in html
    assert "GLM Coding Plan" in html
    assert "Antigravity" in html
    # 模板占位符已全部替换
    assert "{{" not in html


def test_subscription_card_escapes_upstream_strings(client: TestClient):
    """档位名 / 窗口 label / 错误消息均来自上游，渲染必须经 escapeHtml."""
    html = client.get("/dashboard").text
    assert "${escapeHtml(v.plan)}" in html
    assert "${escapeHtml(w.label || w.id || '')}" in html
    assert 'title="${escapeHtml(v.error)}"' in html
    assert "${escapeHtml(meta.label)}" in html


def test_subscription_card_no_inline_handlers(client: TestClient):
    """订阅卡片为纯展示面板，无 onclick 等内联事件处理器."""
    html = client.get("/dashboard").text
    start = html.index('id="subscription-card"')
    # 从卡片起点向后找边界（html.index("charts-grid") 会先命中前面的 CSS 规则，
    # 起点大于终点时切片为空串，断言恒真）
    card = html[start : html.index("charts-grid", start)]
    assert "onclick" not in card


# ── 内联 JS 渲染行为（Node vm 提取执行） ────────────────────


def _extract_function(html: str, signature: str) -> str:
    """从内联脚本按花括号配对截取单个 function 源码."""
    start = html.index(signature)
    i = html.index("{", start)
    depth = 0
    for j in range(i, len(html)):
        if html[j] == "{":
            depth += 1
        elif html[j] == "}":
            depth -= 1
            if depth == 0:
                return html[start : j + 1]
    raise AssertionError(f"unbalanced braces after {signature!r}")


_SUB_HARNESS = textwrap.dedent(
    r"""
    assert.strictEqual(typeof updateSubscriptions, 'function');
    assert.strictEqual(typeof renderSubWindow, 'function');
    assert.strictEqual(typeof fmtResetCountdown, 'function');

    // 重置倒计时人类可读化
    const soon = Math.floor(Date.now() / 1000) + 30 * 60;
    const later = Math.floor(Date.now() / 1000) + 30 * 3600;
    const past = Math.floor(Date.now() / 1000) - 60;
    assert.ok(fmtResetCountdown(soon).includes('分钟后重置'));
    assert.ok(fmtResetCountdown(later).includes('小时后重置'));
    assert.strictEqual(fmtResetCountdown(past), '即将重置');
    assert.strictEqual(fmtResetCountdown(null), '');
    assert.strictEqual(fmtResetCountdown(undefined), '');

    // 正常载荷：三家齐全，档位徽章 + 窗口进度条 + 重置文案
    updateSubscriptions({ vendors: [
      { key: 'claude', plan: null, status: 'waiting', windows: [] },
      { key: 'glm', plan: 'Max', status: 'ok',
        windows: [{ id: '5h', label: '5 小时', used_percent: 30, reset_epoch: soon }] },
      { key: 'antigravity', plan: 'Antigravity', status: 'ok',
        windows: [{ id: 'gemini-5h', label: 'Gemini Models 5h', used_percent: 4.9,
                    reset_epoch: soon }] },
    ] });
    let out = capturedHTML;
    assert.ok(out.includes('Claude Code 订阅'));
    assert.ok(out.includes('GLM Coding Plan'));
    assert.ok(out.includes('Antigravity'));
    assert.ok(out.includes('>Max</span>'));
    assert.ok(out.includes('5 小时 30%'));
    assert.ok(out.includes('Gemini Models 5h 5%'));
    assert.ok(out.includes('分钟后重置'));
    assert.ok(out.includes('等待首个请求（被动采集）'));

    // XSS：上游字符串经 escapeHtml，不得出现可执行标签
    updateSubscriptions({ vendors: [
      { key: 'glm', plan: '<img src=x onerror=alert(1)>', status: 'ok',
        windows: [{ id: '5h', label: '<script>', used_percent: 1, reset_epoch: null }],
      },
      { key: 'antigravity', plan: 'P', status: 'error',
        error: '" onmouseover="alert(2)', windows: [] },
    ] });
    out = capturedHTML;
    // 原始标签不得出现；onerror 仅允许以转义后的惰性文本存在
    assert.ok(!out.includes('<img'));
    assert.ok(!out.includes('<script>'));
    assert.ok(out.includes('&lt;img src=x onerror='));
    assert.ok(out.includes('&lt;script&gt;'));
    assert.ok(out.includes('查询失败'));

    // 状态分支与空数据
    updateSubscriptions({ vendors: [{ key: 'glm', status: 'not_configured', windows: [] }] });
    assert.ok(capturedHTML.includes('未启用'));
    updateSubscriptions({ vendors: [] });
    assert.ok(capturedHTML.includes('无订阅数据'));
    updateSubscriptions(null);
    assert.ok(capturedHTML.includes('无订阅数据'));
    console.log('subscription render harness OK');
    """
)


@pytest.mark.skipif(_NODE is None, reason="node 不可用")
def test_subscription_render_behavior(client: TestClient, tmp_path):
    """提取内联渲染函数在 Node vm 中执行，验证分支与 XSS 转义."""
    html = client.get("/dashboard").text
    script = html[html.index("<script>\n") :]
    sources = [
        _extract_function(script, sig)
        for sig in (
            "function escapeHtml(",
            "function quotaClass(",
            "function quotaBarColor(",
            "function fmtResetCountdown(",
            "function renderSubWindow(",
            "function updateSubscriptions(",
        )
    ]
    prelude = (
        "let capturedHTML = '';\n"
        "const _subEl = { set innerHTML(v) { capturedHTML = v; }, "
        "get innerHTML() { return capturedHTML; } };\n"
        "const document = { getElementById(id) { return id === 'subscription-list' "
        "? _subEl : null; } };\n"
        "const SUB_META = {\n"
        "  claude: { label: 'Claude Code 订阅', initial: 'C' },\n"
        "  glm: { label: 'GLM Coding Plan', initial: 'G' },\n"
        "  antigravity: { label: 'Antigravity', initial: 'A' },\n"
        "};\n"
    )
    harness = tmp_path / "sub_harness.js"
    harness.write_text(
        "const assert = require('assert');\n"
        + prelude
        + "\n".join(sources)
        + _SUB_HARNESS,
        encoding="utf-8",
    )
    result = subprocess.run(
        [_NODE, str(harness)], capture_output=True, text=True, timeout=30
    )
    assert result.returncode == 0, f"node harness failed:\n{result.stderr}"
    assert "subscription render harness OK" in result.stdout
