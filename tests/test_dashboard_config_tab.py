"""Dashboard Config 页：HTML 接线、静态资源、CSP、XSS 汇点静态检查与 config.js 纯函数（Node）."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import textwrap
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from coding.proxy.server import dashboard
from coding.proxy.server.dashboard import register_dashboard_routes

_STATIC = Path(dashboard.__file__).parent / "static" / "config"
_CONFIG_JS = _STATIC / "config.js"
_CONFIG_CSS = _STATIC / "config.css"


@pytest.fixture
def client() -> TestClient:
    app = FastAPI()
    register_dashboard_routes(app)
    return TestClient(app, base_url="http://127.0.0.1:3392")


def _asset_urls(html: str) -> dict[str, str]:
    return {
        Path(m.split("?")[0]).name: m
        for m in re.findall(
            r'"(/static/config/config\.(?:js|css)\?v=[0-9a-f]{8})"', html
        )
    }


# ── HTML 接线 ─────────────────────────────────────────────────


def test_dashboard_wires_config_tab(client: TestClient):
    html = client.get("/dashboard").text
    assert 'id="tab-btn-config"' in html and "switchTab('config')" in html
    assert 'id="tab-pane-config"' in html and 'id="config-root"' in html
    assert "const TABS = ['overview', 'sessions', 'config'];" in html
    assert "config: 'Config'" in html
    assert "window.ConfigTab.refresh(false)" in html
    # Config 页不参与 10 分钟定时刷新
    assert "if (currentTab !== 'config') refresh();" in html
    # 模板占位符已全部替换
    assert "{{" not in html


def test_tab_state_declared_before_chart_usage(client: TestClient):
    """页签状态位于 Chart.js 调用之前，CDN 不可达时不陷入 TDZ."""
    html = client.get("/dashboard").text
    script = html[html.index("<script>\n") :]
    assert script.index("let currentTab") < script.index("Chart.defaults")
    assert "if (window.Chart) {\n  Chart.defaults.color" in script
    assert "if (!window.Chart) return;" in script


def test_config_assets_load_before_inline_script(client: TestClient):
    html = client.get("/dashboard").text
    urls = _asset_urls(html)
    assert set(urls) == {"config.js", "config.css"}
    assert html.index(urls["config.js"]) < html.index("<script>\n")
    defaults = re.search(
        r'<script type="application/json" id="config-schema-defaults">(.*?)</script>',
        html,
        re.S,
    )
    payload = json.loads(defaults.group(1))
    assert payload["fields"]["vendors.*.timeout_ms"] == 300000
    assert "vendors.*.circuit_breaker" in payload["groups"]


def test_asset_version_is_content_hash(client: TestClient):
    import hashlib

    urls = _asset_urls(client.get("/dashboard").text)
    for name, path in (("config.js", _CONFIG_JS), ("config.css", _CONFIG_CSS)):
        digest = hashlib.sha256(path.read_bytes()).hexdigest()[:8]
        assert urls[name].endswith(f"?v={digest}")


# ── 响应头 / 静态资源 ─────────────────────────────────────────


def test_dashboard_csp_and_chart_sri(client: TestClient):
    resp = client.get("/dashboard")
    csp = resp.headers["content-security-policy"]
    for directive in (
        "default-src 'self'",
        "script-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net",
        "connect-src 'self'",
        "frame-ancestors 'none'",
        "object-src 'none'",
        "base-uri 'none'",
    ):
        assert directive in csp
    assert resp.headers["cache-control"] == "no-cache"
    tag = re.search(r"<script src=\"https://cdn\.jsdelivr\.net[^>]+>", resp.text).group(
        0
    )
    assert "chart.js@4.4.3/dist/chart.umd.js" in tag
    assert 'integrity="sha384-' in tag and 'crossorigin="anonymous"' in tag


@pytest.mark.parametrize(
    ("name", "mime"), [("config.js", "javascript"), ("config.css", "text/css")]
)
def test_static_assets_served(client: TestClient, name: str, mime: str):
    url = _asset_urls(client.get("/dashboard").text)[name]
    resp = client.get(url)
    assert resp.status_code == 200
    assert mime in resp.headers["content-type"]
    assert resp.headers["cache-control"] == "no-cache"
    assert resp.headers["x-content-type-options"] == "nosniff"
    etag = resp.headers["etag"]
    assert client.get(url, headers={"if-none-match": etag}).status_code == 304


def test_static_rejects_traversal_and_unknown(client: TestClient):
    assert client.get("/static/config/missing.js").status_code == 404
    assert client.get("/static/../dashboard.py").status_code == 404
    assert client.get("/static/config/%2e%2e/%2e%2e/dashboard.py").status_code == 404


def test_register_dashboard_routes_idempotent():
    app = FastAPI()
    register_dashboard_routes(app)
    register_dashboard_routes(app)
    assert sum(getattr(r, "path", None) == "/static" for r in app.routes) == 1
    assert sum(getattr(r, "path", None) == "/dashboard" for r in app.routes) == 1


# ── XSS 汇点（静态检查） ─────────────────────────────────────


def test_config_js_builds_dom_without_html_sinks():
    src = _CONFIG_JS.read_text(encoding="utf-8")
    for sink in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write"):
        assert sink not in src, sink
    assert not re.search(r"\beval\(|new Function\(", src)
    assert "X-Coding-Proxy-Admin" in src
    assert "coding-proxy.admin-token" in src


def test_dashboard_xss_sinks_fixed(client: TestClient):
    html = client.get("/dashboard").text
    # 图表外部 tooltip 改为 DOM 构建
    assert "el.innerHTML = html;" not in html
    assert "label.textContent = dp.dataset.label" in html
    # 供应商名经转义
    assert '<span class="vendor-name">${safeName}</span>' in html
    assert 'data-vendor="${safeName}"' in html
    assert "${tier.name}" not in html


def _width_tokens(html: str) -> dict[str, int]:
    return {k: int(v) for k, v in re.findall(r"--col-([a-z]+): (\d+)px;", html)}


def test_sessions_table_uses_fixed_width_tokens(client: TestClient):
    html = client.get("/dashboard").text
    colgroup = html[html.index('<table class="session-table">') :]
    colgroup = colgroup[: colgroup.index("</colgroup>")]
    assert "%" not in colgroup
    assert colgroup.count("width:var(--col-") == 11
    # 合计 = main 内容区 1328px（1440 - 2×32 padding - 2×24 card padding）
    tokens = _width_tokens(html)
    cols = re.findall(r"width:var\(--col-([a-z]+)\)", colgroup)
    assert sum(tokens[c] for c in cols) == 1328


# 大写 + letter-spacing 表头的浏览器实测宽（含左右 padding，1286px 视口，Chrome）；
# token 不得小于表头宽，否则表头本身即被截断
_HEADER_MIN_PX = {
    # Sessions（padding 12px×2，letter-spacing .5px）
    "session": 98,
    "title": 61,
    "time": 109,
    "count": 94,
    "models": 79,
    "vendors": 87,
    "latency": 114,
    "success": 86,
    "client": 71,
    # Config（padding 10px×2，letter-spacing .4px）
    "order": 81,
    "bool": 79,
    "vendor": 114,
    "state": 80,
    "name": 58,
    "model": 66,
    "pattern": 79,
    "target": 71,
    "list": 121,  # VENDOR SCOPE
    "currency": 91,
    "price": 81,
    "actions": 112,  # 两个操作按钮（编辑 + 删除）
}


def test_width_tokens_fit_headers(client: TestClient):
    tokens = _width_tokens(client.get("/dashboard").text)
    assert set(_HEADER_MIN_PX) <= set(tokens)
    too_narrow = {k: (tokens[k], m) for k, m in _HEADER_MIN_PX.items() if tokens[k] < m}
    assert not too_narrow


def test_same_header_same_width_token(client: TestClient):
    """Sessions 与 Config 表格中同名表头（同一属性）使用同一列宽 token."""
    html = client.get("/dashboard").text
    table = html[html.index('<table class="session-table">') :]
    table = table[: table.index("</thead>")]
    heads = re.findall(r"<th>([^<]+)</th>", table)
    tokens = re.findall(r"width:var\(--col-([a-z]+)\)", table)
    assert len(heads) == len(tokens) == 11
    by_label: dict[str, set[str]] = {}
    for label, token in zip(heads, tokens, strict=True):
        by_label.setdefault(label, set()).add(token)
    src = _CONFIG_JS.read_text(encoding="utf-8")
    for label, token in re.findall(r"label: '([^']+)', w: '--col-([a-z]+)'", src):
        by_label.setdefault(label, set()).add(token)
    assert {k: v for k, v in by_label.items() if len(v) > 1} == {}


def test_time_range_reset_scoped_to_its_bar(client: TestClient):
    """setTimeRange 只复位时间区间按钮，不清除 Config section pills（同用 .range-btn）的高亮."""
    html = client.get("/dashboard").text
    script = html[html.index("<script>\n") :]
    assert "document.querySelectorAll('.time-range-bar .range-btn')" in script
    assert not re.search(r"querySelectorAll\('\.range-btn'\)", script)


def test_registry_texts_are_chinese_and_informative():
    from coding.proxy.config.ui_registry import REGISTRY

    cjk = re.compile(r"[\u4e00-\u9fff]")
    assert [s.path for s in REGISTRY if not cjk.search(s.description)] == []
    # pill tooltip 为「未设置：<unset_hint>」，hint 须说明未设置的后果
    hints = {s.path: s.unset_hint for s in REGISTRY if s.unset_hint}
    assert {
        p: h for p, h in hints.items() if h in ("未设置", "") or not cjk.search(h)
    } == {}


def test_sessions_cells_get_truncation_tooltip(client: TestClient):
    """溢出的 Sessions 数据单元格经 truncTitle 补 tooltip；标签型单元格自带完整 title."""
    html = client.get("/dashboard").text
    src = _CONFIG_JS.read_text(encoding="utf-8")
    assert ".session-table tr[data-row] > td:not(.cell-bind)" in src
    assert '<td class="cell-bind" onclick="event.stopPropagation()">' in html
    assert "'<td title=\"' + escapeHtml(clientsFull.join(', ')) + '\">'" in html


# ── config.js 纯函数（Node） ─────────────────────────────────

_NODE = shutil.which("node")

_NODE_HARNESS = textwrap.dedent(
    r"""
    const assert = require('assert');
    const h = require(process.argv[2]);

    // 币种解析 / 格式化
    assert.deepStrictEqual(h.parsePrice('¥8.00'), { currency: 'CNY', amount: '8.00' });
    assert.deepStrictEqual(h.parsePrice('$ 5'), { currency: 'USD', amount: '5' });
    assert.deepStrictEqual(h.parsePrice(0.5), { currency: null, amount: '0.5' });
    assert.strictEqual(h.priceText('USD', '5'), '$5.0');
    assert.strictEqual(h.priceText('CNY', '8.00'), '¥8.00');
    const row = { vendor: 'zhipu', model: 'glm', input_cost_per_mtok: 0,
                  output_cost_per_mtok: '¥2.5', cache_write_cost_per_mtok: '¥0',
                  cache_read_cost_per_mtok: '¥0.1' };
    assert.strictEqual(h.entryCurrency(row), 'CNY');
    assert.strictEqual(h.entryCurrency({ input_cost_per_mtok: 3 }), 'USD');
    const usd = h.withCurrency(row, 'USD');
    assert.strictEqual(usd.input_cost_per_mtok, '$0.0');
    assert.strictEqual(usd.output_cost_per_mtok, '$2.5');
    assert.strictEqual(h.entryCurrency(h.withCurrency(usd, 'CNY')), 'CNY');

    // loc → data-path
    assert.strictEqual(h.locToPath(['vendors', 'zhipu', 'timeout_ms']), 'vendors.zhipu.timeout_ms');
    assert.strictEqual(h.locToPath(['secret_ops', 'vendors.zhipu.api_key']), 'vendors.zhipu.api_key');
    assert.strictEqual(h.locToPath(['model_mapping', 2, 'pattern']), 'model_mapping.2.pattern');
    assert.strictEqual(h.locToPath([]), '');
    assert.deepStrictEqual(h.pathCandidates('pricing.3.model'), ['pricing.3.model', 'pricing.3', 'pricing']);
    assert.strictEqual(h.pillOf('vendors.zhipu.api_key'), 'vendors');
    assert.strictEqual(h.pillOf('server.port'), 'system');

    // dirty / 字段计数 / 载荷
    const base = {
      vendors: [{ vendor: 'anthropic', enabled: true, timeout_ms: 300000 },
                { vendor: 'zhipu', enabled: true, circuit_breaker: { failure_threshold: 3 } }],
      tiers: null, failover: { status_codes: [429, 503] }, model_mapping: [],
      pricing: [], native_api: {}, session_policies: { policies: [] },
    };
    const work = h.clone(base);
    assert.deepStrictEqual(h.dirtySections(base, work), []);
    work.vendors[1].circuit_breaker.failure_threshold = 5;
    work.vendors[0].timeout_ms = 1000;
    work.failover.status_codes = [429];
    assert.deepStrictEqual(h.dirtySections(base, work), ['vendors', 'failover']);
    const ops = { 'vendors.zhipu.api_key': { op: 'set', value: 'k' },
                  'vendors.copilot.github_token': { op: 'keep' } };
    assert.strictEqual(h.countChangedFields(base, work, ops), 4);
    const payload = h.buildPayload({ vendors: 3, failover: 0, tiers: 1 }, base, work, ops);
    assert.deepStrictEqual(Object.keys(payload.sections).sort(), ['failover', 'vendors']);
    assert.deepStrictEqual(payload.revisions, { vendors: 3, failover: 0 });
    assert.deepStrictEqual(payload.secret_ops, { 'vendors.zhipu.api_key': { op: 'set', value: 'k' } });
    // 仅顺序变化也计为 1 个字段
    const reordered = h.clone(base);
    reordered.vendors.reverse();
    assert.strictEqual(h.countChangedFields(base, reordered, {}), 1);

    // 改回缺省值：base 未显式设置该键 → 删除键，dirty 清除
    const b2 = { vendor: 'kimi' }, w2 = { vendor: 'kimi' };
    h.assignField(w2, b2, ['quota_guard', 'enabled'], true, false);
    assert.ok(!h.deepEqual(b2, w2));
    h.assignField(w2, b2, ['quota_guard', 'enabled'], false, false);
    assert.deepStrictEqual(w2, b2);

    // 目标地址变化 → 预测清空的密钥（仅计入有存储记录 recorded 的条目；显式 op 除外）
    const cleared = h.predictClearedSecrets({
      base: { vendors: [{ vendor: 'zhipu', base_url: 'https://a' }] },
      work: { vendors: [{ vendor: 'zhipu', base_url: 'https://b' },
                        { vendor: 'kimi', base_url: 'https://k2' }] },
      defaults: { vendors: [{ vendor: 'kimi', base_url: 'https://k' }] },
      secrets: { 'vendors.zhipu.api_key': { kind: 'stored', recorded: true },
                 'vendors.kimi.api_key': { kind: 'env', recorded: false } },
      ops: {}, secretFields: { zhipu: ['api_key'], kimi: ['api_key'] },
      targetDefaults: { base_url: '', token_url: 'https://t' },
    });
    assert.deepStrictEqual(cleared, ['vendors.zhipu.api_key']);

    // 新建覆盖 + 目标地址变化：服务端会物化默认 env 记录再清空，确认框需预判（gate F-3）
    const cleared2 = h.predictClearedSecrets({
      base: { vendors: [] },
      work: { vendors: [{ vendor: 'minimax', base_url: 'https://m2' }] },
      defaults: { vendors: [{ vendor: 'minimax', base_url: 'https://m' }] },
      secrets: { 'vendors.minimax.api_key': { kind: 'env', env_var: 'MINIMAX_API_KEY', recorded: false, default_env: 'MINIMAX_API_KEY' } },
      ops: {}, secretFields: { minimax: ['api_key'] },
      targetDefaults: { base_url: '', token_url: 'https://t' },
    });
    assert.deepStrictEqual(cleared2, ['vendors.minimax.api_key']);
    // 未新建覆盖（沿用默认 vendor dict 仅动 base_url 视为覆盖）但无 default_env 的不预判
    const cleared3 = h.predictClearedSecrets({
      base: { vendors: [{ vendor: 'kimi', base_url: 'https://k' }] },
      work: { vendors: [{ vendor: 'kimi', base_url: 'https://k2' }] },
      defaults: { vendors: [{ vendor: 'kimi', base_url: 'https://k' }] },
      secrets: { 'vendors.kimi.api_key': { kind: 'default', recorded: false } },
      ops: {}, secretFields: { kimi: ['api_key'] },
      targetDefaults: { base_url: '', token_url: 'https://t' },
    });
    assert.deepStrictEqual(cleared3, []);
    // 自定义 URL + stored 密钥 → 改回包内默认 URL：服务端保留，不预判清空（gate F-2）
    const cleared4 = h.predictClearedSecrets({
      base: { vendors: [{ vendor: 'zhipu', base_url: 'https://custom.example' }] },
      work: { vendors: [{ vendor: 'zhipu', base_url: 'https://open.bigmodel.cn/api/anthropic' }] },
      defaults: { vendors: [{ vendor: 'zhipu', base_url: 'https://open.bigmodel.cn/api/anthropic' }] },
      secrets: { 'vendors.zhipu.api_key': { kind: 'stored', recorded: true } },
      ops: {}, secretFields: { zhipu: ['api_key'] },
      targetDefaults: { base_url: '', token_url: 'https://t' },
    });
    assert.deepStrictEqual(cleared4, []);
    // pinned empty 记录（曾覆盖后移除）：服务端不清空（前后保持空串），不预判（gate F-1）
    const cleared5 = h.predictClearedSecrets({
      base: { vendors: [] },
      work: { vendors: [{ vendor: 'zhipu', base_url: 'https://custom.example' }] },
      defaults: { vendors: [{ vendor: 'zhipu', base_url: 'https://open.bigmodel.cn/api/anthropic' }] },
      secrets: { 'vendors.zhipu.api_key': { kind: 'empty', recorded: false, default_env: 'ZHIPU_API_KEY' } },
      ops: {}, secretFields: { zhipu: ['api_key'] },
      targetDefaults: { base_url: '', token_url: 'https://t' },
    });
    assert.deepStrictEqual(cleared5, []);

    // move 后展开行的新下标：被移动行跟随自身，夹在区间内的其余行 ∓1 平移
    assert.strictEqual(h.movedOpenIndex(1, 0, 1), 0);   // [A,B,C] move(0,1) → [B,A,C]，B 1→0
    assert.strictEqual(h.movedOpenIndex(1, 0, 2), 0);   // [A,B,C] move(0,2) → [B,C,A]，B 1→0
    assert.strictEqual(h.movedOpenIndex(1, 2, 0), 2);   // [A,B,C] move(2,0) → [C,A,B]，B 1→2
    assert.strictEqual(h.movedOpenIndex(0, 1, 2), 0);   // 区间外不动
    assert.strictEqual(h.movedOpenIndex(2, 0, 1), 2);   // 区间外不动
    assert.strictEqual(h.movedOpenIndex(null, 0, 1), null);

    // tiers 链路 / 定价默认新增行 / token / 模板节点
    assert.deepStrictEqual(h.effectiveChain(null, [{ vendor: 'a' }, { vendor: 'b', enabled: false }]), ['a']);
    assert.deepStrictEqual(h.effectiveChain(['b'], []), ['b']);
    assert.strictEqual(h.newDefaultRows([{ vendor: 'a', model: 'x' }],
      [{ vendor: 'a', model: 'x' }, { vendor: 'a', model: 'y' }]).length, 1);
    // 可合并默认行：仅 pricing 已覆盖时；以已保存表为基准，未保存的删除不计入，已合并的不再提示
    const dflt = [{ vendor: 'a', model: 'x' }, { vendor: 'a', model: 'y' }];
    const saved = [{ vendor: 'a', model: 'x' }];
    assert.deepStrictEqual(h.pricingMergeRows([], dflt, [], dflt), []);
    assert.deepStrictEqual(h.pricingMergeRows(['pricing'], saved, saved, dflt), [dflt[1]]);
    assert.deepStrictEqual(h.pricingMergeRows(['pricing'], saved, [], dflt), [dflt[1]]);
    assert.deepStrictEqual(h.pricingMergeRows(['pricing'], saved, dflt, dflt), []);
    assert.ok(h.validToken('A'.repeat(32)) && !h.validToken('short') && !h.validToken('a b'.repeat(16)));
    assert.ok(h.isTmpl({ $tmpl: '${X}' }) && !h.isTmpl({ $tmpl: 1 }));

    // 保存后果清单：无后果为空（不弹确认框）；在途请求、密钥、地址变更、熔断、移除 vendor 各自成条
    const view0 = { running: { tiers: ['a'], boot_revisions: {} }, revisions: {} };
    const plain = { sections: { failover: {} }, secret_ops: {}, revisions: {} };
    assert.deepStrictEqual(h.saveImpact({ payload: plain, base: {}, work: {}, view: view0, status: { in_flight: 0, tiers: [] }, cleared: [], chain: ['a'] }), []);
    const codes = o => h.saveImpact(o).map(x => x.code);
    assert.deepStrictEqual(codes({ payload: plain, base: {}, work: {}, view: view0, status: { in_flight: 2, tiers: [] }, cleared: [], chain: ['a'] }), ['inflight']);
    const sec = { sections: {}, secret_ops: { 'vendors.zhipu.api_key': { op: 'clear' } }, revisions: {} };
    const it = h.saveImpact({ payload: sec, base: {}, work: {}, view: view0, status: null, cleared: [], chain: ['a'] });
    assert.deepStrictEqual(it.map(x => [x.code, x.danger]), [['secrets', true]]);
    assert.ok(h.saveImpact({ payload: plain, base: {}, work: {}, view: view0, status: null, cleared: ['vendors.zhipu.api_key'], chain: ['a'] })[0].danger);
    assert.deepStrictEqual(codes({ payload: plain, base: {}, work: {}, view: view0, status: { tiers: [{ name: 'a', circuit_breaker: { state: 'open' } }] }, cleared: [], chain: ['a'] }), ['breaker']);
    const vpay = { sections: { vendors: [] }, secret_ops: {}, revisions: {} };
    assert.ok(codes({ payload: vpay, base: { vendors: [{ vendor: 'kimi' }] }, work: { vendors: [] }, view: view0, status: null, cleared: [], chain: ['a'] }).includes('removed'));
    // 字段级 diff：vendors 按名配对、pricing 按 vendor + model 配对，未配上的行记为新增 / 移除
    const lab = { label: p => p.split('.').pop(), skip: () => false, sectionLabel: s => s, rowTitle: (s, r) => r.vendor + '/' + r.model };
    const g = h.changedRows(
      { vendors: [{ vendor: 'zhipu', timeout_ms: 1 }], pricing: [{ vendor: 'a', model: 'x' }, { vendor: 'a', model: 'y' }] },
      { vendors: [{ vendor: 'zhipu', timeout_ms: 2 }], pricing: [{ vendor: 'a', model: 'y' }] }, lab);
    assert.deepStrictEqual(g.map(x => x.title), ['vendors › zhipu', 'pricing']);
    assert.deepStrictEqual([g[0].rows[0].label, g[0].rows[0].before, g[0].rows[0].after], ['timeout_ms', '1', '2']);
    assert.deepStrictEqual(g[1].rows.map(r => [r.label, !!r.removed]), [['a/x', true]]);
    console.log('ok');
    """
)


def _run_node(tmp_path: Path, source: str) -> None:
    harness = tmp_path / "harness.js"
    harness.write_text(source, encoding="utf-8")
    result = subprocess.run(
        [_NODE, str(harness), str(_CONFIG_JS)],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "ok"


@pytest.mark.skipif(_NODE is None, reason="node 不可用")
def test_config_js_pure_helpers_under_node(tmp_path: Path):
    _run_node(tmp_path, _NODE_HARNESS)


# ── config.js 渲染（Node + 最小 DOM shim） ───────────────────

_DOM_HARNESS = textwrap.dedent(
    r"""
    const assert = require('assert');
    const fs = require('fs');
    const vm = require('vm');

    // 最小 DOM：仅覆盖 config.js 实际用到的 API；选择器支持 tag / #id / .class / [attr] /
    // [attr="v"] / :not(...) 与后代组合
    class ClassList {
      constructor(el) { this.el = el; }
      _get() { return (this.el.className || '').split(/\s+/).filter(Boolean); }
      _set(a) { this.el.className = a.join(' '); }
      contains(c) { return this._get().includes(c); }
      add(c) { const a = this._get(); if (!a.includes(c)) { a.push(c); this._set(a); } }
      remove(c) { this._set(this._get().filter(x => x !== c)); }
      toggle(c, on) { on = on === undefined ? !this.contains(c) : !!on; on ? this.add(c) : this.remove(c); return on; }
    }
    class Node {
      constructor(tag) {
        this.tagName = tag ? tag.toUpperCase() : '#text';
        this.childNodes = []; this.parentNode = null; this.attrs = {}; this.className = '';
        this.listeners = {}; this.style = {}; this._text = ''; this.classList = new ClassList(this);
      }
      get id() { return this.attrs.id || ''; }
      set id(v) { this.attrs.id = String(v); }
      get children() { return this.childNodes.filter(n => n.tagName !== '#text'); }
      get firstChild() { return this.childNodes[0] || null; }
      get nextElementSibling() {
        const sib = this.parentNode ? this.parentNode.children : [];
        return sib[sib.indexOf(this) + 1] || null;
      }
      get textContent() { return this.tagName === '#text' ? this._text : this.childNodes.map(n => n.textContent).join(''); }
      set textContent(v) { this.childNodes = []; if (this.tagName === '#text') this._text = String(v); else if (v !== '') this.appendChild(doc.createTextNode(v)); }
      appendChild(n) { if (n.parentNode) n.parentNode.removeChild(n); n.parentNode = this; this.childNodes.push(n); return n; }
      insertBefore(n, ref) {
        if (n.parentNode) n.parentNode.removeChild(n);
        const i = ref ? this.childNodes.indexOf(ref) : -1;
        n.parentNode = this; i < 0 ? this.childNodes.push(n) : this.childNodes.splice(i, 0, n); return n;
      }
      removeChild(n) { this.childNodes = this.childNodes.filter(x => x !== n); n.parentNode = null; return n; }
      remove() { if (this.parentNode) this.parentNode.removeChild(this); }
      setAttribute(k, v) { if (k === 'class') this.className = String(v); else this.attrs[k] = String(v); }
      getAttribute(k) { if (k === 'class') return this.className || null; return k in this.attrs ? this.attrs[k] : null; }
      hasAttribute(k) { return this.getAttribute(k) !== null; }
      removeAttribute(k) { delete this.attrs[k]; }
      get title() { return this.attrs.title || ''; }
      set title(v) { this.attrs.title = String(v); }
      get htmlFor() { return this.attrs.for || ''; }
      addEventListener(t, f) { (this.listeners[t] = this.listeners[t] || []).push(f); }
      dispatch(t, extra) {
        // 与浏览器一致：inert 子树不接收用户输入事件
        for (let n = this; n; n = n.parentNode) if (n.attrs && 'inert' in n.attrs) return;
        const e = Object.assign({ type: t, target: this, preventDefault() {}, stopPropagation() {} }, extra || {});
        for (let n = this; n; n = n.parentNode) (n.listeners[t] || []).forEach(f => f(Object.assign(e, { currentTarget: n })));
      }
      click() { if (!this.disabled) this.dispatch('click'); }
      focus() { doc.activeElement = this; }
      scrollIntoView() {}
      showModal() { this.open = true; }
      close() { this.open = false; }
      _walk(out) { this.children.forEach(c => { out.push(c); c._walk(out); }); return out; }
      querySelectorAll(sel) {
        const all = this._walk([]);
        return sel.split(',').map(s => s.trim()).reduce((acc, s) => acc.concat(all.filter(el => matches(el, s, this))), [])
          .filter((el, i, a) => a.indexOf(el) === i);
      }
      querySelector(sel) { return this.querySelectorAll(sel)[0] || null; }
      closest(sel) { for (let n = this; n && n.tagName !== '#document'; n = n.parentNode) if (sel.split(',').some(s => matches(n, s.trim()))) return n; return null; }
    }
    function simple(el, s) {
      if (!el || el.tagName === '#text' || el.tagName === '#document') return false;
      const not = /:not\((.*)\)$/.exec(s);
      if (not) { if (simple(el, not[1])) return false; s = s.slice(0, not.index); }
      const re = /([#.]?[\w-]+)|\[([\w-]+)(?:="([^"]*)")?\]/g; let m;
      while ((m = re.exec(s))) {
        if (m[2]) { const v = el.getAttribute(m[2]); if (v === null || (m[3] !== undefined && v !== m[3])) return false; continue; }
        const t = m[1];
        if (t[0] === '#') { if (el.id !== t.slice(1)) return false; }
        else if (t[0] === '.') { if (!el.classList.contains(t.slice(1))) return false; }
        else if (el.tagName !== t.toUpperCase()) return false;
      }
      return true;
    }
    function matches(el, sel, scope) {
      const parts = sel.replace(/\s*>\s*/g, ' > ').split(/\s+(?![^(]*\))/).filter(Boolean);
      const go = (node, i) => {
        if (!simple(node, parts[i])) return false;
        if (i === 0) return true;
        if (parts[i - 1] === '>') return go(node.parentNode, i - 2);
        for (let p = node.parentNode; p && p !== scope; p = p.parentNode) if (go(p, i - 1)) return true;
        return false;
      };
      return go(el, parts.length - 1);
    }
    const doc = new Node('#document');
    doc.tagName = '#document';
    doc.createElement = t => new Node(t);
    doc.createTextNode = t => { const n = new Node(); n._text = String(t); return n; };
    doc.getElementById = id => doc.querySelector('#' + id);
    doc.body = doc.appendChild(new Node('body'));
    const tabBtn = doc.body.appendChild(new Node('button')); tabBtn.id = 'tab-btn-config';
    const root = doc.body.appendChild(new Node('div')); root.id = 'config-root';
    const schema = doc.body.appendChild(new Node('script')); schema.id = 'config-schema-defaults';
    schema.textContent = JSON.stringify({ fields: {}, groups: {} });

    const store = {};
    const confirms = [];
    const win = {
      location: { hash: '', pathname: '/dashboard', search: '?tab=config', href: 'http://127.0.0.1/dashboard?tab=config' },
      history: { state: null, replaceState() {} },
      localStorage: { getItem: k => (k in store ? store[k] : null), setItem: (k, v) => { store[k] = String(v); }, removeItem: k => { delete store[k]; } },
      confirm: m => { confirms.push(m); return true; },
      addEventListener() {},
    };
    let view;
    // PUT 经 putGate 放行（模拟 preflight + activate 耗时）；put409 非 null 时 PUT 返回 409；
    // getFail > 0 时 GET 返回 500
    const net = { puts: [], gets: 0, getFail: 0, putGate: Promise.resolve(), putBody: null, put409: null };
    const resp = (status, body) => ({ status: status, ok: status === 200, json: () => Promise.resolve(JSON.parse(JSON.stringify(body))) });
    const fetchFn = (url, init) => {
      if (url === '/api/status') return Promise.resolve(resp(200, { tiers: [], in_flight: 0 }));
      if (init && init.method === 'PUT') {
        net.puts.push(JSON.parse(init.body));
        if (net.put409) return Promise.resolve(resp(409, { error: 'conflict', sections: net.put409 }));
        return net.putGate.then(() => resp(200, net.putBody || { revisions: {}, applied: true, apply_error: null, cleared_secrets: [], reset_runtime: [] }));
      }
      net.gets++;
      if (net.getFail > 0) { net.getFail--; return Promise.resolve(resp(500, {})); }
      return Promise.resolve(resp(200, view));
    };
    const tick = () => new Promise(r => setTimeout(r, 0));
    const until = async (f, what) => { for (let i = 0; i < 200 && !f(); i++) await tick(); assert.ok(f(), what); };
    const ctx = { window: win, document: doc, fetch: fetchFn, URL, console, JSON, Promise, setTimeout };
    win.document = doc;
    vm.createContext(ctx);
    vm.runInContext(fs.readFileSync(process.argv[2], 'utf8'), ctx);
    const CT = win.ConfigTab;
    store['coding-proxy.admin-token'] = 'T'.repeat(43);

    const pricing = [
      { vendor: 'a', model: 'x', input_cost_per_mtok: '$1.0' },
      { vendor: 'a', model: 'y', input_cost_per_mtok: '$2.0' },
    ];
    const registry = [
      { path: 'server.port', label: 'Port', widget: 'number', readonly: true, group: 'Server', description: '端口' },
      { path: 'failover.status_codes', label: 'Status Codes', widget: 'list', description: '状态码' },
    ];
    function mkView(overridden, saved) {
      return {
        mode: 'store', errors: [], notices: [], revisions: { pricing: 1 }, overridden: overridden, vendor_overrides: [],
        values: { vendors: [], absent_vendors: [], tiers: null, failover: { status_codes: [429] }, model_mapping: [],
          pricing: saved, native_api: {}, session_policies: { policies: [] }, server: { port: 3392 }, logging: {}, database: {}, auth: {} },
        defaults: { vendors: [], tiers: null, failover: {}, model_mapping: [], pricing: pricing, native_api: {}, session_policies: {} },
        secrets: {}, sources: {}, running: { tiers: null, boot_revisions: {} }, registry: { fields: registry, hidden: [] },
        apply_supported: true,
      };
    }
    const pill = name => root.querySelector('.cfg-pills .range-btn[data-pill="' + name + '"]').click();
    const banners = () => root.querySelectorAll('.cfg-banner-text').map(b => b.textContent);
    const rows = () => root.querySelectorAll('.cfg-list-row');
    const delRow = i => rows()[i].querySelector('.cfg-btn-danger').click();

    (async () => {
      // 1) 未覆盖 pricing：删除默认行不出现「合并」banner
      view = mkView([], pricing.slice());
      await CT.refresh(true);
      pill('pricing');
      delRow(0);
      assert.strictEqual(rows().length, 1);
      assert.ok(!banners().some(t => t.includes('包内默认定价')), banners().join('|'));
      assert.ok(tabBtn.classList.contains('cfg-dirty'));
      assert.strictEqual(confirms.length, 0, 'row removal must not ask for confirmation');
      const undoBtn = root.querySelectorAll('.cfg-savebar .cfg-link').find(b => b.textContent === '撤销');
      assert.ok(undoBtn, 'undo entry must be offered');
      undoBtn.click();
      assert.strictEqual(rows().length, 2);
      assert.strictEqual(CT.isDirty(), false, 'undo restores the saved state');
      delRow(0); // 恢复原步骤的前置状态：移除第 1 行后继续 → 页签 dirty 点同步清除
      root.querySelectorAll('.cfg-toolbar .cfg-btn').find(b => b.textContent === '锁定').click();
      assert.ok(root.querySelector('.cfg-lock'));
      assert.strictEqual(CT.isDirty(), false);
      assert.strictEqual(tabBtn.classList.contains('cfg-dirty'), false);

      // 1b) 已覆盖 pricing：仅统计已保存表中缺失的默认行；本地未保存删除不计入；合并后消失
      store['coding-proxy.admin-token'] = 'T'.repeat(43);
      view = mkView(['pricing'], pricing.slice(0, 1));
      await CT.refresh(true);
      pill('pricing');
      const hint = () => banners().filter(t => t.includes('包内默认定价'));
      assert.deepStrictEqual(hint().length, 1);
      assert.ok(hint()[0].includes(' 1 行'), hint()[0]);
      delRow(0);
      assert.ok(hint()[0].includes(' 1 行'), hint()[0]);
      root.querySelectorAll('.cfg-banner .cfg-btn').find(b => b.textContent === '合并').click();
      assert.deepStrictEqual(rows().map(r => r.textContent.includes('y')), [true]);
      assert.strictEqual(hint().length, 0);

      // 5) pills 为 group + aria-current（不再声明缺少 tab 子项的 tablist）
      for (const sel of ['.cfg-pills']) {
        const g = root.querySelector(sel);
        assert.strictEqual(g.getAttribute('role'), 'group');
        assert.ok(g.children.every(b => b.getAttribute('role') === null));
        assert.strictEqual(g.querySelectorAll('[aria-current="true"]').length, 1);
      }
      assert.strictEqual(root.querySelectorAll('[role="tablist"]').length, 0);

      // 4) label[for] 只指向可标注控件；只读值行用 aria-labelledby 命名
      const LABELABLE = ['INPUT', 'SELECT', 'TEXTAREA', 'BUTTON', 'OUTPUT'];
      for (const name of ['failover', 'system']) {
        pill(name);
        root.querySelectorAll('label').forEach(l => {
          const t = doc.getElementById(l.htmlFor);
          assert.ok(t && LABELABLE.includes(t.tagName), name + ': ' + l.textContent);
        });
        root.querySelectorAll('.cfg-row').forEach(r => {
          const ctl = r.querySelector('.cfg-control');
          const ref = ctl.getAttribute('aria-labelledby');
          assert.ok(r.querySelector('label') || (ref && doc.getElementById(ref)), name + ': unlabeled row');
        });
      }
      pill('failover');
      assert.ok(root.querySelector('label[for]'));
      pill('system');
      const port = root.querySelector('.cfg-row[data-path="server.port"]');
      assert.strictEqual(port.querySelector('label'), null);
      assert.strictEqual(doc.getElementById(port.querySelector('.cfg-control').getAttribute('aria-labelledby')).textContent, 'Port');
      const migration = root.querySelectorAll('.cfg-row').filter(r => r.getAttribute('data-path') === null);
      assert.strictEqual(migration.length, 5);

      // 3) Sessions 溢出单元格经 truncTitle 补 tooltip；Model Bind 单元格除外
      const table = doc.body.appendChild(new Node('table')); table.className = 'session-table';
      const tr = table.appendChild(new Node('tr')); tr.setAttribute('data-row', '');
      const td = tr.appendChild(new Node('td')); td.textContent = '1,234,567';
      td.scrollWidth = 100; td.clientWidth = 88;
      const bind = tr.appendChild(new Node('td')); bind.className = 'cell-bind'; bind.textContent = 'zhipu';
      bind.scrollWidth = 200; bind.clientWidth = 120;
      const over = n => doc.listeners.mouseover.forEach(f => f({ target: n }));
      over(td);
      assert.strictEqual(td.title, '1,234,567');
      over(bind);
      assert.strictEqual(bind.title, '');
      td.scrollWidth = 80;
      over(td);
      assert.strictEqual(td.getAttribute('title'), null);

      const saveBtn = () => root.querySelectorAll('.cfg-savebar .cfg-btn').find(b => b.textContent.startsWith('保存'));
      const chips = () => root.querySelectorAll('.cfg-chip-text').map(c => c.textContent).join(',');
      const addChip = v => { const i = root.querySelector('.cfg-chip-add'); i.value = v; i.dispatch('keydown', { key: 'Enter' }); };

      // 6) 保存进行中冻结表单（inert），⟳ 不重载；PUT 200 后以服务端值为准、dirty 清除
      view = mkView([], pricing.slice());
      await CT.refresh(true);
      pill('failover');
      addChip('500');
      assert.ok(CT.isDirty());
      let releasePut;
      net.putGate = new Promise(r => { releasePut = r; });
      const putsBefore = net.puts.length;
      saveBtn().click();
      await until(() => net.puts.length > putsBefore, 'PUT not sent');
      const body = root.querySelector('.cfg-body');
      assert.ok(body.hasAttribute('inert'));
      assert.strictEqual(body.getAttribute('aria-busy'), 'true');
      addChip('502'); // 保存期间的编辑被拦截，不会在随后的整体重载中被静默丢弃
      assert.ok(!chips().includes('502'));
      assert.ok(root.querySelectorAll('.cfg-toolbar .cfg-btn').every(b => b.disabled), 'lock button enabled during save');
      const getsDuring = net.gets, confirmsDuring = confirms.length;
      await CT.refresh(false);
      assert.strictEqual(net.gets, getsDuring);
      assert.strictEqual(confirms.length, confirmsDuring);
      view.values.failover.status_codes = [429, 500];
      releasePut();
      await until(() => !CT.isDirty() && !root.querySelector('.cfg-body').hasAttribute('inert'), 'still saving');
      assert.strictEqual(root.querySelector('.cfg-body').getAttribute('aria-busy'), null);
      assert.strictEqual(chips(), '429,500');
      const bar6 = root.querySelector('.cfg-savebar').textContent;
      assert.ok(bar6.includes('已保存并生效'), bar6);
      assert.ok(!banners().some(t => t.startsWith('已保存')), banners().join('|'));
      assert.strictEqual(confirms.length, confirmsDuring, 'routine save must not ask for confirmation');
      net.putGate = Promise.resolve();

      // 7) PUT 200 但随后 GET 失败：本地已落定（无 dirty、无待设置明文），「已保存」提示仍可见
      const secretReg = registry.concat([
        { path: 'vendors.*.api_key', label: 'API Key', widget: 'secret', vendors: ['zhipu'], group: 'Credentials', description: '密钥' },
      ]);
      const PLAIN = 'PLAINTEXT-PROBE-' + 'x'.repeat(16);
      view = mkView([], pricing.slice());
      view.values.vendors = [{ vendor: 'zhipu', enabled: true }];
      view.registry.fields = secretReg;
      view.secrets = { 'vendors.zhipu.api_key': { kind: 'env', env_var: 'ZHIPU_API_KEY', resolved: false, hint: '', default_env: 'ZHIPU_API_KEY' } };
      await CT.refresh(true);
      pill('vendors');
      const menu = root.querySelector('select.cfg-secret-menu');
      menu.value = 'set'; menu.dispatch('change');
      const inp = root.querySelector('.cfg-secret-input');
      inp.value = PLAIN; inp.dispatch('input');
      assert.ok(CT.isDirty());
      net.getFail = 1;
      saveBtn().click();
      await until(() => root.textContent.includes('加载配置失败'), 'reload failure not rendered');
      assert.strictEqual(net.puts[net.puts.length - 1].secret_ops['vendors.zhipu.api_key'].value, PLAIN);
      assert.strictEqual(CT.isDirty(), false);
      assert.strictEqual(tabBtn.classList.contains('cfg-dirty'), false);
      assert.ok(root.textContent.includes('已保存并生效'), root.textContent);
      assert.ok(!root.textContent.includes(PLAIN));
      const confirmsAfter = confirms.length;
      await CT.refresh(false);
      assert.strictEqual(confirms.length, confirmsAfter, 'asked to discard already-saved changes');
      assert.ok(!root.querySelectorAll('input').some(i => i.value === PLAIN));
      const bar7 = root.querySelector('.cfg-savebar');
      assert.ok(!bar7.hidden && bar7.textContent.includes('已保存并生效'), bar7.textContent);
      const close7 = root.querySelectorAll('.cfg-savebar .cfg-btn').find(b => b.textContent === '关闭');
      assert.ok(close7, 'result state must be dismissible');
      close7.click();
      assert.ok(root.querySelector('.cfg-savebar').hidden);

      // 8) Esc 只隐藏当前说明；再次悬停 / 聚焦其他 ⓘ 时恢复显示
      pill('failover');
      const wraps = root.querySelectorAll('.cfg-info-wrap');
      assert.ok(wraps.length >= 2);
      doc.listeners.keydown.forEach(f => f({ key: 'Escape' }));
      assert.ok(wraps.every(w => w.classList.contains('cfg-tip-off')));
      wraps[0].dispatch('mouseenter');
      assert.ok(!wraps[0].classList.contains('cfg-tip-off'));
      wraps[1].querySelector('.cfg-info').dispatch('focusin');
      assert.ok(!wraps[1].classList.contains('cfg-tip-off'));

      // 9) System：只读 secret 不出更改菜单；Google Secret 为显式例外，单独保存时不宣称「已生效」
      view = mkView([], pricing.slice());
      view.registry.fields = registry.concat([
        { path: 'auth.google_client_secret', label: 'Google Secret', widget: 'secret', group: 'Auth', description: '密钥', unset_hint: '使用内置默认' },
        { path: 'logging.ro_secret', label: 'RO Secret', widget: 'secret', group: 'Logging', description: '只读', readonly: true },
      ]);
      view.secrets = { 'auth.google_client_secret': { kind: 'default', env_var: null, resolved: true, hint: '', default_env: null } };
      await CT.refresh(true);
      pill('system');
      assert.ok(root.querySelector('.cfg-body > p.cfg-note').textContent.includes('Google Secret 例外'));
      assert.strictEqual(root.querySelector('.cfg-row[data-path="logging.ro_secret"] select'), null);
      const gmenu = root.querySelector('.cfg-row[data-path="auth.google_client_secret"] select.cfg-secret-menu');
      gmenu.value = 'clear'; gmenu.dispatch('change');
      const putsBefore9 = net.puts.length;
      saveBtn().click();
      await until(() => net.puts.length > putsBefore9 && !CT.isDirty() && !root.querySelector('.cfg-body').hasAttribute('inert'), 'save not finished');
      assert.deepStrictEqual(net.puts[net.puts.length - 1].secret_ops, { 'auth.google_client_secret': { op: 'clear' } });
      const note = root.querySelector('.cfg-savebar').textContent;
      assert.ok(note && !note.startsWith('已保存并生效') && note.includes('CLI 登录'), String(note));

      // 10) 排序后展开行仍指向原条目：move 对夹在区间内的展开下标做 ∓1 平移
      view = mkView([], pricing.slice());
      view.values.model_mapping = [
        { pattern: 'A', target: 'a', is_regex: false, vendors: [] },
        { pattern: 'B', target: 'b', is_regex: false, vendors: [] },
        { pattern: 'C', target: 'c', is_regex: false, vendors: [] },
      ];
      await CT.refresh(true);
      pill('model_mapping');
      rows()[1].click(); // 展开 B（index 1）
      assert.ok(root.querySelector('.cfg-detail-row .cfg-row[data-path="model_mapping.1.pattern"]'));
      rows()[0].querySelector('.cfg-btn-icon[aria-label="下移"]').focus();
      rows()[0].querySelector('.cfg-btn-icon[aria-label="下移"]').click(); // [A,B,C] → [B,A,C]
      assert.ok(root.querySelector('.cfg-detail-row .cfg-row[data-path="model_mapping.0.pattern"]'),
        'expanded row must follow the original entry after move');
      assert.ok(root.querySelectorAll('.cfg-detail-row').length === 1);
      const moved = doc.activeElement;
      assert.ok(moved && moved.tagName === 'BUTTON' && moved.getAttribute('aria-label') === '下移' &&
        moved.closest('[data-path]').getAttribute('data-path') === 'model_mapping.1', 'focus must follow the moved row');

      // 10b) 「添加」后焦点落在新行自己的编辑表单（此时另一行处于展开状态）
      root.querySelectorAll('.cfg-section-head .cfg-btn').find(b => b.textContent === '添加').click();
      const added = doc.activeElement;
      assert.ok(added && added.tagName === 'INPUT' && added.closest('.cfg-detail-row') &&
        added.closest('.cfg-detail-row').querySelector('.cfg-row').getAttribute('data-path') === 'model_mapping.3.pattern',
        'focus must land in the newly added row, not an already expanded one');

      // 11) 409 + 定向重载失败：不保留「已重新加载」承诺；重试仅重载冲突 section
      view = mkView([], pricing.slice());
      view.values.failover = { status_codes: [429] };
      await CT.refresh(true);
      pill('pricing');
      delRow(0); // pricing 与 failover 同时有本地修改
      pill('failover');
      addChip('500');
      assert.ok(CT.isDirty());
      net.put409 = ['pricing'];
      net.getFail = 1; // 409 后的定向重载失败
      saveBtn().click();
      await until(() => root.textContent.includes('加载配置失败'), 'fatal not rendered');
      assert.ok(!root.textContent.includes('已重新加载'), 'must not claim reloaded');
      assert.ok(root.textContent.includes('但重新加载失败'), root.textContent);
      assert.ok(CT.isDirty(), 'unrelated local edits must survive');
      root.querySelectorAll('.cfg-banner .cfg-btn').find(b => b.textContent === '重试').click();
      await until(() => root.querySelector('.cfg-body'), 'retry did not restore the form');
      assert.ok(CT.isDirty(), 'failover edit must survive section-scoped retry');
      pill('failover');
      assert.ok(chips().includes('500'));
      assert.ok(!root.textContent.includes('但重新加载失败'));
      net.put409 = null;

      // 12) Per-Model 行移除后保存栏给「撤销」，撤销恢复该模型的上限
      view = mkView([], pricing.slice());
      view.registry.fields = registry.concat([
        { path: 'vendors.*.concurrency', label: 'Concurrency', widget: 'optional_group', vendors: ['zhipu'], group: 'Concurrency', description: '并发' },
        { path: 'vendors.*.concurrency.models', label: 'Per-Model', widget: 'map', vendors: ['zhipu'], group: 'Concurrency', description: '上限' },
      ]);
      view.values.vendors = [{ vendor: 'zhipu', enabled: true, concurrency: { models: { 'glm-x': 3 } } }];
      await CT.refresh(true);
      pill('vendors');
      assert.strictEqual(root.querySelector('.cfg-map-row span.cfg-map-key').textContent, 'glm-x');
      const undo = () => root.querySelectorAll('.cfg-savebar .cfg-link').find(b => b.textContent === '撤销');
      root.querySelector('.cfg-map-row .cfg-btn-sm').click(); // 移除 glm-x
      assert.strictEqual(root.querySelector('.cfg-map-row span.cfg-map-key'), null);
      assert.ok(undo(), 'map removal must offer undo immediately');
      undo().click();
      assert.strictEqual(root.querySelector('.cfg-map-row span.cfg-map-key').textContent, 'glm-x', 'undo restores the removed limit');
      assert.strictEqual(CT.isDirty(), false);

      // 13) 移除 vendor 后撤销把它插回 vendors 与 tiers 的原位；期间重新添加会清掉撤销入口，不产生重复 vendor
      view = mkView([], pricing.slice());
      view.registry.fields = registry.concat([{ path: 'tiers', label: 'Tiers', widget: 'list', description: '链路', enum: ['zhipu', 'kimi'] }]);
      view.values.vendors = [{ vendor: 'zhipu', enabled: true }, { vendor: 'kimi', enabled: true }];
      view.values.tiers = ['zhipu', 'kimi'];
      view.defaults.vendors = [{ vendor: 'zhipu', enabled: true }, { vendor: 'kimi', enabled: true }];
      await CT.refresh(true);
      pill('vendors');
      const pills = () => root.querySelectorAll('.cfg-subpills .range-btn[data-vendor]').map(x => x.getAttribute('data-vendor'));
      const removeVendorBtn = () => root.querySelector('.cfg-vendor-head .cfg-btn-danger');
      removeVendorBtn().click(); // window.confirm 桩恒为 true
      assert.deepStrictEqual(pills(), ['kimi', 'zhipu']);
      assert.ok(undo(), 'vendor removal must offer undo');
      undo().click();
      assert.deepStrictEqual(pills(), ['zhipu', 'kimi'], 'undo puts the vendor back at its original position');
      assert.strictEqual(CT.isDirty(), false);
      removeVendorBtn().click();
      assert.ok(undo());
      root.querySelectorAll('.cfg-body .cfg-btn-primary').find(b => b.textContent === '添加').click(); // 在「未配置」视图里重新添加 zhipu
      assert.strictEqual(undo(), undefined, 're-adding the vendor clears the undo entry');
      assert.deepStrictEqual(pills(), ['kimi', 'zhipu']);
      console.log('ok');
    })().catch(e => { console.error(e && e.stack || e); process.exit(1); });
    """
)


@pytest.mark.skipif(_NODE is None, reason="node 不可用")
def test_config_js_rendering_under_node(tmp_path: Path):
    _run_node(tmp_path, _DOM_HARNESS)


def test_session_policies_section_renders_model_fields():
    """Config 页 session_policies 节渲染 bindable_models 清单与 policies 的 model 字段."""
    src = _CONFIG_JS.read_text(encoding="utf-8")
    # bindable_models listBlock：Label/Model/Tiers/Exclude 列 + 四字段编辑
    assert "specPath: 'session_policies.bindable_models'" in src
    assert (
        "itemFields(parts.concat(['bindable_models']), i, "
        "['label', 'model', 'tiers', 'exclude'])" in src
    )
    # policies 条目编辑器含 model 覆盖字段
    assert (
        "itemFields(parts.concat(['policies']), i, "
        "['name', 'match.session_keys', 'tiers', 'model'])" in src
    )
