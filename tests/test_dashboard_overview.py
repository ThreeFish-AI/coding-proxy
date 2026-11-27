"""Dashboard Overview 的 KPI 条、Model Calling 列表、趋势页签与订阅区溢出提示：HTML 接线（静态检查）与内联 JS 行为（Node）."""

from __future__ import annotations

import re
import textwrap

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from coding.proxy.server.dashboard import register_dashboard_routes
from tests.test_dashboard_subscription_card import (
    _NODE,
    _extract_block,
    _extract_function,
    _inline_script,
    _run_node,
)

_KPI_CARDS = (
    ("req", "今日请求数", "kpi-card"),
    ("tok", "今日 Token 总量", "kpi-card"),
    ("out", "今日输出 Token", "kpi-card"),
    ("cost", "今日费用估算", "kpi-card kpi-cost"),
    ("fo", "今日故障转移", "kpi-card"),
    ("lat", "今日平均延迟", "kpi-card"),
)
_TREND_TABS = (
    ("requests", "请求量", "chart-timeline"),
    ("tokens", "Token（按供应商）", "chart-token-timeline"),
    ("models", "Token（Vendor / 模型）", "chart-model-token-timeline"),
)


@pytest.fixture
def client() -> TestClient:
    app = FastAPI()
    register_dashboard_routes(app)
    return TestClient(app, base_url="http://127.0.0.1:3392")


def _attrs(tag: str) -> dict[str, str]:
    return dict(re.findall(r"([\w-]+)=\"([^\"]*)\"", tag))


def _rule(html: str, selector: str) -> str:
    match = re.search(r"(?m)^\s*" + re.escape(selector) + r"\s*\{([^}]*)\}", html)
    assert match, f"{selector} 规则缺失"
    return match.group(1)


# ── HTML / CSS 接线 ────────────────────────────────────────────────────────


def test_overview_status_row_and_trend_card_wired(client: TestClient):
    """订阅状态与供应商请求分布并排；三张趋势图收进一个页签图位，页签与面板的 ARIA 引用两两对应."""
    html = client.get("/dashboard").text
    assert 'class="charts-grid' not in html
    row_at = html.index('<div class="status-row">')
    trend_tag_at = html.rindex(
        "<section", 0, html.index('class="card timeline-card"', row_at)
    )
    row = html[row_at:trend_tag_at]
    assert row.index('id="subscription-card"') < row.index('id="chart-vendor-dist"')
    assert len(re.findall(r'<div class="card ', row)) == 2

    # 趋势卡保留完整的 class="card timeline-card"：订阅卡测试用它做片段的结束分隔
    trend = html[trend_tag_at : html.index("</section>", trend_tag_at)]
    tablist_at = trend.index('role="tablist"')
    tablist = trend[: trend.index("</div>", tablist_at)]
    assert 'role="tablist" aria-label="趋势图"' in tablist
    tab_tags = re.findall(r"<button [^>]*>", tablist)
    panel_tags = re.findall(r'<div class="trend-panel"[^>]*>', trend)
    assert len(tab_tags) == len(panel_tags) == len(_TREND_TABS)
    for i, (key, label, canvas_id) in enumerate(_TREND_TABS):
        tab, panel = _attrs(tab_tags[i]), _attrs(panel_tags[i])
        tab_id, panel_id = f"trend-tab-{key}", f"trend-panel-{key}"
        assert tab["class"] == "trend-tab" and tab["role"] == "tab"
        assert tab["id"] == tab_id and tab["aria-controls"] == panel_id
        assert tab["data-trend"] == key
        assert tab["aria-selected"] == ("true" if i == 0 else "false")
        assert tab["tabindex"] == ("0" if i == 0 else "-1")
        assert trend.index(f'id="{tab_id}"') < trend.index(f">{label}</button>")
        assert panel["id"] == panel_id and panel["aria-labelledby"] == tab_id
        assert panel["role"] == "tabpanel" and panel["tabindex"] == "0"
        # 只有当前页签的面板可见（hidden 是布尔属性，没有值）
        assert (" hidden" in panel_tags[i]) is (i != 0)
        panel_at = html.rindex(
            '<div class="trend-panel"', 0, html.index(f'id="{panel_id}"')
        )
        canvas_at = html.index(f'id="{canvas_id}"')
        assert panel_at < canvas_at
        assert 'role="tabpanel"' not in html[html.index(">", panel_at) : canvas_at]
    # 页签类名另起一套：.tab-btn / .tab-pane 被页首主页签的状态逻辑按类名全文档匹配
    assert not re.search(r'class="[^"]*\btab-(btn|pane)\b[^"]*trend', html)
    assert 'class="trend-range" id="trend-range"' in trend
    for gone in (
        "title-timeline",
        "title-token-timeline",
        "title-model-token-timeline",
    ):
        assert gone not in html


def test_overview_kpi_and_model_calling_markup(client: TestClient):
    """KPI 是一条 strip（标签、数值、副行，无图标与装饰色类）；Model Calling 标题不带 emoji."""
    html = client.get("/dashboard").text
    grid = html[html.index('<div class="kpi-grid" id="kpi-grid">') :]
    grid = grid[: grid.index("\n  </div>")]
    cards = re.split(r"(?=<div class=\"kpi-card)", grid)[1:]
    assert len(cards) == len(_KPI_CARDS)
    for card, (key, label, cls) in zip(cards, _KPI_CARDS, strict=True):
        assert card.startswith(f'<div class="{cls}">'), key
        assert f'<div class="kpi-label">{label}</div>' in card
        assert f'<div class="kpi-value" id="kpi-{key}-today">' in card
        assert f'<div class="kpi-sub" id="kpi-{key}-week">' in card
    assert "kpi-icon" not in html
    assert not re.search(
        r"class=\"[^\"]*\bcolor-(blue|purple|green|yellow|red|orange)\b", html
    )

    title = html[html.index('id="model-calling-card"') :]
    title = title[title.index('<div class="card-title">') :]
    title = title[: title.index("</div>")]
    assert title.startswith('<div class="card-title">Model Calling 实时状态')
    assert title.endswith('<span class="card-note">每 10 秒刷新</span>')


