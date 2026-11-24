"""Dashboard Overview「订阅状态」卡片（订阅档位 / 用量窗口 × 路由 tier 运行状态）：HTML 接线、XSS 防护与内联 JS 渲染行为（Node）."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import textwrap
from typing import get_args

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from coding.proxy.config.routing import VendorType
from coding.proxy.server.dashboard import register_dashboard_routes

# 与 test_dashboard_config_tab.py 同款 node 可用性守卫
_NODE = shutil.which("node")


@pytest.fixture
def client() -> TestClient:
    app = FastAPI()
    register_dashboard_routes(app)
    return TestClient(app, base_url="http://127.0.0.1:3392")


def _inline_script(html: str) -> str:
    return html[html.index("<script>\n") :]


def _card(html: str) -> str:
    """截取合并卡片的 HTML 片段（起点为卡片 id，终点为紧随其后的折线图卡片）."""
    start = html.index('id="subscription-card"')
    return html[start : html.index('class="card timeline-card"', start)]


def test_subscription_card_wired(client: TestClient):
    html = client.get("/dashboard").text
    card = _card(html)
    # 一张卡片承接两类状态：带 tier 的行（可拖拽）在前，无 tier 的订阅行在后
    assert '<div class="card-title">🎫 订阅状态' in card
    assert 'id="btn-vendor-reset"' in card
    assert 'id="vendor-list"' in card
    assert card.index('id="vendor-list"') < card.index('id="subscription-list"')
    # 原「供应商状态」卡片不再独立存在，折线图脱离网格变全宽
    assert '<div class="card-title">供应商状态' not in html
    assert 'class="charts-grid"' not in html
    assert 'class="card timeline-card"' in html
    # 数据链路：refreshOverview 并发拉取 + 独立兜底
    assert "fetchJSON('/api/dashboard/subscriptions').catch" in html
    assert "updateSubscriptions(subs);" in html
    # 三家订阅常量
    assert "SUB_META" in html
    for label in ("Claude Code 订阅", "GLM Coding Plan", "Antigravity"):
        assert label in html
    # 模板占位符已全部替换
    assert "{{" not in html


def test_subscription_card_escapes_upstream_strings(client: TestClient):
    """档位名 / 窗口 label / 错误消息 / tier 名均来自载荷，渲染必须经 escapeHtml."""
    html = client.get("/dashboard").text
    assert "${escapeHtml(v.plan)}" in html
    assert "${escapeHtml(w.label || w.id || '')}" in html
    assert 'title="${escapeHtml(v.error)}"' in html
    assert "${escapeHtml(meta.label)}" in html
    assert '<span class="vendor-name">${safeName}</span>' in html
    assert 'data-vendor="${safeName}"' in html


def test_subscription_card_inline_handler_limited_to_reset_button(client: TestClient):
    """卡片内唯一的内联事件处理器是标题栏的「状态复位」按钮，行模板为纯展示.

    复位按钮必须是静态节点：resetVendorStatus 在 1.5s 后用捕获的 btn 还原文案，
    不能随列表的 innerHTML 重绘一起生成。
    """
    html = client.get("/dashboard").text
    card = _card(html)
    assert card.count("onclick=") == 1
    assert 'onclick="resetVendorStatus(this)"' in card
    assert card.index("onclick=") < card.index('id="vendor-list"')
    script = _inline_script(html)
    for signature in (
        "function _renderStatusRows(",
        "function _subZoneHTML(",
        "function _tierZoneHTML(",
    ):
        assert not re.search(r"\son[a-z]+=\"", _extract_function(script, signature))


@pytest.mark.skipif(_NODE is None, reason="node 不可用")
def test_subscription_meta_maps_each_account_to_its_tier(client: TestClient):
    """SUB_META.tier 是订阅行与路由 tier 的归并键，取值必须是后端 VendorType 之一."""
    script = _inline_script(client.get("/dashboard").text)
    snippet = _extract_block(script, "const SUB_META = {", "};")
    result = subprocess.run(
        [_NODE, "-e", snippet + "\nconsole.log(JSON.stringify(SUB_META));"],
        capture_output=True,
        text=True,
        timeout=30,
        check=True,
    )
    meta = json.loads(result.stdout)
    assert {key: value["tier"] for key, value in meta.items()} == {
        "claude": "anthropic",
        "glm": "zhipu",
        "antigravity": "antigravity",
    }
    vendor_types = set(get_args(VendorType))
    assert all(value["tier"] in vendor_types for value in meta.values())


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


def _extract_block(script: str, start_marker: str, end_marker: str) -> str:
    start = script.index(start_marker)
    return script[start : script.index(end_marker, start) + len(end_marker)]


_SUB_HARNESS = textwrap.dedent(
    r"""
    assert.strictEqual(typeof updateSubscriptions, 'function');
    assert.strictEqual(typeof updateVendorStatus, 'function');
    assert.strictEqual(typeof renderSubWindow, 'function');
    assert.strictEqual(typeof fmtResetCountdown, 'function');

    function tierHTML() { return captured['vendor-list']; }
    function subHTML() { return captured['subscription-list']; }
    function rowCount(html) { return (html.match(/class="vendor-item/g) || []).length; }

    // 重置倒计时人类可读化
    const soon = Math.floor(Date.now() / 1000) + 30 * 60;
    const later = Math.floor(Date.now() / 1000) + 30 * 3600;
    const past = Math.floor(Date.now() / 1000) - 60;
    assert.ok(fmtResetCountdown(soon).includes('分钟后重置'));
    assert.ok(fmtResetCountdown(later).includes('小时后重置'));
    assert.strictEqual(fmtResetCountdown(past), '即将重置');
    assert.strictEqual(fmtResetCountdown(null), '');
    assert.strictEqual(fmtResetCountdown(undefined), '');

    const tier = (name, extra) => Object.assign(
      { name: name, circuit_breaker: { state: 'closed', failure_count: 0 },
        rate_limit: { is_rate_limited: false } }, extra || {});
    const quota = (hours, pct) => ({ state: 'within_quota', usage_percent: pct, window_hours: hours });
    const screenshotTiers = [tier('zhipu', { quota_guard: quota(24, 0) }), tier('anthropic')];
    const screenshotVendors = [
      { key: 'claude', plan: null, status: 'waiting', windows: [] },
      { key: 'glm', plan: 'Max', status: 'ok',
        windows: [{ id: '5h', label: '5 小时', used_percent: 30, reset_epoch: soon }] },
      { key: 'antigravity', plan: 'Antigravity', status: 'ok',
        windows: [
          { id: 'gemini-weekly', label: 'Gemini Models weekly', used_percent: 0, reset_epoch: later },
          { id: 'gemini-5h', label: 'Gemini Models 5h', used_percent: 4.9, reset_epoch: soon },
          { id: 'gpt-weekly', label: 'Claude and GPT models weekly', used_percent: 0, reset_epoch: later },
          { id: 'gpt-5h', label: 'Claude and GPT models 5h', used_percent: 0, reset_epoch: soon },
        ] },
    ];

    // 同账号合并为一行：[标识] [订阅区] │ [路由区]，行序 = tier 优先级
    updateVendorStatus({ tiers: screenshotTiers });
    updateSubscriptions({ vendors: screenshotVendors });
    let rows = tierHTML();
    assert.strictEqual(rowCount(rows), 2);
    assert.ok(rows.indexOf('data-vendor="zhipu"') < rows.indexOf('data-vendor="anthropic"'));
    const glmRow = rows.slice(0, rows.indexOf('data-vendor="anthropic"'));
    assert.ok(glmRow.includes('GLM Coding Plan'));                           // 行名取订阅名
    assert.ok(glmRow.includes('<span class="tier-tag">zhipu</span>'));        // 路由区标注 tier 名
    assert.ok(glmRow.includes('>Max</span>') && glmRow.includes('5 小时 30%'));
    assert.ok(glmRow.includes('分钟后重置'));
    assert.ok(glmRow.includes('>正常</span>') && glmRow.includes('1d配额 0%'));
    assert.ok(glmRow.indexOf('class="vendor-badges sub-zone"')
      < glmRow.indexOf('class="vendor-badges tier-zone"'));
    assert.strictEqual((glmRow.match(/class="drag-handle"/g) || []).length, 1);
    const claudeRow = rows.slice(rows.indexOf('data-vendor="anthropic"'));
    assert.ok(claudeRow.includes('Claude Code 订阅'));
    assert.ok(claudeRow.includes('<span class="tier-tag">anthropic</span>'));
    assert.ok(claudeRow.includes('等待首个请求（被动采集）'));
    assert.ok(!rows.includes('无订阅数据'));

    // 无 tier 的订阅行排在其后的独立容器，不可拖拽
    assert.strictEqual(rowCount(subHTML()), 1);
    assert.ok(subHTML().includes('Antigravity') && subHTML().includes('Gemini Models 5h 5%'));
    assert.ok(!subHTML().includes('data-vendor') && !subHTML().includes('drag-handle'));
    assert.ok(subHTML().includes('未加入路由链'));

    // 无订阅对应的 tier 行：保留路由区并加「供应商状态」标注，不出订阅区
    updateVendorStatus({ tiers: [tier('zhipu'),
      tier('copilot', { weekly_quota_guard: quota(168, 12) }), tier('anthropic')] });
    rows = tierHTML();
    assert.strictEqual(rowCount(rows), 3);
    assert.ok(rows.indexOf('data-vendor="zhipu"') < rows.indexOf('data-vendor="copilot"'));
    const copilotRow = rows.slice(rows.indexOf('data-vendor="copilot"'), rows.indexOf('data-vendor="anthropic"'));
    assert.ok(copilotRow.includes('<span class="vendor-name">copilot</span>'));
    assert.ok(copilotRow.includes('<span class="tier-tag">供应商状态</span>'));
    assert.ok(copilotRow.includes('7d配额 12%'));
    assert.ok(!copilotRow.includes('sub-zone'));

    // 行序跟随运行时优先级（拖拽后 tiers 顺序变化）
    updateVendorStatus({ tiers: [tier('anthropic'), tier('zhipu')] });
    rows = tierHTML();
    assert.ok(rows.indexOf('data-vendor="anthropic"') < rows.indexOf('data-vendor="zhipu"'));

    // 路由区徽章分支：熔断 ×N / 半开 / 周配额 / 限速中
    updateVendorStatus({ tiers: [
      tier('zhipu', { circuit_breaker: { state: 'open', failure_count: 3 },
        rate_limit: { is_rate_limited: true }, weekly_quota_guard: quota(168, 45) }),
      tier('anthropic', { circuit_breaker: { state: 'half_open', failure_count: 0 } }),
    ] });
    rows = tierHTML();
    assert.ok(rows.includes('<span class="status-badge sb-err">熔断 ×3</span>'));
    assert.ok(rows.includes('<span class="status-badge sb-warn">半开</span>'));
    assert.ok(rows.includes('<span class="status-badge sb-warn">限速中</span>'));
    assert.ok(rows.includes('7d配额 45%'));

    // 订阅接口失败或为空：行名不跳变、订阅区不渲染，卡片底部提示
    updateVendorStatus({ tiers: screenshotTiers });
    for (const payload of [{ vendors: [] }, null, { vendors: 'bad' }]) {
      updateSubscriptions(payload);
      rows = tierHTML();
      assert.ok(rows.includes('GLM Coding Plan') && rows.includes('Claude Code 订阅'));
      assert.ok(!rows.includes('<span class="vendor-name">zhipu</span>'));
      assert.ok(!rows.includes('sub-zone'));
      assert.ok(subHTML().includes('无订阅数据'));
    }

    // 状态分支与 XSS：上游字符串经 escapeHtml，不得出现可执行标签
    updateSubscriptions({ vendors: [
      { key: 'glm', plan: '<img src=x onerror=alert(1)>', status: 'ok',
        windows: [{ id: '5h', label: '<script>', used_percent: 1, reset_epoch: null }] },
      { key: 'antigravity', plan: 'P', status: 'error',
        error: '" onmouseover="alert(2)', windows: [] },
      { key: 'claude', status: 'not_configured', windows: [] },
    ] });
    let out = tierHTML() + subHTML();
    // 原始标签不得出现；onerror 仅允许以转义后的惰性文本存在
    assert.ok(!out.includes('<img'));
    assert.ok(!out.includes('<script>'));
    assert.ok(out.includes('&lt;img src=x onerror='));
    assert.ok(out.includes('&lt;script&gt;'));
    assert.ok(!out.includes('" onmouseover="alert(2)'));
    assert.ok(out.includes('查询失败'));
    assert.ok(out.includes('未启用'));

    // tier 名同样来自载荷：名称 / data-vendor / 首字母三处均转义
    updateVendorStatus({ tiers: [tier('<img src=x onerror=alert(3)>'), tier('anthropic')] });
    out = tierHTML();
    assert.ok(!out.includes('<img'));
    assert.ok(out.includes('&lt;img src=x onerror=alert(3)&gt;'));
    assert.ok(out.includes('<div class="vendor-avatar">&lt;</div>'));
    assert.strictEqual((out.match(/data-vendor="/g) || []).length, 2);
    updateVendorStatus({ tiers: screenshotTiers });

    // 仅带 tier 数据的定向重渲染（拖拽回滚 / 复位后刷新）复用订阅缓存，订阅徽章不被清空
    updateSubscriptions({ vendors: screenshotVendors });
    updateVendorStatus({ tiers: [tier('anthropic'), tier('zhipu')] });
    rows = tierHTML();
    assert.ok(rows.includes('>Max</span>') && rows.includes('5 小时 30%'));
    assert.ok(rows.indexOf('data-vendor="anthropic"') < rows.indexOf('data-vendor="zhipu"'));

    // 拖拽进行中 / PUT 未决：两路刷新只更新缓存、不动 DOM，守卫解除后按最新缓存补绘
    const frozen = tierHTML() + subHTML();
    const draggingTiers = [tier('zhipu'), tier('copilot'), tier('anthropic')];
    _tierDrag.active = true;
    updateVendorStatus({ tiers: draggingTiers });
    updateSubscriptions({ vendors: [{ key: 'glm', plan: 'Pro', status: 'ok', windows: [] }] });
    assert.strictEqual(tierHTML() + subHTML(), frozen);
    _tierDrag.active = false;
    _tierDrag.inFlight = true;
    updateVendorStatus({ tiers: screenshotTiers });
    assert.strictEqual(tierHTML() + subHTML(), frozen);
    _tierDrag.inFlight = false;
    updateVendorStatus({ tiers: draggingTiers });
    rows = tierHTML();
    assert.ok(rows.includes('<span class="vendor-name">copilot</span>') && rows.includes('>Pro</span>'));

    // antigravity tier 在路由链上时并入同一行；显示名与 tier 名仅大小写不同，不重复标注。
    // 此时 anthropic 不在路由链上，Claude 订阅转为无 tier 的订阅行
    updateVendorStatus({ tiers: [tier('zhipu'), tier('antigravity')] });
    updateSubscriptions({ vendors: screenshotVendors });
    assert.ok(subHTML().includes('Claude Code 订阅'));
    assert.ok(!subHTML().includes('Antigravity'));
    const agRow = tierHTML().slice(tierHTML().indexOf('data-vendor="antigravity"'));
    assert.ok(agRow.includes('<span class="vendor-name">Antigravity</span>'));
    assert.ok(!agRow.includes('tier-tag'));

    // 后端新增的未知订阅 key：落为无 tier 的订阅行，不误并
    updateSubscriptions({ vendors: screenshotVendors.concat([{ key: 'newvendor', status: 'ok', windows: [] }]) });
    assert.ok(subHTML().includes('<span class="vendor-name">newvendor</span>'));

    // 只有 1 个 tier 不渲染手柄；两侧都空只给一个空态
    updateVendorStatus({ tiers: [tier('zhipu')] });
    assert.ok(!tierHTML().includes('drag-handle'));
    updateVendorStatus({ tiers: [] });
    updateSubscriptions(null);
    assert.ok(tierHTML().includes('无订阅数据'));
    assert.strictEqual(subHTML(), '');
    console.log('subscription render harness OK');
    """
)


_ROW_FUNCTION_SIGNATURES = (
    "function escapeHtml(",
    "function quotaClass(",
    "function quotaBarColor(",
    "function quotaWindowLabel(",
    "function fmtResetCountdown(",
    "function renderSubWindow(",
    "function renderQuotaBar(",
    "function cbStateClass(",
    "function cbStateLabel(",
    "function _tierZoneHTML(",
    "function _subZoneHTML(",
    "function updateVendorStatus(",
    "function updateSubscriptions(",
    "function _renderStatusRows(",
)


def _row_sources(script: str) -> list[str]:
    """合并卡渲染链路的全部函数源码（取页面内联脚本，不在测试里另抄）."""
    return [
        _extract_function(script, signature) for signature in _ROW_FUNCTION_SIGNATURES
    ]


def _meta_block(script: str) -> str:
    """订阅元信息与 tier 映射（SUB_META / SUB_BY_TIER）取页面源码."""
    start = script.index("const SUB_META = {")
    return script[start : script.index("function fmtResetCountdown(", start)]


def _run_node(tmp_path, name: str, source: str, ok_marker: str) -> None:
    harness = tmp_path / name
    harness.write_text(source, encoding="utf-8")
    result = subprocess.run(
        [_NODE, str(harness)], capture_output=True, text=True, timeout=30
    )
    assert result.returncode == 0, f"node harness failed:\n{result.stderr}"
    assert ok_marker in result.stdout


@pytest.mark.skipif(_NODE is None, reason="node 不可用")
def test_subscription_render_behavior(client: TestClient, tmp_path):
    """提取内联渲染函数在 Node vm 中执行，验证行模型、分支、并发守卫与 XSS 转义."""
    script = _inline_script(client.get("/dashboard").text)
    prelude = (
        "let captured = { 'vendor-list': '', 'subscription-list': '' };\n"
        "const document = { getElementById(id) {\n"
        "  return id in captured ? { set innerHTML(v) { captured[id] = v; },\n"
        "  get innerHTML() { return captured[id]; } } : null; } };\n"
        "const _tierDrag = { active: false, inFlight: false };\n"
        "let _tierStatus = { tiers: [] };\n"
        "let _subData = { vendors: [] };\n" + _meta_block(script)
    )
    _run_node(
        tmp_path,
        "sub_harness.js",
        "const assert = require('assert');\n"
        + prelude
        + "\n".join(_row_sources(script))
        + _SUB_HARNESS,
        "subscription render harness OK",
    )


# 最小假 DOM：只实现拖拽状态机与合并渲染用到的表面（子节点重排 / 事件监听 / 指针捕获 / 按序取行）。
_DRAG_PRELUDE = textwrap.dedent(
    r"""
    class FakeEl {
      constructor(kind, vendor) {
        this.kind = kind; this.dataset = vendor ? { vendor: vendor } : {};
        this.children = []; this.parentElement = null; this._h = {}; this.captured = null;
        const set = new Set();
        this.classList = { add: c => set.add(c), remove: c => set.delete(c), contains: c => set.has(c) };
      }
      addEventListener(type, fn) { (this._h[type] = this._h[type] || []).push(fn); }
      fire(type, ev) { (this._h[type] || []).forEach(fn => fn(Object.assign({ preventDefault() {}, button: 0 }, ev))); }
      setPointerCapture(id) { this.captured = id; }
      releasePointerCapture() { this.captured = null; }
      getBoundingClientRect() {
        const i = this.parentElement ? this.parentElement.children.indexOf(this) : 0;
        return { top: i * 56, height: 56 };
      }
      querySelectorAll(sel) {
        const items = this.children.filter(c => c.kind === 'item');
        return sel.includes('[data-vendor]') ? items.filter(c => c.dataset.vendor) : items;
      }
      _detach(node) {
        const p = node.parentElement;
        if (p) p.children.splice(p.children.indexOf(node), 1);
      }
      insertBefore(node, ref) {
        this._detach(node);
        const i = ref ? this.children.indexOf(ref) : this.children.length;
        this.children.splice(i, 0, node); node.parentElement = this;
      }
      appendChild(node) { this._detach(node); this.children.push(node); node.parentElement = this; }
      get lastElementChild() { return this.children[this.children.length - 1] || null; }
      get nextElementSibling() {
        const p = this.parentElement;
        return p ? (p.children[p.children.indexOf(this) + 1] || null) : null;
      }
      closest() { return this; }
      // 合并渲染写回 innerHTML：按 data-vendor 出现顺序重建行节点
      set innerHTML(html) {
        this.html = html;
        this.children = Array.from(
          html.matchAll(/data-vendor="([^"]*)"/g),
          m => new FakeEl('item', m[1])
        );
        this.children.forEach(c => { c.parentElement = this; });
      }
      get innerHTML() { return this.html || ''; }
    }
    const tierList = new FakeEl('list');
    const subList = new FakeEl('list');
    const docHandlers = {};
    const document = {
      getElementById(id) { return id === 'vendor-list' ? tierList : id === 'subscription-list' ? subList : null; },
      addEventListener(type, fn) { (docHandlers[type] = docHandlers[type] || []).push(fn); },
      // 指针在列表外松开：收尾事件只到达 document，不经过 #vendor-list
      releaseOutside(pointerId) { (docHandlers.pointerup || []).forEach(fn => fn({ pointerId: pointerId, preventDefault() {} })); },
    };
    const handleOf = (vendor) => {
      const item = tierList.querySelectorAll('[data-vendor]').find(el => el.dataset.vendor === vendor);
      const handle = new FakeEl('handle');
      handle.closest = sel => (sel === '.vendor-item' ? item : handle);
      return handle;
    };
    const domOrder = () => _tierCurrentOrder(tierList);
    const tick = () => new Promise(resolve => setTimeout(resolve, 0));
    const server = { order: ['zhipu', 'anthropic', 'copilot'], cb: {} };
    const serverStatus = () => ({ tiers: server.order.map(name => ({
      name: name,
      circuit_breaker: { state: server.cb[name] || 'closed', failure_count: server.cb[name] ? 2 : 0 },
      rate_limit: { is_rate_limited: false } })) });
    const puts = [];
    const pending = { gate: null, fail: false };
    let refreshFails = false;
    function fetch(url, opts) {
      if (url !== '/api/tier-order') throw new Error('unexpected fetch: ' + url);
      const names = JSON.parse(opts.body).vendors;
      puts.push(names);
      return new Promise(resolve => {
        const settle = () => {
          if (pending.fail) { resolve({ ok: false, status: 400 }); return; }
          server.order = names.slice();
          resolve({ ok: true });
        };
        const gate = pending.gate;
        pending.gate = null;
        if (gate) gate.settle = settle; else settle();      // 扣住响应，由用例决定何时返回
      });
    }
    function fetchJSON(url) { return Promise.resolve(url === '/api/status' ? serverStatus() : {}); }
    // 与页面一致：任一请求失败则整次刷新失败，updateVendorStatus 不会被调用
    async function refreshOverview() {
      if (refreshFails) throw new Error('refresh failed');
      updateVendorStatus(await fetchJSON('/api/status'));
      updateSubscriptions({ vendors: [] });
    }
    """
)

_DRAG_HARNESS = textwrap.dedent(
    r"""
    (async () => {
      const pointerDown = (vendor, id) => tierList.fire('pointerdown', { target: handleOf(vendor), pointerId: id, clientY: 100 });
      const pointerMove = (id, y) => tierList.fire('pointermove', { pointerId: id, clientY: y });
      // 收尾事件经冒泡到达 document，统一从 document 侧派发
      const pointerUp = (id) => document.releaseOutside(id);
      const rowTop = (vendor) => tierList.querySelectorAll('[data-vendor]').find(el => el.dataset.vendor === vendor).getBoundingClientRect().top;
      const startDrag = async (vendor, id) => {
        pointerDown(vendor, id);
        pointerMove(id, 112);                                   // 越过 4px 阈值
      };
      const reset = async (order) => {
        server.order = order.slice();
        server.cb = {};
        refreshFails = false; pending.fail = false; pending.gate = null;
        puts.length = 0;
        await refreshOverview();
      };

      // 一、指针在列表外松开：收尾事件只到达 document，仍须提交新顺序并复位状态（存量卡死缺陷）
      initTierDrag();
      await reset(['zhipu', 'anthropic', 'copilot']);
      await startDrag('zhipu', 1);
      pointerMove(1, rowTop('anthropic') + 50);                 // 越过 anthropic 行中点 → DOM 重排
      assert.deepStrictEqual(domOrder(), ['anthropic', 'zhipu', 'copilot']);
      document.releaseOutside(1);
      await tick(); await tick();
      assert.deepStrictEqual(puts, [['anthropic', 'zhipu', 'copilot']]);
      assert.deepStrictEqual(server.order, ['anthropic', 'zhipu', 'copilot']);
      assert.deepStrictEqual(domOrder(), server.order);
      assert.strictEqual(_tierDrag.active, false);
      assert.strictEqual(tierList.querySelectorAll('[data-vendor]').filter(el => el.classList.contains('dragging')).length, 0);

      // 二、拖拽进行中第二个指针按在另一行手柄上：不得劫持（触屏多指）
      await reset(['zhipu', 'anthropic', 'copilot']);
      await startDrag('zhipu', 1);
      pointerDown('anthropic', 2);
      assert.strictEqual(_tierDrag.srcEl.dataset.vendor, 'zhipu');
      assert.strictEqual(_tierDrag.pointerId, 1);
      pointerMove(2, 300);                                      // 第二个指针的移动被忽略
      assert.deepStrictEqual(domOrder(), ['anthropic', 'zhipu', 'copilot']);
      document.releaseOutside(2);                               // 第二个指针松开不收尾
      assert.strictEqual(_tierDrag.active, true);
      pointerMove(1, rowTop('anthropic') + 50);
      pointerUp(1);
      await tick(); await tick();
      assert.deepStrictEqual(puts, [['anthropic', 'zhipu', 'copilot']]);
      assert.strictEqual(_tierDrag.active, false);

      // 三、PUT 在途时开始第二次拖拽并拖回原位：补绘不得用过期缓存画回旧序
      await reset(['zhipu', 'anthropic', 'copilot']);
      const gate = {}; pending.gate = gate;                     // 扣住 PUT 响应
      await startDrag('zhipu', 1);
      pointerMove(1, rowTop('anthropic') + 50);
      pointerUp(1);
      assert.strictEqual(_tierDrag.inFlight, true);
      await startDrag('anthropic', 1);
      pointerMove(1, rowTop('zhipu') + 50);                     // 第二次拖拽越过 zhipu：DOM 回到拖拽 A 之前的相对位置
      assert.deepStrictEqual(domOrder(), ['zhipu', 'anthropic', 'copilot']);
      server.cb = { zhipu: 'open' };                           // 服务端状态在 PUT 之后有更新
      gate.settle();                                            // PUT A 成功返回：服务端已是新序，刷新被守卫跳过
      await tick(); await tick(); await tick();
      assert.deepStrictEqual(server.order, ['anthropic', 'zhipu', 'copilot']);
      assert.ok(!tierList.innerHTML.includes('熔断 ×2'));      // 拖拽中不重绘
      pointerMove(1, rowTop('zhipu') - 10);                     // 拖回本次拖拽的起点（顺序未变）
      pointerUp(1);
      assert.deepStrictEqual(domOrder(), server.order);
      assert.strictEqual(puts.length, 1);                       // 顺序未变不发 PUT
      assert.ok(tierList.innerHTML.includes('熔断 ×2'));       // 拖拽期间到达的刷新在收尾时补绘

      // 四、PUT 成功但随后的刷新失败：缓存已按服务端确认的顺序同步，空拖拽后行序不回弹
      await reset(['anthropic', 'zhipu', 'copilot']);
      refreshFails = true;
      await startDrag('zhipu', 1);
      pointerMove(1, rowTop('anthropic') - 10);                 // zhipu 提到首位
      pointerUp(1);
      await tick(); await tick(); await tick();
      assert.deepStrictEqual(server.order, ['zhipu', 'anthropic', 'copilot']);
      assert.deepStrictEqual(domOrder(), server.order);
      await startDrag('anthropic', 1);
      pointerMove(1, rowTop('anthropic') + 4);                  // 原地空拖拽：顺序未变
      pointerUp(1);
      assert.deepStrictEqual(domOrder(), server.order);
      assert.strictEqual(puts.length, 1);

      // 五、PUT 失败：回滚到服务端真实顺序
      await reset(['zhipu', 'anthropic', 'copilot']);
      pending.fail = true;
      await startDrag('zhipu', 1);
      pointerMove(1, rowTop('anthropic') + 50);
      pointerUp(1);
      await tick(); await tick(); await tick();
      assert.deepStrictEqual(domOrder(), ['zhipu', 'anthropic', 'copilot']);
      assert.strictEqual(_tierDrag.active, false);
      assert.strictEqual(_tierDrag.inFlight, false);

      // 六、仅按下手柄未拖动：空操作
      await reset(['zhipu', 'anthropic', 'copilot']);
      pointerDown('zhipu', 1);
      pointerUp(1);
      await tick();
      assert.strictEqual(puts.length, 0);
      assert.deepStrictEqual(domOrder(), ['zhipu', 'anthropic', 'copilot']);
      console.log('tier drag harness OK');
    })().catch(err => { console.error(err); process.exit(1); });
    """
)


@pytest.mark.skipif(_NODE is None, reason="node 不可用")
def test_tier_drag_behavior(client: TestClient, tmp_path):
    """拖拽状态机在最小假 DOM 上执行：列表外松开、多指重入、过期缓存补绘、失败回滚."""
    script = _inline_script(client.get("/dashboard").text)
    drag_sources = [
        _extract_block(script, "const _tierDrag = {", "};"),
        _extract_block(script, "const _TIER_DRAG_THRESHOLD = ", ";"),
    ]
    drag_sources += [
        _extract_function(script, signature)
        for signature in (
            "function _tierCurrentOrder(",
            "function _tierDragMoveTo(",
            "function _tierDragReset(",
            "function initTierDrag(",
            "function _syncTierOrder(",
            "function persistTierOrder(",
            "function _tierRevertToList(",
        )
    ]
    prelude = (
        "let _tierStatus = { tiers: [] };\n"
        "let _subData = { vendors: [] };\n" + _meta_block(script)
    )
    _run_node(
        tmp_path,
        "drag_harness.js",
        "const assert = require('assert');\n"
        + _DRAG_PRELUDE
        + prelude
        + "\n".join(drag_sources + _row_sources(script))
        + _DRAG_HARNESS,
        "tier drag harness OK",
    )
