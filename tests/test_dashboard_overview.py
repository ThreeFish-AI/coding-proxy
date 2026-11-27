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

    # 趋势卡带完整的 class="card timeline-card"（订阅卡测试静态断言它存在）
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
        # 面板只靠 aria-labelledby 取名：再写 aria-label 会被它覆盖，等于没写
        assert "aria-label" not in panel
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
    # 并行度编辑结果的播报节点写在模板里，而不是首次播报时才插入（首条内容可能被读屏漏掉）
    assert (
        '<div class="sr-only" id="mc-live" role="status" aria-live="polite"></div>'
        in html
    )
    assert html.count('id="mc-live"') == 1


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
    # 负下边距：选中页签的下划线盖住头部 hairline；负左边距：首个页签的文字与其它卡片标题左对齐
    assert "margin: 0 0 -1px -12px;" in _rule(html, ".trend-tabs")
    # 区间标签只有「近 N 天」：标签与说明都不需要任意断行
    assert "white-space: nowrap;" in _rule(html, ".trend-range")
    assert "white-space: nowrap;" in _rule(html, ".kpi-sub-label, .kpi-sub-val")
    # 可聚焦的页签面板有与全页一致的焦点环（2px 蓝色，画在内侧）
    focus = _rule(html, ".trend-panel:focus-visible")
    assert "outline: 2px solid var(--accent-blue); outline-offset: -2px;" in focus
    # 分布卡图形区的 flex-basis 为 0：钉在 canvas 上一次写下的像素高度上，左卡变矮后这一行回不去
    assert "flex: 1 1 0;" in _rule(html, ".dist-card .chart-wrap")
    # 模型名最多折两行，不默认截断（全名只在 title 里，键盘与触屏拿不到）
    name = html[html.index(".mc-model-name {\n      font-size: 12px") :]
    assert "-webkit-line-clamp: 2" in name[: name.index("}")]
    # 手机两列：先清掉 3 列布局的列内边距，再排 2 列（同特异性下后写的赢，末列才是 24px）
    phone = html[html.index("@media (max-width: 519px)") :]
    assert re.findall(r"\.kpi-card:nth-child\(([^)]*)\) \{ ([^}]*?);? \}", phone) == [
        (
            "3n+1",
            "border-left: 1px solid rgba(255,255,255,.06); padding-left: 16px; padding-right: 16px",
        ),
        ("3n", "padding-right: 16px"),
        ("2n+1", "border-left: 0; padding-left: 24px"),
        ("2n", "padding-right: 24px"),
        ("n+3", "border-top: 1px solid rgba(255,255,255,.06)"),
    ]
    # 简化后不再有引用的规则与变量已删除
    for dead in (".sb-ok", "--glow-blue:", "--accent-orange:", "--text-muted:"):
        assert dead not in html, dead
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
    """KPI、Model Calling 与图表摘要的文案全部来自接口，只能经 textContent / setAttribute 写入."""
    script = _inline_script(client.get("/dashboard").text)
    for signature in (
        "function updateKPI(",
        "function updateModelCalling(",
        "function _mcBuildRow(",
        "function _mcUpdateRow(",
        "function _mcAnnounce(",
        "function startLimitEdit(",
        "function setTrendTab(",
        "function setChartSummary(",
    ):
        assert "innerHTML" not in _extract_function(script, signature), signature
    # 上限按钮的点击由挂在 document 上的事件委托接管（页面没有别的委托 click 处理器，不需要捕获阶段）
    listener = _extract_function(
        script, "document.addEventListener('click', function (e) {"
    )
    assert listener.startswith("document.addEventListener('click', function (e) {")
    assert "startLimitEdit(btn);" in listener and "_mcPendingEdit = btn;" in listener
    assert (
        script.count("startLimitEdit(") == 3
    )  # 定义一次，监听器与上一段编辑的收尾各调用一次
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
      // 是否挂在树上：假 DOM 用 root 充当文档，祖先链上有 root 才算已连接（被移除的行连同它的子节点一起断开）
      get isConnected() { for (let n = this; n; n = n.parentNode) if (n === root) return true; return false; }
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
    // body: null：假 DOM 用 null 表示焦点落空，等价于真实 DOM 里焦点回到 document.body
    const document = { activeElement: null, body: null, createElement: (tag) => new FakeEl(tag), getElementById: (id) => byId[id] || null };
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
        + "let currentDays = 7;\nconst MAX_RANGE_DAYS = 90;\nlet refreshed = 0;\n"
        "const refresh = () => { refreshed += 1; };\n"
        + _sources(
            script,
            "function localDateStr(",
            "function rangeLabel(",
            "function applyCustomRange(",
            "function costParts(",
            "function updateKPI(",
            "function fmtTokens(",
            "function fmtNum(",
        )
        + textwrap.dedent(
            r"""
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

            // 区间标签只由取数天数决定：接口取的是截至今天的最近 N 天，自选区间也只折算成天数
            assert.strictEqual(rangeLabel(7), '近 7 天');
            assert.strictEqual(rangeLabel(30), '近 30 天');
            // 本地日期：东八区凌晨（UTC 仍是前一天）也写本地的今天
            assert.strictEqual(localDateStr(new Date(2026, 10, 27, 1, 0)), '2026-11-27');
            assert.strictEqual(localDateStr(new Date(2026, 0, 5, 23, 59)), '2026-01-05');

            // 自选区间：起止日期折算成天数（含首尾两天）；反向区间、缺一端都不取数
            start.value = '2026-11-01'; end.value = '2026-11-07';
            applyCustomRange();
            assert.deepStrictEqual([currentDays, refreshed], [7, 1]);
            start.value = '2026-11-20'; end.value = '2026-11-15';
            applyCustomRange();
            assert.deepStrictEqual([currentDays, refreshed], [7, 1]);
            start.value = '';
            applyCustomRange();
            assert.deepStrictEqual([currentDays, refreshed], [7, 1]);
            // 超出接口上限（90 天）按 90 天取数，标签与实际取数一致
            start.value = '2025-12-20'; end.value = '2026-06-30';
            applyCustomRange();
            assert.deepStrictEqual([currentDays, refreshed], [90, 2]);

            // 费用按「 + 」拆段：第二段带加号；没有费用时是一格「–」
            assert.deepStrictEqual(costParts('¥54.24'), ['¥54.24']);
            assert.deepStrictEqual(costParts('¥298.96 + $412.37'), ['¥298.96', '+\u00a0$412.37']);   // 加号后是不换行空格
            assert.deepStrictEqual(costParts(''), ['–']);
            assert.deepStrictEqual(costParts(null), ['–']);

            // 副行标签取这批数据实际取数的天数（入参），不读页面当前选中的区间
            currentDays = 30;
            updateKPI(summary('¥54.24', '¥298.96 + $412.37'), 12);
            assert.strictEqual(nodes.req.today.textContent, '2,583');
            assert.deepStrictEqual(partsOf(nodes.req.week), ['近 12 天', '14,237']);
            assert.deepStrictEqual(nodes.req.week.children.map((c) => c.className), ['kpi-sub-label', 'kpi-sub-val']);
            assert.strictEqual(nodes.tok.today.textContent, '800.7K');
            assert.deepStrictEqual(partsOf(nodes.tok.week), ['近 12 天', '4.41M']);
            assert.strictEqual(nodes.out.today.textContent, '800.7K');
            assert.deepStrictEqual(partsOf(nodes.out.week), ['近 12 天', '4.41M']);
            assert.deepStrictEqual(partsOf(nodes.cost.today), ['¥54.24']);
            assert.deepStrictEqual(partsOf(nodes.cost.week), ['近 12 天', '¥298.96', '+\u00a0$412.37']);
            assert.strictEqual(nodes.fo.today.textContent, '3');
            assert.strictEqual(nodes.lat.today.textContent, '4820ms');

            // 重复刷新整体重写，不残留上一轮的段；没有费用时是一格「–」
            updateKPI(summary('', null), 12);
            assert.deepStrictEqual(partsOf(nodes.cost.week), ['近 12 天', '–']);
            assert.strictEqual(nodes.lat.today.textContent, '4820ms');
            // 没有请求数与延迟时都是「–」
            updateKPI({ today: { requests: null, tokens: {}, failovers: null, avg_duration_ms: 0, cost: '' }, range: { requests: null, tokens: {}, failovers: null, avg_duration_ms: 0, cost: '' } }, 12);
            assert.strictEqual(nodes.req.today.textContent, '–');
            assert.strictEqual(nodes.lat.today.textContent, '–');
            assert.deepStrictEqual(partsOf(nodes.lat.week), ['近 12 天', '–']);
            // 文案原样进文本节点：带标签的字符串不会被当成标记
            updateKPI({ today: { requests: '<img src=x onerror=alert(1)>', tokens: {}, failovers: 0, avg_duration_ms: 0, cost: '<b>x</b>' }, range: { requests: 0, tokens: {}, failovers: 0, avg_duration_ms: 0, cost: '' } }, 7);
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
    """区间标签一律「近 N 天」且跟着实际取数的天数走；副行由区间标签与数值段组成，费用按币种拆段，文案不拼 HTML."""
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
    const card = el('div', 'card model-calling-card'); card.id = 'model-calling-card'; root.appendChild(card);
    const wrap = el('div', ''); wrap.id = 'model-calling-wrap'; card.appendChild(wrap);
    // 播报节点写在模板里（属性由 HTML 接线测试断言），这里只占位
    const live = el('div', 'sr-only'); live.id = 'mc-live'; card.appendChild(live);
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
            script, "const _mcRows = new Map();", "var _mcPendingEdit = null;"
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
                // 编辑中点别处的上限：不开第二个编辑器，这次点击记下来等当前编辑结束再接手（见 test_model_calling_limit_edit_hands_off_to_next_row）
                clickLimit(limitOf(rows()[1]));
                assert.strictEqual(inputs, 1);
                assert.strictEqual(_mcPendingEdit, limitOf(rows()[1]));
                _mcPendingEdit = null;   // 下面只验证单行流程，丢掉这次点击
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
                  // 播报节点来自模板（属性由 HTML 接线测试断言）
                  assert.strictEqual(live, byId['mc-live']);
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
def test_model_calling_limit_edit_hands_off_to_next_row(client: TestClient, tmp_path):
    """编辑一行时点另一行的上限：当前编辑结束后接着打开那一行（这次点击不被吞掉）；焦点已被移走或那一行已消失则作废."""
    script = _inline_script(client.get("/dashboard").text)
    _run_node(
        tmp_path,
        "mc_handoff_harness.js",
        _mc_harness(
            script,
            textwrap.dedent(
                r"""
                updateModelCalling(status);
                const [rowA, rowB] = rows();
                const btnA = limitOf(rowA), btnB = limitOf(rowB);
                const inputOf = (btn) => { const node = btn.nextSibling; return node && node.className === 'mc-limit-input' ? node : null; };
                const hang = () => { fetch = (url, opts) => { puts.push(opts.body); return new Promise((resolve) => { resolvePut = resolve; }); }; };
                let resolvePut;

                // 鼠标流：编辑 A 时点 B。浏览器先让 A 的输入框失焦，再派发点击；失焦守卫要 50 毫秒后才提交，
                // 点击到达时仍是「编辑中」：记下 B，而不是把这次点击吞掉（原实现要再点一次才进编辑）
                clickLimit(btnA);
                const inputA = inputOf(btnA);
                inputA.value = '12';
                btnB.focus();
                inputA.fire('blur', {});
                clickLimit(btnB);
                assert.strictEqual(_mcPendingEdit, btnB);
                assert.strictEqual(inputOf(btnB), null, 'A 还在编辑：不同时开两个编辑器');
                advance(50);
                assert.deepStrictEqual(puts, ['{"tier":"zhipu","model":"glm-4.6","limit":12}']);
                return tick().then(() => {
                  const inputB = inputOf(btnB);
                  assert.ok(inputB, 'A 提交完成后接着打开 B');
                  assert.strictEqual(inputB.getAttribute('aria-label'), 'zhipu/glm-4.5-air 的并行度上限（1 到 20 的整数）');
                  assert.strictEqual(document.activeElement, inputB);
                  assert.strictEqual(_mcPendingEdit, null);
                  assert.ok(_mcEditing, 'B 进入编辑，整张表仍是编辑中');
                  assert.strictEqual(wrap.querySelectorAll('.mc-limit-input').length, 1);
                  assert.strictEqual(btnB.style.display, 'none');
                  assert.strictEqual(btnA.style.display, '');
                  assert.strictEqual(btnA.querySelector('.mc-limit-val').textContent, '12');
                  assert.deepStrictEqual(puts.length, 1);
                  // B 按 Esc：不提交，焦点回到 B 的按钮
                  inputB.fire('keydown', { key: 'Escape' });
                  assert.strictEqual(puts.length, 1);
                  assert.strictEqual(document.activeElement, btnB);
                  assert.ok(!_mcEditing);

                  // 键盘流：A 的输入框还聚焦时点了 B（Safari 不把焦点给按钮），A 回车提交后焦点直接交给 B 的输入框
                  puts.length = 0;
                  clickLimit(btnA);
                  const inputA2 = inputOf(btnA);
                  inputA2.value = '11';
                  clickLimit(btnB);
                  assert.strictEqual(btnA.nextSibling, inputA2, '点击不打断正在进行的编辑');
                  assert.strictEqual(document.activeElement, inputA2);
                  inputA2.fire('keydown', { key: 'Enter' });
                  return tick().then(() => {
                    assert.deepStrictEqual(puts, ['{"tier":"zhipu","model":"glm-4.6","limit":11}']);
                    assert.strictEqual(document.activeElement, inputOf(btnB));
                    inputOf(btnB).fire('keydown', { key: 'Escape' });

                    // 点了 B 之后焦点又被移到别处（例如开始操作别的控件）：不抢焦点，这次点击作废
                    puts.length = 0;
                    hang();
                    clickLimit(btnA);
                    const inputA3 = inputOf(btnA);
                    inputA3.value = '9';
                    inputA3.fire('keydown', { key: 'Enter' });
                    clickLimit(btnB);
                    const elsewhere = el('button', 'range-btn'); root.appendChild(elsewhere); elsewhere.focus();
                    resolvePut({ ok: true });
                    return tick().then(() => {
                      assert.strictEqual(inputOf(btnB), null, '不抢焦点');
                      assert.strictEqual(_mcPendingEdit, null);
                      assert.strictEqual(document.activeElement, elsewhere);
                      assert.ok(!_mcEditing);

                      // 排队的那一行在当前编辑结束前已不在页面上了：作废，也不把整张表留在「编辑中」
                      clickLimit(btnA);
                      const inputA4 = inputOf(btnA);
                      inputA4.value = '7';
                      clickLimit(btnB);
                      rowB.remove();
                      inputA4.fire('keydown', { key: 'Escape' });
                      assert.strictEqual(inputOf(btnB), null);
                      assert.strictEqual(_mcPendingEdit, null);
                      assert.ok(!_mcEditing);
                      console.log('mc handoff harness OK');
                    });
                  });
                });
                """
            ),
        ),
        "mc handoff harness OK",
    )


# 时间区间与刷新循环的 harness 前置：取数换成可手动放行的假函数（记下每次用的天数），
# 其余全局与真实脚本同形。refreshOverview 里「取回后记下 overviewDays」这一行由静态断言守住
_RANGE_PRELUDE = textwrap.dedent(
    r"""
    let refreshing = false;
    let currentTab = 'overview'; let currentDays = 7; let overviewDays = 0;
    const tabLoaded = { overview: false, sessions: false, config: false };
    const TAB_LABELS = { overview: 'Overview', sessions: 'Sessions', config: 'Config' };
    const now = () => '22:00:23';
    globalThis.window = {};
    const errors = [];
    const console = Object.assign({}, globalThis.console, { error: (...args) => errors.push(args.map(String).join(' ')) });
    const refreshTime = el('div', ''); refreshTime.id = 'refresh-time';
    const title = el('div', 'card-title'); title.id = 'title-vendor-dist';
    const range = el('span', 'trend-range'); range.id = 'trend-range';
    const start = el('input', 'range-date'); start.id = 'range-start';
    const end = el('input', 'range-date'); end.id = 'range-end';
    const custom = el('div', 'range-custom'); custom.id = 'range-custom';
    const fetches = []; let release = null; let failNext = false; let configFetches = 0;
    const refreshSessions = () => Promise.resolve();
    const refreshOverview = async () => {
      const days = currentDays > 0 ? currentDays : 7;
      const mine = {}; release = mine;
      await new Promise((resolve) => { mine.resolve = resolve; });
      if (failNext) { failNext = false; throw new Error('fetch failed'); }
      overviewDays = days;
      updateChartTitles(days);
      fetches.push(days);
    };
    window.ConfigTab = { refresh: async () => { configFetches += 1; if (configFetches > 5) throw new Error('loop'); } };
    """
)


def _range_harness(script: str, body: str) -> str:
    return (
        "const assert = require('assert');\n"
        + _FAKE_DOM
        + _RANGE_PRELUDE
        + "const MAX_RANGE_DAYS = 90;\n"
        + _sources(
            script,
            "function localDateStr(",
            "function rangeLabel(",
            "function setTimeRange(",
            "function applyCustomRange(",
            "function updateChartTitles(",
        )
        # _extract_function 从 function 起截，async 修饰符要自己补回
        + "async "
        + _extract_function(script, "function refresh(")
        + "\n"
        + textwrap.dedent(body)
    )


@pytest.mark.skipif(_NODE is None, reason="node 不可用")
def test_refresh_reruns_when_range_changes_mid_flight(client: TestClient, tmp_path):
    """刷新途中改时间区间：那次 refresh() 被在途标记挡掉，但当前这轮结束后按新区间补刷；取数失败不补刷（否则空转）."""
    script = _inline_script(client.get("/dashboard").text)
    # 标签与摘要读的是这批数据实际取数的天数：取回后先记入 overviewDays，再渲染
    overview = _extract_function(script, "function refreshOverview(")
    assert overview.index("overviewDays = days;") < overview.index(
        "updateKPI(summary, days);"
    )
    assert overview.index("overviewDays = days;") < overview.index(
        "updateChartTitles(days);"
    )
    assert "fetchJSON('/api/dashboard/summary?days=' + days)" in overview
    _run_node(
        tmp_path,
        "range_refresh_harness.js",
        _range_harness(
            script,
            r"""
            // 首轮刷新途中点「近 30 天」：按钮状态立即切换；这次 refresh() 被在途标记挡掉，不另起取数
            const btn30 = el('button', 'range-btn'); root.appendChild(btn30);
            const first = refresh();
            assert.strictEqual(refreshTime.textContent, '刷新中…');
            assert.ok(refreshing);
            setTimeRange(30, btn30);
            assert.ok(btn30.classList.contains('active'));
            assert.deepStrictEqual(fetches, []);
            // 第一轮按旧区间收尾：已上屏的数据属于 7 天，标签跟着数据写「近 7 天」
            release.resolve();
            tick().then(() => {
              assert.deepStrictEqual(fetches, [7]);
              assert.strictEqual(title.textContent, '供应商请求分布（近 7 天）');
              // 发现区间已改：接着按新区间补刷（原实现把这次点击丢掉，数据和按钮各说各话）
              assert.ok(refreshing);
              assert.strictEqual(refreshTime.textContent, '刷新中…');
              release.resolve();
              return first;
            }).then(() => {
              assert.deepStrictEqual(fetches, [7, 30]);
              assert.strictEqual(title.textContent, '供应商请求分布（近 30 天）');
              assert.strictEqual(range.textContent, '近 30 天');
              assert.ok(!refreshing && tabLoaded.overview);
              assert.strictEqual(refreshTime.textContent, '上次刷新: 22:00:23（Overview）');

              // 区间没变：只取一次
              const plain = refresh();
              release.resolve();
              return plain.then(() => {
                assert.deepStrictEqual(fetches, [7, 30, 30]);

                // 取数失败：不补刷（否则接口不可达时会空转），在途标记复位，报错进控制台
                failNext = true;
                const failed = refresh();
                release.resolve();
                return failed.then(() => {
                  assert.deepStrictEqual(fetches, [7, 30, 30]);
                  assert.ok(!refreshing);
                  assert.strictEqual(refreshTime.textContent, '刷新失败 22:00:23');
                  assert.strictEqual(errors.length, 1);

                  // 取数失败、且途中又改了区间：同样不补刷
                  const failedToo = refresh();
                  setTimeRange(12, null);
                  failNext = true;
                  release.resolve();
                  return failedToo.then(() => {
                    assert.deepStrictEqual(fetches, [7, 30, 30]);
                    assert.strictEqual(errors.length, 2);
                    assert.strictEqual(currentDays, 12, '按钮状态保留用户的最后一次选择');

                    // 下一次刷新（手动 ⟳ 或 10 分钟轮询）按最后选的区间取数
                    const next = refresh();
                    release.resolve();
                    return next.then(() => {
                      assert.deepStrictEqual(fetches, [7, 30, 30, 12]);
                      assert.strictEqual(title.textContent, '供应商请求分布（近 12 天）');

                      // 不在 Overview 页签时区间对不上也不循环：只刷新当前页签一次
                      currentDays = 30;
                      currentTab = 'config';
                      return refresh().then(() => {
                        assert.strictEqual(configFetches, 1);
                        assert.deepStrictEqual(fetches, [7, 30, 30, 12]);
                        currentTab = 'overview';
                        console.log('range refresh harness OK');
                      });
                    });
                  });
                });
              });
            }).catch((e) => { process.stderr.write(String((e && e.stack) || e) + '\n'); process.exit(1); });
""",
        ),
        "range refresh harness OK",
    )


@pytest.mark.skipif(_NODE is None, reason="node 不可用")
def test_custom_range_defaults_to_local_dates(client: TestClient, tmp_path):
    """自选区间默认带出本地今天的近 7 天：东八区 0 到 8 点不再把截止日写成 UTC 的昨天；天数超过接口上限按上限取."""
    script = _inline_script(client.get("/dashboard").text)
    assert "toISOString" not in _extract_function(script, "function setTimeRange(")
    _run_node(
        tmp_path,
        "range_custom_harness.js",
        _range_harness(
            script,
            r"""
            process.env.TZ = 'Asia/Shanghai';
            const RealDate = globalThis.Date;
            const fakeNow = new RealDate(2026, 10, 27, 1, 0);   // 本地 11-27 凌晨 1 点，UTC 还是 11-26
            globalThis.Date = class extends RealDate { constructor(...args) { super(...(args.length ? args : [fakeNow])); } };
            assert.strictEqual(new Date().toISOString().slice(0, 10), '2026-11-26', '前置：进程时区已切到东八区');

            const btnCustom = el('button', 'range-btn'); root.appendChild(btnCustom);
            setTimeRange(0, btnCustom);
            assert.ok(custom.classList.contains('visible') && btnCustom.classList.contains('active'));
            assert.deepStrictEqual([start.value, end.value], ['2026-11-21', '2026-11-27'], '起止日期是本地日期');
            assert.strictEqual(currentDays, 7);
            release.resolve();
            tick().then(() => {
              assert.deepStrictEqual(fetches, [7]);
              assert.strictEqual(title.textContent, '供应商请求分布（近 7 天）');

              // 超过接口上限（90 天）的自选区间：按 90 天取数，标签随之
              end.value = '2026-11-27'; start.value = '2026-01-01';
              applyCustomRange();
              assert.strictEqual(currentDays, 90);
              release.resolve();
              return tick();
            }).then(() => {
              assert.deepStrictEqual(fetches, [7, 90]);
              assert.strictEqual(range.textContent, '近 90 天');

              // 点回预设按钮：自选面板收起，按预设天数取数
              const btn30 = el('button', 'range-btn'); root.appendChild(btn30);
              setTimeRange(30, btn30);
              assert.ok(!custom.classList.contains('visible'));
              release.resolve();
              return tick();
            }).then(() => {
              assert.deepStrictEqual(fetches, [7, 90, 30]);
              assert.strictEqual(title.textContent, '供应商请求分布（近 30 天）');
              console.log('range custom harness OK');
            }).catch((e) => { process.stderr.write(String((e && e.stack) || e) + '\n'); process.exit(1); });
            """,
        ),
        "range custom harness OK",
    )


@pytest.mark.skipif(_NODE is None, reason="node 不可用")
def test_chart_summaries_and_dist_legend_position(client: TestClient, tmp_path):
    """图形读屏读不到：canvas 带 role=img 与一句文字摘要（区间、合计、峰值 / 各项占比）；分布图图例在窄列移到下方."""
    script = _inline_script(client.get("/dashboard").text)
    for builder, name in (
        ("buildTimeline", "请求量趋势"),
        ("buildTokenTimeline", "Token 量趋势（按供应商）"),
        ("buildModelTokenTimeline", "Token 用量趋势（Vendor / 模型）"),
    ):
        source = _extract_function(script, f"function {builder}(")
        assert f"setChartSummary(ctx.canvas, trendSummary('{name}'," in source
        assert source.index("chartEmpty(ctx);") < source.index(
            "setChartSummary("
        )  # 空态不写摘要
    dist = _extract_function(script, "function buildVendorDist(")
    assert (
        "position: distLegendPosition(ctx, legendLabels, ctx.canvas.parentElement.clientWidth),"
        in dist
    )
    assert "onResize: function (chart, size) {" in dist
    assert (
        "chart.options.plugins.legend.position = distLegendPosition(chart.ctx, chart.data.labels, size.width);"
        in dist
    )
    # 图例宽度按 COMMON_LEGEND.labels 的色点、内边距与字体量出来：harness 用脚本里的同一份配置
    legend_labels = _extract_block(
        script, "const COMMON_LEGEND = {", "font: { size: 13, weight: '500' }"
    )
    assert "boxWidth: 10" in legend_labels and "padding: 14" in legend_labels
    assert "供应商请求分布，" in dist

    _run_node(
        tmp_path,
        "chart_summary_harness.js",
        "const assert = require('assert');\n"
        + _FAKE_DOM
        + "let overviewDays = 30;\n"
        + legend_labels
        + "\n}};\n"
        + _sources(
            script,
            "function setChartSummary(",
            "function trendSummary(",
            "function rangeLabel(",
            "function fmtTokens(",
            "function fmtNum(",
            "function distLegendPosition(",
        )
        + textwrap.dedent(
            r"""
            const canvas = new FakeEl('canvas');
            const dates = ['2026-11-23', '2026-11-24', '2026-11-25'];
            const datasets = [
              { data: [1000, 0, 2500] },
              { data: [500, 250, null] },   // 缺值按 0 计
              { data: [0, undefined, 500] },
            ];
            assert.strictEqual(canvas.getAttribute('role'), null);
            setChartSummary(canvas, trendSummary('请求量趋势', fmtNum, ' 次', dates, datasets));
            assert.strictEqual(canvas.getAttribute('role'), 'img');
            // 区间取最近一次取数的天数；合计与单日最高都按所有系列逐日相加
            assert.strictEqual(
              canvas.getAttribute('aria-label'),
              '请求量趋势，近 30 天：合计 4,750 次，单日最高 3,000 次（2026-11-25）',
            );
            // 覆盖写入，不残留上一次的摘要
            setChartSummary(canvas, trendSummary('Token 量趋势（按供应商）', fmtTokens, ' Token', dates.slice(0, 1), datasets));
            assert.strictEqual(
              canvas.getAttribute('aria-label'),
              'Token 量趋势（按供应商），近 30 天：合计 1.5K Token，单日最高 1.5K Token（2026-11-23）',
            );

            // 分布图图例：右侧图例最多占一半画布宽。宽度用图例自己的字体量：色点 + 字号的一半 + 文字宽。
            // 假字体每个字符 6px：17 个字符的「agy | antigravity」一项 118.5px，连内边距占 132.5px，画布不到 265px 就移到下方
            const fonts = [];
            const ctx = {
              canvas: { parentElement: { clientWidth: 0 } },
              save() {}, restore() {},
              set font(v) { fonts.push(v); },
              measureText: (text) => ({ width: text.length * 6 }),
            };
            globalThis.Chart = { helpers: { toFont: (o) => Object.assign({}, o, { string: o.weight + ' ' + o.size + 'px fake' }) } };
            const labels = ['cc | zhipu', 'agy | antigravity'];
            assert.strictEqual(distLegendPosition(ctx, labels, 265), 'right');
            assert.strictEqual(distLegendPosition(ctx, labels, 264.9), 'bottom');
            assert.strictEqual(distLegendPosition(ctx, labels, 233), 'bottom');
            // 标签短时更窄也放得下；没有标签不报错
            assert.strictEqual(distLegendPosition(ctx, ['cc | zhipu'], 181), 'right');
            assert.strictEqual(distLegendPosition(ctx, ['cc | zhipu'], 180), 'bottom');
            assert.strictEqual(distLegendPosition(ctx, [], 233), 'right');
            assert.deepStrictEqual(fonts, Array(6).fill('500 13px fake'));
            console.log('chart summary harness OK');
            """
        ),
        "chart summary harness OK",
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