def test_overview_css_constraints(client: TestClient):
    """布局硬约束落在 CSS 上：两栏列宽与堆叠断点、KPI 列、Model Calling 定宽列、页签下划线、渐隐提示."""
    html = client.get("/dashboard").text
    assert "clamp(280px, 24vw, 360px)" in _rule(html, ".status-row")
    assert ".status-row { grid-template-columns: minmax(0, 1fr);" in _rule(
        html, "@media (max-width: 1179px)"
    )
    assert (
        "repeat(3, minmax(0, 1fr)) minmax(0, 1.45fr) repeat(2, minmax(0, 1fr))"
        in _rule(html, ".kpi-grid")
    )
    assert "@media (max-width: 1023px)" in html and "@media (max-width: 519px)" in html
    # 计数列定宽：条形终点逐行对齐，不随「4/10」「5/8」的字数变化
    assert (
        "minmax(72px, 96px) minmax(0, 1.5fr) minmax(80px, 1fr) 64px minmax(80px, auto)"
        in _rule(html, ".mc-row")
    )
    # 上限按钮与编辑输入框同宽：进入编辑态列不跳；命中区不小于 24×24
    editable = _rule(html, ".mc-limit-editable")
    assert "width: 40px;" in editable and "min-height: 24px;" in editable
    assert "rgba(88,166,255,.8)" in editable  # 虚线是非文字对比信息：对行底色不低于 3:1
    assert "width: 40px; height: 24px;" in _rule(html, ".mc-limit-input")
    assert "@media (min-width: 1280px)" in html and "@media (max-width: 759px)" in html
    assert "border-bottom-color: var(--accent-blue)" in _rule(
        html, '.trend-tab[aria-selected="true"]'
    )
    assert "linear-gradient(90deg, #000 calc(100% - 28px), transparent)" in _rule(
        html, ".vendor-badges.has-more"
    )
    # 新增小字不用 --text-tertiary：它在卡片底色上只有 4.02:1，不到 AA
    for selector in (
        ".kpi-sub",
        ".mc-model-name",
        ".mc-meter-note",
        ".mc-peak",
        ".card-note",
        ".trend-range",
    ):
        assert "--text-tertiary" not in _rule(html, selector), selector


def test_overview_scripts_avoid_unsafe_dom_writes(client: TestClient):
    """KPI 与 Model Calling 的文案全部来自接口，只能经 textContent 写入；编辑监听在捕获阶段接管."""
    script = _inline_script(client.get("/dashboard").text)
    for signature in (
        "function updateKPI(",
        "function updateModelCalling(",
        "function _mcBuildRow(",
        "function _mcUpdateRow(",
        "function _mcAnnounce(",
        "function startLimitEdit(",
        "function setTrendTab(",
    ):
        assert "innerHTML" not in _extract_function(script, signature), signature
    # 编辑监听在捕获阶段接管：同一次点击不会再冒泡给页面里其它委托处理器
    listener_at = script.index("document.addEventListener('click', function (e) {")
    listener = script[listener_at : script.index("}, true);", listener_at)]
    assert "startLimitEdit(btn);" in listener
    assert script.count("startLimitEdit(") == 2  # 定义一次，只在这个监听器里调用
    # 页面原有的 CSS 规则管不到 canvas：减少动态时关掉 Chart.js 的画布动画
    assert (
        "if (window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches) {\n"
        "    Chart.defaults.animation = false;" in script
    )


# ── 内联 JS 行为（Node） ────────────────────────────────────────────────────

# 最小假 DOM：只实现被测函数用到的表面（类名、属性、数据集、子节点、事件、焦点、按类名查询）。
# 没有 HTML 解析：被测代码一旦改回 innerHTML 拼接，这里就直接失败
_FAKE_DOM = textwrap.dedent(
    r"""
    const byId = {};
    // 假计时器按时长推进：advance(ms) 只触发这段时间内到期的回调（8 秒兜底不会被 50 毫秒的失焦守卫顺带触发）
    const timers = new Map(); let clock = 0; let timerSeq = 0;
    const setTimeout = (fn, ms) => { timers.set(++timerSeq, { fn: fn, at: clock + (ms || 0) }); return timerSeq; };
    const clearTimeout = (id) => { timers.delete(id); };
    const advance = (ms) => {
      clock += ms;
      for (let due = [...timers].filter(([, t]) => t.at <= clock); due.length; due = [...timers].filter(([, t]) => t.at <= clock)) {
        due.forEach(([id, t]) => { timers.delete(id); t.fn(); });
      }
    };
    const tick = () => new Promise((resolve) => setImmediate(resolve));

    class FakeEl {
      constructor(tag) {
        this.tagName = String(tag).toUpperCase();
        this.children = []; this.parentNode = null;
        this.className = ''; this.dataset = {}; this.attrs = {}; this.style = {};
        this._text = ''; this._handlers = {}; this._id = '';
        this.classList = {
          add: (...cs) => { this.className = Array.from(new Set(this.className.split(/\s+/).filter(Boolean).concat(cs))).join(' '); },
          remove: (...cs) => { this.className = this.className.split(/\s+/).filter((c) => c && !cs.includes(c)).join(' '); },
          contains: (c) => this.className.split(/\s+/).includes(c),
          toggle: (c, force) => {
            const on = force === undefined ? !this.classList.contains(c) : !!force;
            if (on) this.classList.add(c); else this.classList.remove(c);
            return on;
          },
        };
      }
      set id(value) { this._id = String(value); byId[this._id] = this; }
      get id() { return this._id; }
      get nextSibling() {
        if (!this.parentNode) return null;
        return this.parentNode.children[this.parentNode.children.indexOf(this) + 1] || null;
      }
      setAttribute(k, v) { this.attrs[k] = String(v); if (k === 'class') this.className = String(v); }
      getAttribute(k) { return k in this.attrs ? this.attrs[k] : null; }
      get textContent() { return this._text + this.children.map((c) => c.textContent).join(''); }
      set textContent(v) { this.children.slice().forEach((c) => c.remove()); this._text = String(v); }
      appendChild(c) { c.remove(); c.parentNode = this; this.children.push(c); return c; }
      append(...cs) { cs.forEach((c) => this.appendChild(c)); }
      insertBefore(node, ref) {
        this.appendChild(node);
        if (ref && ref !== node) {
          this.children.splice(this.children.indexOf(node), 1);
          this.children.splice(this.children.indexOf(ref), 0, node);
        }
        return node;
      }
      // 移除带焦点的节点会触发 blur（原实现的第二个 PUT 就是从这里发出的）
      remove() {
        const hadFocus = document.activeElement === this;
        if (this.parentNode) this.parentNode.children.splice(this.parentNode.children.indexOf(this), 1);
        this.parentNode = null;
        if (hadFocus) { document.activeElement = null; (this._handlers.blur || []).forEach((fn) => fn({})); }
      }
      replaceChildren(...cs) { this.children.slice().forEach((c) => c.remove()); cs.forEach((c) => this.appendChild(c)); }
      // 选择器只支持本页用到的简单形态：#id、.class、tag 的组合，以及「#id .class」后代
      matches(sel) {
        const id = (sel.match(/#([\w-]+)/) || [])[1];
        if (id && this._id !== id) return false;
        const have = this.className.split(/\s+/);
        if (!(sel.match(/\.[\w-]+/g) || []).every((c) => have.includes(c.slice(1)))) return false;
        const bare = sel.replace(/#[\w-]+/g, '').replace(/\.[\w-]+/g, '').trim();
        return !bare || this.tagName === bare.toUpperCase();
      }
      closest(sel) { for (let el = this; el; el = el.parentNode) if (el.matches(sel)) return el; return null; }
      querySelectorAll(sel) {
        const parts = sel.trim().split(/\s+/);
        const scope = parts.length === 2 && parts[0].startsWith('#') ? byId[parts[0].slice(1)] : this;
        const last = parts[parts.length - 1];
        const out = [];
        const walk = (el) => el.children.forEach((c) => { if (c.matches(last)) out.push(c); walk(c); });
        if (scope) walk(scope);
        return out;
      }
      querySelector(sel) { return this.querySelectorAll(sel)[0] || null; }
      addEventListener(type, fn) { (this._handlers[type] = this._handlers[type] || []).push(fn); }
      fire(type, ev) {  // 触发监听器，返回是否拦截了默认行为
        const event = Object.assign({ defaultPrevented: false, preventDefault() { this.defaultPrevented = true; } }, ev);
        (this._handlers[type] || []).forEach((fn) => fn(event));
        return event.defaultPrevented;
      }
      focus() { document.activeElement = this; }
      select() {}
      insertAdjacentHTML(pos, html) {   // 被测代码只用它插入铅笔图标
        const m = html.match(/^<(\w+)[^>]*class="([^"]*)"/);
        const node = new FakeEl(m[1]); node.className = m[2]; this.appendChild(node);
      }
    }
    const document = { activeElement: null, createElement: (tag) => new FakeEl(tag), getElementById: (id) => byId[id] || null };
    const el = (tag, cls, text) => { const n = new FakeEl(tag); if (cls) n.className = cls; if (text != null) n.textContent = text; return n; };
    const root = new FakeEl('root');
    document.querySelector = (sel) => root.querySelectorAll(sel)[0] || null;
    document.querySelectorAll = (sel) => root.querySelectorAll(sel);
    """
)


def _sources(script: str, *signatures: str) -> str:
    return "\n".join(_extract_function(script, signature) for signature in signatures)


def _kpi_harness(script: str) -> str:
    return (
        "const assert = require('assert');\n"
        + _FAKE_DOM
        + "let currentDays = 7;\n"
        + _sources(
            script,
            "function rangeLabel(",
            "function costParts(",
            "function updateKPI(",
            "function fmtTokens(",
            "function fmtNum(",
        )
        + textwrap.dedent(
            r"""
            const today = new Date().toISOString().slice(0, 10);
            const custom = el('div', ''); custom.id = 'range-custom';
            const start = el('input', ''); start.id = 'range-start';
            const end = el('input', ''); end.id = 'range-end';
            const nodes = {};
            for (const key of ['req', 'tok', 'out', 'cost', 'fo', 'lat']) {
              nodes[key] = { today: el('div', 'kpi-value'), week: el('div', 'kpi-sub') };
              nodes[key].today.id = 'kpi-' + key + '-today'; nodes[key].week.id = 'kpi-' + key + '-week';
            }
            const partsOf = (node) => node.children.map((c) => c.textContent);
            const summary = (todayCost, rangeCost) => ({
              today: { requests: 2583, tokens: { input: 1, output: 800700, cache_creation: 2, cache_read: 3 }, failovers: 3, avg_duration_ms: 4820, cost: todayCost },
              range: { requests: 14237, tokens: { input: 0, output: 4410000, cache_creation: 0, cache_read: 0 }, failovers: 21, avg_duration_ms: 4820, cost: rangeCost },
            });

            // 内置区间：近 N 天
            assert.strictEqual(rangeLabel(), '近 7 天');
            currentDays = 30;
            assert.strictEqual(rangeLabel(), '近 30 天');
            // 自选区间且截止日是今天：仍写近 N 天
            currentDays = 12; custom.classList.add('visible'); start.value = today; end.value = today;
            assert.strictEqual(rangeLabel(), '近 12 天');
            // 自选区间看历史窗口：写起止日期，不能也叫近 N 天；跨年写两位年份
            start.value = '2026-11-01'; end.value = '2026-11-07';
            assert.strictEqual(rangeLabel(), '11-01 ~ 11-07');
            start.value = '2025-12-20'; end.value = '2026-01-05';
            assert.strictEqual(rangeLabel(), '25-12-20 ~ 26-01-05');
            // 自选面板没收起（点的是预设按钮）时不读日期输入框
            custom.classList.remove('visible'); start.value = '';
            assert.strictEqual(rangeLabel(), '近 12 天');

            // 费用按「 + 」拆段：第二段带加号；没有费用时是一格「–」
            assert.deepStrictEqual(costParts('¥54.24'), ['¥54.24']);
            assert.deepStrictEqual(costParts('¥298.96 + $412.37'), ['¥298.96', '+ $412.37']);
            assert.deepStrictEqual(costParts(''), ['–']);
            assert.deepStrictEqual(costParts(null), ['–']);

            updateKPI(summary('¥54.24', '¥298.96 + $412.37'));
            assert.strictEqual(nodes.req.today.textContent, '2,583');
            assert.deepStrictEqual(partsOf(nodes.req.week), ['近 12 天', '14,237']);
            assert.deepStrictEqual(nodes.req.week.children.map((c) => c.className), ['kpi-sub-label', 'kpi-sub-val']);
            assert.strictEqual(nodes.tok.today.textContent, '800.7K');
            assert.deepStrictEqual(partsOf(nodes.tok.week), ['近 12 天', '4.41M']);
            assert.strictEqual(nodes.out.today.textContent, '800.7K');
            assert.deepStrictEqual(partsOf(nodes.out.week), ['近 12 天', '4.41M']);
            assert.deepStrictEqual(partsOf(nodes.cost.today), ['¥54.24']);
            assert.deepStrictEqual(partsOf(nodes.cost.week), ['近 12 天', '¥298.96', '+ $412.37']);
            assert.strictEqual(nodes.fo.today.textContent, '3');
            assert.strictEqual(nodes.lat.today.textContent, '4820ms');

            // 重复刷新整体重写，不残留上一轮的段；没有费用时是一格「–」
            updateKPI(summary('', null));
            assert.deepStrictEqual(partsOf(nodes.cost.week), ['近 12 天', '–']);
            assert.strictEqual(nodes.lat.today.textContent, '4820ms');
            // 没有请求数与延迟时都是「–」
            updateKPI({ today: { requests: null, tokens: {}, failovers: null, avg_duration_ms: 0, cost: '' }, range: { requests: null, tokens: {}, failovers: null, avg_duration_ms: 0, cost: '' } });
            assert.strictEqual(nodes.req.today.textContent, '–');
            assert.strictEqual(nodes.lat.today.textContent, '–');
            assert.deepStrictEqual(partsOf(nodes.lat.week), ['近 12 天', '–']);
            // 文案原样进文本节点：带标签的字符串不会被当成标记
            updateKPI({ today: { requests: '<img src=x onerror=alert(1)>', tokens: {}, failovers: 0, avg_duration_ms: 0, cost: '<b>x</b>' }, range: { requests: 0, tokens: {}, failovers: 0, avg_duration_ms: 0, cost: '' } });
            assert.strictEqual(nodes.req.today.textContent, '<img src=x onerror=alert(1)>');
            assert.deepStrictEqual(nodes.req.today.children, []);
            assert.deepStrictEqual(partsOf(nodes.cost.today), ['<b>x</b>']);
            assert.ok(nodes.cost.today.children.every((c) => c.tagName === 'SPAN' && c.children.length === 0));
            console.log('kpi harness OK');
            """
        )
    )


@pytest.mark.skipif(_NODE is None, reason="node 不可用")
def test_kpi_range_label_and_sub_lines(client: TestClient, tmp_path):
    """区间标签只有一处口径；副行由区间标签与数值段组成，费用按币种拆段，文案不拼 HTML."""
    script = _inline_script(client.get("/dashboard").text)
    _run_node(tmp_path, "kpi_harness.js", _kpi_harness(script), "kpi harness OK")


# Model Calling harness 的公共前置：按 /api/status 的形状拼载荷（tiers[].diagnostics.concurrency[model]），
# 每个 tier 是 [名字, { 模型: [mode, limit, 在途, 排队, 最近 10 秒排队峰值] }]
_MC_PRELUDE = textwrap.dedent(
    r"""
    const _status = (...tiers) => {
      const out = { tiers: [] };
      tiers.forEach(([name, models]) => {
        const concurrency = {};
        Object.keys(models).forEach((model) => {
          const [mode, limit, inUse, pending, peak] = models[model];
          const entry = { mode: mode, in_use: inUse, pending: pending, peak_pending_recent: peak };
          if (mode === 'limited') { entry.limit = limit; entry.available = limit - inUse - pending; }
          concurrency[model] = entry;
        });
        out.tiers.push({ name: name, diagnostics: { concurrency: concurrency } });
      });
      return out;
    };
    const status = _status(
      ['zhipu', { 'glm-4.6': ['limited', 10, 4, 1, 0], 'glm-4.5-air': ['limited', 6, 1, 0, 1] }],
      ['anthropic', { 'claude-sonnet-4-5': ['limited', 8, 5, 2, 0] }],
      ['copilot', { 'gpt-5': ['limited', 4, 1, 0, 0] }],
    );
    // 与 status 同构，只把 glm-4.6 的上限换成 limit
    const fullStatus = (limit) => {
      const next = _status(
        ['zhipu', { 'glm-4.6': ['limited', 10, 4, 1, 0], 'glm-4.5-air': ['limited', 6, 1, 0, 1] }],
        ['anthropic', { 'claude-sonnet-4-5': ['limited', 8, 5, 2, 0] }],
        ['copilot', { 'gpt-5': ['limited', 4, 1, 0, 0] }],
      );
      next.tiers[0].diagnostics.concurrency['glm-4.6'].limit = limit;
      return next;
    };
    const card = el('div', 'card model-calling-card'); card.id = 'model-calling-card';
    const wrap = el('div', ''); wrap.id = 'model-calling-wrap'; card.appendChild(wrap);
    const list = () => wrap.querySelector('.mc-list');
    const rows = () => (list() ? list().children.slice() : []);
    const shown = (node) => node.style.display !== 'none';   // 与 CSS 的显隐一致：隐藏的段不算可见文案
    const useText = (row) => row.querySelector('.mc-use').children.filter(shown).map((c) => c.textContent).join('');
    const queueText = (row) => row.querySelector('.mc-queue').textContent;
    const fillOf = (row) => row.querySelector('.mc-fill');
    const limitOf = (row) => row.querySelector('.mc-limit-editable');
    const puts = []; const asked = [];
    let fetchJSON = (url) => { asked.push(url); return Promise.resolve({ tiers: [] }); };
    let fetch = (url, opts) => { puts.push(opts.body); return Promise.resolve({ ok: true }); };
    const limitClickListener = LIMIT_LISTENER;
    const clickLimit = (btn) => limitClickListener({ target: btn, preventDefault() {} });
    """
)


def _mc_harness(script: str, body: str) -> str:
    prefix = "document.addEventListener('click', "
    listener = _extract_function(script, prefix + "function (e) {")
    assert listener.startswith(prefix)
    return (
        "const assert = require('assert');\n"
        + _FAKE_DOM
        + _extract_block(
            script, "const _mcRows = new Map();", "var _mcEditing = false;"
        )
        + "\n"
        + _sources(
            script,
            "function _mcRowKey(",
            "function _mcEl(",
            "function _mcBuildRow(",
            "function _mcUpdateRow(",
            "function updateModelCalling(",
            "function _mcAnnounce(",
            "function startLimitEdit(",
            "function limitValue(",
        )
        + _MC_PRELUDE.replace("LIMIT_LISTENER", listener[len(prefix) :])
        + body
    )


@pytest.mark.skipif(_NODE is None, reason="node 不可用")
def test_model_calling_rows_are_keyed_and_aligned(client: TestClient, tmp_path):
    """行节点按 vendor/model 键复用：刷新只改文字与条宽，行序跟随 tier，消失的模型被移除."""
    script = _inline_script(client.get("/dashboard").text)
    _run_node(
        tmp_path,
        "mc_rows_harness.js",
        _mc_harness(
            script,
            textwrap.dedent(
                r"""
                updateModelCalling(status);
                assert.strictEqual(rows().length, 4);
                assert.strictEqual(list().getAttribute('role'), 'list');
                assert.ok(list().classList.contains('mc-cols-2'), '不少于 3 行排两列');
                const first = rows();
                assert.deepStrictEqual(first.map((r) => r.dataset.key), ['zhipu/glm-4.6', 'zhipu/glm-4.5-air', 'anthropic/claude-sonnet-4-5', 'copilot/gpt-5']);
                assert.ok(first.every((r) => r.getAttribute('role') === 'listitem'));
                const glm = first[0];
                assert.strictEqual(glm.querySelector('.mc-vendor').textContent, 'zhipu');
                assert.strictEqual(glm.querySelector('.mc-model-name').textContent, 'glm-4.6');
                assert.strictEqual(glm.querySelector('.mc-model-name').title, 'zhipu/glm-4.6');
                assert.strictEqual(useText(glm), '4/10');
                assert.strictEqual(fillOf(glm).style.width, '40%');
                assert.ok(fillOf(glm).classList.contains('mc-low'));
                assert.strictEqual(queueText(glm), '排队 1');
                const limit = limitOf(glm);
                assert.strictEqual(limit.tagName, 'BUTTON', '真按钮：键盘可达');
                assert.deepStrictEqual([limit.getAttribute('data-tier'), limit.getAttribute('data-model'), limit.getAttribute('data-limit')], ['zhipu', 'glm-4.6', '10']);
                assert.ok(limit.getAttribute('aria-label').includes('当前 10'));
                assert.strictEqual(limit.querySelector('.mc-limit-val').textContent, '10');
                assert.ok(glm.querySelector('.mc-pencil'), '行悬停 / 聚焦时露出的铅笔图标');
                // 严重度阈值：不超过 50% 绿、不超过 80% 黄、其余红
                const sonnet = first[2];
                assert.strictEqual(fillOf(sonnet).style.width, '63%');
                assert.ok(fillOf(sonnet).classList.contains('mc-mid'));
                assert.strictEqual(queueText(sonnet), '排队 2');
                // 当前无排队、最近 10 秒排过队：「曾排队 N」；都没有则留空槽位
                assert.strictEqual(queueText(first[1]), '曾排队 1');
                assert.strictEqual(queueText(first[3]), '');

                // 同样的载荷再刷新：不重建节点；数值变化只就地更新
                updateModelCalling(status);
                updateModelCalling(fullStatus(10));
                assert.deepStrictEqual(rows(), first);
                const busy = _status(
                  ['zhipu', { 'glm-4.6': ['limited', 10, 9, 0, 5], 'glm-4.5-air': ['limited', 6, 1, 0, 1] }],
                  ['anthropic', { 'claude-sonnet-4-5': ['limited', 8, 5, 2, 0] }],
                  ['copilot', { 'gpt-5': ['limited', 4, 1, 0, 0] }],
                );
                updateModelCalling(busy);
                assert.deepStrictEqual(rows(), first);
                assert.strictEqual(useText(glm), '9/10');
                assert.strictEqual(fillOf(glm).style.width, '90%');
                assert.ok(fillOf(glm).classList.contains('mc-high'));
                assert.strictEqual(queueText(glm), '曾排队 5');
                assert.strictEqual(glm.querySelector('.mc-meter').title, '并行度 9 / 10');

                // 行序 = tier 顺序（路由优先级）：节点不重建，只换位置
                updateModelCalling({ tiers: [status.tiers[2], status.tiers[0], status.tiers[1]] });
                assert.deepStrictEqual(rows().map((r) => r.dataset.key), ['copilot/gpt-5', 'zhipu/glm-4.6', 'zhipu/glm-4.5-air', 'anthropic/claude-sonnet-4-5']);
                assert.strictEqual(rows()[1], glm);

                // 消失的模型被移除；少于 3 行不再排两列
                updateModelCalling(_status(['zhipu', { 'glm-4.6': ['limited', 10, 4, 0, 0] }]));
                assert.deepStrictEqual(rows().map((r) => r.dataset.key), ['zhipu/glm-4.6']);
                assert.strictEqual(rows()[0], glm);
                assert.ok(!list().classList.contains('mc-cols-2') && _mcRows.size === 1);

                // monitor 模式：没有上限，不画进度条，计数写成「N 进行中」
                // （说明文字分宽屏 / 窄屏两份，由 CSS 只显示其一）
                updateModelCalling(_status(['antigravity', { 'gemini-3-pro-preview': ['monitor', null, 2, 0, 0] }]));
                const monitor = rows()[0];
                assert.ok(monitor.classList.contains('mc-monitor'));
                assert.strictEqual(useText(monitor), '2进行中');
                assert.strictEqual(limitOf(monitor).style.display, 'none');
                assert.strictEqual(monitor.querySelector('.mc-meter-note').textContent, '仅监控 · 不限并行度仅监控');
                assert.strictEqual(monitor.querySelector('.mc-badge'), null);
                updateModelCalling(status);
                assert.ok(!glm.classList.contains('mc-monitor') && limitOf(glm).style.display === '');

                // 没有 concurrency 诊断的 tier 不出行；全空显示空态并清掉缓存
                updateModelCalling({ tiers: [{ name: 'zhipu' }, { name: 'anthropic', diagnostics: {} }] });
                assert.strictEqual(wrap.querySelector('.mc-empty').textContent, '无活跃模型调用');
                assert.strictEqual(_mcRows.size, 0);
                updateModelCalling({ tiers: [] });
                assert.strictEqual(wrap.children.length, 1);
                console.log('mc rows harness OK');
                """
            ),
        ),
        "mc rows harness OK",
    )


@pytest.mark.skipif(_NODE is None, reason="node 不可用")
def test_model_calling_limit_edit_flow(client: TestClient, tmp_path):
    """并行度编辑：只收 1 到 20 的整数；编辑中整张表不重绘；每次编辑只发一个 PUT；结束或超时后焦点回到按钮."""
    script = _inline_script(client.get("/dashboard").text)
    _run_node(
        tmp_path,
        "mc_edit_harness.js",
        _mc_harness(
            script,
            textwrap.dedent(
                r"""
                // 输入判定：7.5、1e2、全角数字、越界都按非法处理，不截断后提交
                for (const raw of ['0', '21', '7.5', '1e2', '007', '７', '', '-1', '1.0', null, undefined, '<img>']) {
                  assert.ok(Number.isNaN(limitValue(raw)), String(raw));
                }
                for (const [raw, want] of [['1', 1], ['7', 7], ['20', 20], [' 8 ', 8]]) assert.strictEqual(limitValue(raw), want, String(raw));

                updateModelCalling(status);
                const btn = limitOf(rows()[0]);
                let inputs = 0;
                const created = document.createElement;
                document.createElement = (tag) => { inputs += 1; return created(tag); };
                const lastInput = () => { const node = btn.nextSibling; assert.strictEqual(node.className, 'mc-limit-input'); return node; };
                const edit = () => { clickLimit(btn); return lastInput(); };
                let resolvePut;
                const hang = () => { fetch = (url, opts) => { puts.push(opts.body); return new Promise((resolve) => { resolvePut = resolve; }); }; };

                // 打开编辑：输入框插到按钮之后并聚焦，整张表进入「编辑中」
                hang();
                let input = edit();
                assert.strictEqual(inputs, 1);
                assert.strictEqual(input.getAttribute('aria-label'), 'zhipu/glm-4.6 的并行度上限（1 到 20 的整数）');
                assert.strictEqual(input.value, '10');
                assert.strictEqual(document.activeElement, input);
                assert.strictEqual(btn.style.display, 'none');
                assert.ok(_mcEditing);
                // 编辑中点别处的上限：不开第二个编辑器
                clickLimit(limitOf(rows()[1]));
                assert.strictEqual(inputs, 1);
                // 编辑中轮询到达：不重绘，输入框、已输入的内容与焦点都原样保留（原实现在这里提交半截输入）
                input.value = '1';
                const drawn = rows();
                updateModelCalling(_status(['zhipu', { 'glm-4.6': ['limited', 3, 3, 0, 0], 'new-model': ['limited', 1, 0, 0, 0] }]));
                assert.deepStrictEqual(rows(), drawn);
                assert.strictEqual(btn.nextSibling, input);
                assert.strictEqual(input.value, '1');
                assert.strictEqual(document.activeElement, input);
                // 编辑期间上限按钮是隐藏的：在途数与上限的原值都没被上一步的轮询改掉
                assert.strictEqual(drawn[0].querySelector('.mc-in').textContent + '/' + btn.querySelector('.mc-limit-val').textContent, '4/10');
                assert.deepStrictEqual(puts, []);

                // 补输成 12 回车：只提交一次；PUT 未决期间再按回车、再失焦都不再提交
                input.value = '12';
                assert.strictEqual(input.fire('keydown', { key: 'Enter' }), true);
                input.fire('keydown', { key: 'Enter' });
                input.fire('blur', {});
                advance(1000);   // 只过 1 秒：失焦守卫到期，8 秒兜底还没到
                assert.deepStrictEqual(puts, ['{"tier":"zhipu","model":"glm-4.6","limit":12}']);
                assert.ok(_mcEditing, 'PUT 未决期间仍是编辑中');
                assert.strictEqual(btn.nextSibling, input);
                // 成功后：这一行立刻按新上限重算（条形、可访问名称），并拉一次状态对齐服务端
                fetchJSON = (url) => { asked.push(url); return Promise.resolve(fullStatus(12)); };
                resolvePut({ ok: true });
                return tick().then(() => {
                  assert.strictEqual(document.activeElement, btn, '焦点回到上限按钮');
                  assert.ok(btn.classList.contains('mc-limit-ok') && !_mcEditing && btn.style.display === '');
                  assert.strictEqual(rows().length, 4);
                  assert.strictEqual(useText(rows()[0]), '4/12');
                  assert.strictEqual(fillOf(rows()[0]).style.width, '33%');
                  assert.ok(btn.getAttribute('aria-label').includes('当前 12'));
                  assert.deepStrictEqual(asked, ['/api/status']);
                  // 输入框移除引发的 blur 不会再提交第二次（原实现在这里连发两个 PUT）
                  advance(2000);
                  assert.strictEqual(puts.length, 1);
                  const live = byId['mc-live'];
                  assert.ok(live.classList.contains('sr-only') && live.getAttribute('role') === 'status' && live.getAttribute('aria-live') === 'polite');
                  assert.strictEqual(live.textContent, 'zhipu/glm-4.6 的并行度已更新为 12');
                  assert.ok(!btn.classList.contains('mc-limit-ok'), '静态色标 1.5 秒后清除');

                  // Esc：不提交，值不变
                  input = edit();
                  input.value = '5';
                  input.fire('keydown', { key: 'Escape' });
                  assert.strictEqual(puts.length, 1);
                  assert.strictEqual(document.activeElement, btn);
                  assert.strictEqual(btn.querySelector('.mc-limit-val').textContent, '12');

                  // 没改动直接回车：不提交
                  input = edit();
                  input.fire('keydown', { key: 'Enter' });
                  assert.strictEqual(puts.length, 1);

                  // 非整数 / 越界：不提交，上限标红，播报原因
                  for (const bad of ['7.5', '21', '0', '1e2']) {
                    input = edit();
                    input.value = bad;
                    input.fire('keydown', { key: 'Enter' });
                    assert.strictEqual(puts.length, 1, bad);
                    assert.ok(btn.classList.contains('mc-limit-err'), bad);
                    assert.strictEqual(document.activeElement, btn, bad);
                    advance(2000);
                    assert.strictEqual(live.textContent, '并行度须是 1 到 20 的整数');
                  }

                  // PUT 失败：值不变，标红，解除编辑
                  fetch = (url, opts) => { puts.push(opts.body); return Promise.resolve({ ok: false, status: 500 }); };
                  input = edit();
                  input.value = '8';
                  input.fire('keydown', { key: 'Enter' });
                  return tick().then(() => {
                    assert.strictEqual(puts[1], '{"tier":"zhipu","model":"glm-4.6","limit":8}');
                    assert.strictEqual(btn.querySelector('.mc-limit-val').textContent, '12');
                    assert.ok(btn.classList.contains('mc-limit-err') && !_mcEditing);
                    advance(2000);
                    assert.strictEqual(live.textContent, 'zhipu/glm-4.6 的并行度更新失败');

                    // PUT 悬住：8 秒兜底解除编辑态，标红，焦点回到按钮；迟到的成功不再改动这一行
                    puts.length = 0;
                    hang();
                    input = edit();
                    input.value = '6';
                    input.fire('keydown', { key: 'Enter' });
                    assert.ok(_mcEditing);
                    advance(9000);   // 8 秒兜底计时器到点，收尾在微任务里完成
                    return tick().then(() => {
                      assert.ok(!_mcEditing && btn.style.display === '', '超时后解除编辑态');
                      assert.strictEqual(document.activeElement, btn);
                      assert.ok(btn.classList.contains('mc-limit-err'));
                      assert.strictEqual(btn.querySelector('.mc-limit-val').textContent, '12');
                      resolvePut({ ok: true });
                      return tick().then(() => {
                        assert.strictEqual(btn.querySelector('.mc-limit-val').textContent, '12');
                        advance(2000);
                        assert.strictEqual(live.textContent, 'zhipu/glm-4.6 的并行度更新失败');
                        console.log('mc edit harness OK');
                      });
                    });
                  });
                });
                """
            ),
        ),
        "mc edit harness OK",
    )


@pytest.mark.skipif(_NODE is None, reason="node 不可用")
def test_trend_tabs_switch(client: TestClient, tmp_path):
    """页签切换同步 aria-selected / tabindex / 面板 hidden；方向键循环、Home / End 到首尾，切焦点即选中."""
    script = _inline_script(client.get("/dashboard").text)
    _run_node(
        tmp_path,
        "trend_tabs_harness.js",
        "const assert = require('assert');\n"
        + _FAKE_DOM
        + _extract_block(script, "const TREND_CHARTS = {", "};")
        + "\n"
        + _sources(script, "function showTrendChart(", "function setTrendTab(")
        + "\nconst initTrendTabs = "
        + _extract_function(script, "function initTrendTabs(")
        + ";\n"
        + textwrap.dedent(
            r"""
            const keys = ['requests', 'tokens', 'models'];
            const tablist = el('div', 'trend-tabs'); root.appendChild(tablist);
            const tabs = keys.map((key, i) => {
              const tab = el('button', 'trend-tab', 'x');
              tab.dataset.trend = key; tab.tabIndex = i === 0 ? 0 : -1; tab.setAttribute('aria-selected', i === 0 ? 'true' : 'false');
              tab.id = 'trend-tab-' + key; tablist.appendChild(tab);
              return tab;
            });
            const panels = keys.map((key, i) => {
              const panel = el('div', 'trend-panel'); panel.id = 'trend-panel-' + key; panel.hidden = i !== 0;
              root.appendChild(panel); return panel;
            });
            // 每个页签一项：选中用大写，可见加叹号
            const state = () => keys.map((key, i) => (tabs[i].getAttribute('aria-selected') === 'true' ? key[0].toUpperCase() : key[0]) + (panels[i].hidden ? '' : '!')).join(' ');
            const calls = [];
            const askedFor = [];
            const stubs = {};
            for (const key of keys) {
              stubs[TREND_CHARTS[key]] = { resize: () => calls.push('resize'), stop: () => calls.push('stop'), update: (mode) => calls.push('update:' + mode) };
            }
            globalThis.window = { Chart: { getChart: (id) => { askedFor.push(id); return stubs[id]; } } };
            globalThis.Chart = globalThis.window.Chart;
            initTrendTabs();
            assert.strictEqual(state(), 'R! t m');

            // 面板由隐藏变可见：resize → stop → update('none')，只作用于当前页签的图
            // （Chart.js 在 resize 里建的 attach 动画要先停掉，update('none') 取消不了在途动画）
            setTrendTab('tokens', false);
            assert.strictEqual(state(), 'r T! m');
            assert.deepStrictEqual(askedFor, ['chart-token-timeline']);
            assert.deepStrictEqual(calls, ['resize', 'stop', 'update:none']);
            assert.strictEqual(tabs[1].tabIndex, 0);
            assert.ok(tabs[0].tabIndex === -1 && tabs[2].tabIndex === -1);
            assert.strictEqual(document.activeElement, null, '鼠标点击不抢焦点');
            setTrendTab('models', true);
            assert.strictEqual(state(), 'r t M!');
            assert.strictEqual(document.activeElement, tabs[2]);

            // 方向键：← → 循环，Home / End 到首尾；切焦点即选中
            const press = (from, key) => { calls.length = 0; return tablist.fire('keydown', { key: key, target: tabs[from] }); };
            assert.strictEqual(press(2, 'ArrowRight'), true);      // 末尾回绕到首个
            assert.strictEqual(state(), 'R! t m');
            assert.strictEqual(document.activeElement, tabs[0]);
            assert.strictEqual(press(0, 'ArrowLeft'), true);       // 首个回绕到末尾
            assert.strictEqual(state(), 'r t M!');
            press(2, 'Home');
            assert.strictEqual(state(), 'R! t m');
            assert.strictEqual(document.activeElement, tabs[0]);
            press(0, 'End');
            assert.strictEqual(state(), 'r t M!');
            assert.deepStrictEqual(calls, ['resize', 'stop', 'update:none']);
            // 水平页签不拦截 ↑ ↓（留给页面滚动），其它按键也不管
            assert.strictEqual(press(2, 'ArrowDown'), false);
            assert.strictEqual(press(2, 'Enter'), false);
            assert.strictEqual(state(), 'r t M!');

            // 点击页签；点在页签行的空白处不做事
            tablist.fire('click', { target: tabs[0] });
            assert.strictEqual(state(), 'R! t m');
            tablist.fire('click', { target: tablist });
            assert.strictEqual(state(), 'R! t m');

            // 图还没建出来（首轮刷新未完成）或 Chart.js 不可达：照常切换，不报错
            globalThis.window.Chart.getChart = () => null;
            setTrendTab('tokens', false);
            assert.strictEqual(state(), 'r T! m');
            globalThis.window = {};
            setTrendTab('models', false);
            assert.strictEqual(state(), 'r t M!');
            console.log('trend tabs harness OK');
            """
        ),
        "trend tabs harness OK",
    )


@pytest.mark.skipif(_NODE is None, reason="node 不可用")
def test_zone_overflow_cue(client: TestClient, tmp_path):
    """订阅区放不下的提示按各分区自己的溢出量判断，滚到头提示消失."""
    script = _inline_script(client.get("/dashboard").text)
    _run_node(
        tmp_path,
        "zone_cue_harness.js",
        "const assert = require('assert');\n"
        + _FAKE_DOM
        + _sources(script, "function syncZoneCues(")
        + textwrap.dedent(
            r"""
            const card = el('div', 'card subscription-card'); card.id = 'subscription-card'; root.appendChild(card);
            const other = el('div', 'card'); root.appendChild(other);   // 别的卡片里的同名分区不受影响
            const zone = (parent, cls, scrollWidth, clientWidth) => {
              const z = el('span', cls); z.scrollWidth = scrollWidth; z.clientWidth = clientWidth; z.scrollLeft = 0; parent.appendChild(z); return z;
            };
            const fits = zone(card, 'vendor-badges sub-zone', 300, 300);
            const over = zone(card, 'vendor-badges sub-zone', 410, 300);
            const tier = zone(card, 'vendor-badges tier-zone', 90, 90);
            const elsewhere = zone(other, 'vendor-badges sub-zone', 410, 300);
            const cues = () => [fits, over, tier, elsewhere].map((z) => z.classList.contains('has-more'));
            syncZoneCues();
            assert.deepStrictEqual(cues(), [false, true, false, false]);
            // 滚到头：只剩 1px 以内的舍入余量就不算「后面还有」
            over.scrollLeft = 108;
            syncZoneCues();
            assert.ok(over.classList.contains('has-more'));
            over.scrollLeft = 109;
            syncZoneCues();
            assert.ok(!over.classList.contains('has-more'));
            console.log('zone cue harness OK');
            """
        ),
        "zone cue harness OK",
    )
