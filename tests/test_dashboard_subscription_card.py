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
    """档位名 / 模型族 / 窗口 label / 错误消息 / tier 名均来自载荷，渲染必须经 escapeHtml."""
    html = client.get("/dashboard").text
    assert "${escapeHtml(plan)}" in html
    assert '<span class="zone-label">${escapeHtml(g)}</span>' in html
    assert '<span class="quota-label">${escapeHtml(label)}</span>' in html
    assert 'title="${escapeHtml(title)}"' in html
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


def test_subscription_card_css_keeps_rows_on_one_line(client: TestClient):
    """单行是 CSS 硬约束：分区不折行、放不下时区内横向滚动；960px 及以下才退回两行."""
    html = client.get("/dashboard").text
    start = html.index("/* ── 订阅状态")
    block = html[start : html.index("/* ── 故障转移表", start)]
    narrow_at = block.index("@media (max-width: 960px)")
    wide, narrow = block[:narrow_at], block[narrow_at:]

    def rule(source: str, selector: str) -> str:
        match = re.search(re.escape(selector) + r"\s*\{([^}]*)\}", source)
        assert match, f"{selector} 规则缺失"
        return match.group(1)

    zones = rule(wide, ".vendor-badges")
    assert "flex-wrap: nowrap" in zones and "overflow-x: auto" in zones
    # 经典滚动条平台上滚动条会占掉分区的行内高度，让放不下的那一行文字上移，所以隐藏
    assert "scrollbar-width: none" in zones
    assert "white-space: nowrap" in rule(wide, ".vendor-name")
    assert "flex: 1 1 auto" in rule(wide, ".has-sub > .sub-zone")
    assert "flex: 0 0 auto" in rule(
        wide, ".tier-zone"
    )  # 路由区不收缩，余量不足只由订阅区滚动
    assert "flex-wrap: wrap" in rule(narrow, ".subscription-card .vendor-item")
    assert "min-width: 0" in rule(narrow, ".subscription-card .vendor-info")
    assert "flex: 1 1 100%" in rule(narrow, ".subscription-card .has-sub > .sub-zone")


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
    // 紧凑写法留在行内，完整写法进 Tooltip
    assert.strictEqual(fmtResetShort(soon), '30m');
    assert.strictEqual(fmtResetShort(later), '30h');
    assert.strictEqual(fmtResetShort(later + 2 * 86400), '3d');
    assert.strictEqual(fmtResetShort(past), '即将');
    assert.strictEqual(fmtResetShort(null), '');
    // 距重置不足半分钟按「即将」处理，不出 0m；48 小时是「小时」与「天」的分界
    const almost = Math.floor(Date.now() / 1000) + 20;
    assert.strictEqual(fmtResetShort(almost), '即将');
    assert.strictEqual(fmtResetCountdown(almost), '即将重置');
    assert.strictEqual(fmtResetShort(Math.floor(Date.now() / 1000) + 47 * 3600 + 58 * 60), '47h');
    assert.strictEqual(fmtResetShort(Math.floor(Date.now() / 1000) + 48 * 3600), '2d');
    // 重置时间不是有限数字（ISO 字符串 / NaN）：不出倒计时
    assert.strictEqual(fmtResetShort('2026-11-25T00:00:00Z'), '');
    assert.strictEqual(fmtResetShort(NaN), '');
    assert.strictEqual(fmtResetCountdown('2026-11-25T00:00:00Z'), '');

    // 用量格严重度与守卫窗口短标签
    assert.strictEqual(quotaClass(69), 'q-ok');
    assert.strictEqual(quotaClass(70), 'q-warn');
    assert.strictEqual(quotaClass(90), 'q-err');
    assert.strictEqual(quotaWindowLabel(5), '5h');
    assert.strictEqual(quotaWindowLabel(24), '1d');
    assert.strictEqual(quotaWindowLabel(168), '7d');
    assert.strictEqual(quotaWindowLabel(undefined), '配额');
    const labels = (html) => Array.from(html.matchAll(/class="quota-label">([^<]*)<\/span>/g), m => m[1]);
    const values = (html) => Array.from(html.matchAll(/class="quota-val">([^<]*)<\/span>/g), m => m[1]);

    const tier = (name, extra) => Object.assign(
      { name: name, circuit_breaker: { state: 'closed', failure_count: 0 },
        rate_limit: { is_rate_limited: false } }, extra || {});
    const quota = (hours, pct) => ({ state: 'within_quota', usage_percent: pct, window_hours: hours });
    const screenshotTiers = [tier('zhipu', { quota_guard: quota(24, 0) }), tier('anthropic')];
    // 上游按「先 周 后 5h」给窗口：渲染时组内固定先 5h 后 周
    const agWindows = [
      { id: 'gemini-weekly', window: 'weekly', group: 'Gemini', label: 'Gemini Models weekly', used_percent: 0, reset_epoch: later },
      { id: 'gemini-5h', window: '5h', group: 'Gemini', label: 'Gemini Models 5h', used_percent: 4.9, reset_epoch: soon },
      { id: 'gpt-weekly', window: 'weekly', group: 'Claude+GPT', label: 'Claude and GPT models weekly', used_percent: 0, reset_epoch: later },
      { id: 'gpt-5h', window: '5h', group: 'Claude+GPT', label: 'Claude and GPT models 5h', used_percent: 0, reset_epoch: soon },
    ];
    const screenshotVendors = [
      { key: 'claude', plan: null, status: 'waiting', windows: [] },
      { key: 'glm', plan: 'Max', status: 'ok',
        windows: [{ id: '5h', window: '5h', label: '5 小时', used_percent: 30, reset_epoch: soon }] },
      { key: 'antigravity', plan: 'Antigravity', status: 'ok', windows: agWindows },
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
    assert.ok(glmRow.includes('<span class="zone-label plan">Max</span>'));   // 档位：行名后的加粗亮字
    assert.deepStrictEqual(labels(glmRow), ['5h', '1d']);                     // 订阅区与路由区的窗口短标签同一套
    assert.deepStrictEqual(values(glmRow), ['30%', '0%']);
    // 用量格：完整写法进 Tooltip，行内只留紧凑倒计时
    assert.ok(/class="quota-group q-ok" title="5 小时：已用 30%，\d+ 分钟后重置">/.test(glmRow));
    assert.ok(/class="sub-reset"><svg[^>]*>.*?<\/svg>\d+m<\/span>/.test(glmRow));
    assert.ok(glmRow.includes('title="本代理 1d 滑动窗口配额守卫：已用 0%"'));
    assert.ok(glmRow.includes('>正常</span>'));
    assert.ok(glmRow.indexOf('class="vendor-badges sub-zone"')
      < glmRow.indexOf('class="vendor-badges tier-zone"'));
    assert.strictEqual((glmRow.match(/class="drag-handle"/g) || []).length, 1);
    const claudeRow = rows.slice(rows.indexOf('data-vendor="anthropic"'));
    assert.ok(claudeRow.includes('Claude Code 订阅'));
    assert.ok(claudeRow.includes('<span class="tier-tag">anthropic</span>'));
    assert.ok(claudeRow.includes('>等待首个请求</span>'));                    // 行内短文案，说明进 Tooltip
    assert.ok(claudeRow.includes('title="用量来自被动采集'));
    assert.ok(!rows.includes('无订阅数据'));

    // 无 tier 的订阅行排在其后的独立容器，不可拖拽；没有路由区，也就不画分隔线
    assert.strictEqual(rowCount(subHTML()), 1);
    const agHTML = subHTML();
    assert.ok(agHTML.includes('<span class="vendor-name">Antigravity</span>'));
    assert.ok(!agHTML.includes('zone-label plan'));                          // 档位名与行名相同，不重复显示
    assert.ok(agHTML.includes('<span class="zone-note sub-only" title="该订阅对应的供应商未启用或未加入路由链（tiers）">未加入路由链</span>'));
    assert.ok(!agHTML.includes('tier-zone'));
    assert.ok(!agHTML.includes('data-vendor') && !agHTML.includes('drag-handle'));
    // 窗口按模型族分组：灰字标签 + 该组的用量格，组内先 5h 后 周
    const groupLabels = (html) => Array.from(html.matchAll(/class="zone-label">([^<]*)<\/span>/g), m => m[1]);
    assert.deepStrictEqual(groupLabels(agHTML), ['Gemini', 'Claude+GPT']);
    assert.ok(agHTML.includes('<span class="qg"><span class="zone-label">Gemini</span><span class="quota-group '));
    assert.deepStrictEqual(labels(agHTML), ['5h', '周', '5h', '周']);
    assert.deepStrictEqual(values(agHTML), ['5%', '0%', '0%', '0%']);

    // 无订阅对应的 tier 行：保留路由区并加「供应商状态」标注，不出订阅区
    updateVendorStatus({ tiers: [tier('zhipu'),
      tier('copilot', { weekly_quota_guard: quota(168, 12) }), tier('anthropic')] });
    rows = tierHTML();
    assert.strictEqual(rowCount(rows), 3);
    assert.ok(rows.indexOf('data-vendor="zhipu"') < rows.indexOf('data-vendor="copilot"'));
    const copilotRow = rows.slice(rows.indexOf('data-vendor="copilot"'), rows.indexOf('data-vendor="anthropic"'));
    assert.ok(copilotRow.includes('<span class="vendor-name">copilot</span>'));
    assert.ok(copilotRow.includes('<span class="tier-tag">供应商状态</span>'));
    assert.ok(copilotRow.includes('<div class="vendor-badges tier-zone">'));  // 路由区类名统一，分隔线才对齐
    assert.deepStrictEqual(labels(copilotRow), ['7d']);
    assert.deepStrictEqual(values(copilotRow), ['12%']);
    assert.ok(!copilotRow.includes('sub-zone'));

    // 头像首字母：供应商行与订阅行头像（C / G / A）撞字母时取前两个字母
    assert.ok(copilotRow.includes('<div class="vendor-avatar">Co</div>'));
    updateVendorStatus({ tiers: [tier('alibaba'), tier('minimax'), tier('kimi')] });
    rows = tierHTML();
    assert.ok(rows.includes('<div class="vendor-avatar">Al</div>'));
    assert.ok(rows.includes('<div class="vendor-avatar">M</div>'));
    assert.ok(rows.includes('<div class="vendor-avatar">K</div>'));

    // 手柄是可聚焦按钮：读屏标签带当前位置，快捷键 ↑ / ↓
    updateVendorStatus({ tiers: [tier('zhipu'), tier('copilot'), tier('anthropic')] });
    rows = tierHTML();
    assert.strictEqual((rows.match(/<button type="button" class="drag-handle"/g) || []).length, 3);
    assert.ok(rows.includes('aria-keyshortcuts="ArrowUp ArrowDown"'));
    assert.ok(rows.includes('aria-label="调整 GLM Coding Plan 的优先级：第 1 位，共 3 个"'));
    assert.ok(rows.includes('aria-label="调整 copilot 的优先级：第 2 位，共 3 个"'));
    assert.ok(rows.includes('aria-label="调整 Claude Code 订阅 的优先级：第 3 位，共 3 个"'));

    // 行序跟随运行时优先级（拖拽后 tiers 顺序变化）
    updateVendorStatus({ tiers: [tier('anthropic'), tier('zhipu')] });
    rows = tierHTML();
    assert.ok(rows.indexOf('data-vendor="anthropic"') < rows.indexOf('data-vendor="zhipu"'));

    // 路由区：状态徽章在前（熔断 ×N / 限速中 / 半开），配额守卫用量格在后
    updateVendorStatus({ tiers: [
      tier('zhipu', { circuit_breaker: { state: 'open', failure_count: 3 },
        rate_limit: { is_rate_limited: true }, weekly_quota_guard: quota(168, 78) }),
      tier('anthropic', { circuit_breaker: { state: 'half_open', failure_count: 0 } }),
    ] });
    rows = tierHTML();
    const zhipuRow = rows.slice(0, rows.indexOf('data-vendor="anthropic"'));
    const zhipuTier = zhipuRow.slice(zhipuRow.indexOf('class="vendor-badges tier-zone"'));
    assert.ok(zhipuRow.includes('<span class="status-badge sb-err">熔断 ×3</span>'));
    assert.ok(zhipuTier.includes('<span class="status-badge sb-warn">限速中</span>'));
    assert.ok(zhipuTier.indexOf('>熔断 ×3</span>') < zhipuTier.indexOf('>限速中</span>'));
    assert.ok(zhipuTier.indexOf('>限速中</span>') < zhipuTier.indexOf('class="quota-group'));
    assert.ok(zhipuTier.includes('<span class="quota-group q-warn" title="本代理 7d 滑动窗口配额守卫：已用 78%">'));
    assert.ok(rows.includes('<span class="status-badge sb-warn">半开</span>'));

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
        windows: [{ id: 'w', window: '<b>', group: '<img src=x onerror=alert(4)>',
          label: '<script>', used_percent: 95, reset_epoch: null }] },
      { key: 'antigravity', plan: 'P', status: 'error',
        error: '" onmouseover="alert(2)', windows: [] },
      { key: 'claude', status: 'not_configured', windows: [] },
    ] });
    let out = tierHTML() + subHTML();
    // 原始标签不得出现；onerror 仅允许以转义后的惰性文本存在
    assert.ok(!out.includes('<img'));
    assert.ok(!out.includes('<script>'));
    assert.ok(out.includes('&lt;img src=x onerror=alert(1)&gt;</span>'));     // 档位名
    assert.ok(out.includes('&lt;img src=x onerror=alert(4)&gt;</span>'));     // 模型族
    assert.ok(out.includes('&lt;script&gt;'));                                // 认不出的窗口类别回退 label
    assert.ok(!out.includes('" onmouseover="alert(2)'));
    assert.ok(out.includes('查询失败'));
    assert.ok(out.includes('未启用'));
    assert.ok(out.includes('<span class="quota-group q-err"'));               // ≥ 90% 落红档

    // tier 名同样来自载荷：名称 / data-vendor / 首字母三处均转义
    updateVendorStatus({ tiers: [tier('<img src=x onerror=alert(3)>'), tier('anthropic')] });
    out = tierHTML();
    assert.ok(!out.includes('<img'));
    assert.ok(out.includes('&lt;img src=x onerror=alert(3)&gt;'));
    assert.ok(out.includes('<div class="vendor-avatar">&lt;</div>'));
    assert.strictEqual((out.match(/data-vendor="/g) || []).length, 2);
    updateVendorStatus({ tiers: screenshotTiers });

    // 仅带 tier 数据的定向重渲染（拖拽回滚 / 复位后刷新）复用订阅缓存，订阅区不被清空
    updateSubscriptions({ vendors: screenshotVendors });
    updateVendorStatus({ tiers: [tier('anthropic'), tier('zhipu')] });
    rows = tierHTML();
    assert.ok(rows.includes('<span class="zone-label plan">Max</span>') && rows.includes('<span class="quota-val">30%</span>'));
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
    assert.ok(rows.includes('<span class="vendor-name">copilot</span>') && rows.includes('<span class="zone-label plan">Pro</span>'));

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

    // 认不出的窗口类别（上游新增）：标签回退 label，排在已知类别之后；没有重置时间就不出倒计时
    updateVendorStatus({ tiers: [tier('zhipu')] });
    updateSubscriptions({ vendors: [{ key: 'glm', plan: null, status: 'ok', windows: [
      { id: 'x', label: '自定义窗口', used_percent: 20, reset_epoch: null },
      { id: 'weekly', window: 'weekly', label: '每周', used_percent: 10, reset_epoch: null },
      { id: '5h', window: '5h', label: '5 小时', used_percent: 5, reset_epoch: null },
    ] }] });
    assert.deepStrictEqual(labels(tierHTML()), ['5h', '周', '自定义窗口']);
    assert.ok(!tierHTML().includes('sub-reset'));

    // 窗口类别 / 模型族取到对象原型上的名字：不命中继承成员，按未知类别处理
    updateSubscriptions({ vendors: [{ key: 'glm', status: 'ok', windows: [
      { id: 'x', window: 'constructor', label: 'legacy-window', used_percent: 5, reset_epoch: null },
    ] }] });
    assert.deepStrictEqual(labels(tierHTML()), ['legacy-window']);
    updateSubscriptions({ vendors: [{ key: 'antigravity', status: 'ok', windows: [
      { id: 'y', window: '5h', group: 'constructor', label: 'g 5h', used_percent: 5, reset_epoch: null },
      { id: 'z', window: 'weekly', group: '__proto__', label: 'p weekly', used_percent: 6, reset_epoch: null },
    ] }] });
    const protoHTML = subHTML();
    assert.ok(protoHTML.includes('<span class="zone-label">constructor</span>'));
    assert.ok(protoHTML.includes('<span class="zone-label">__proto__</span>'));
    assert.deepStrictEqual(labels(protoHTML), ['5h', '周']);

    // 同一家混有分组与不分组的窗口：不分组的前一组在前，分组标签只带在该组窗口之前
    updateSubscriptions({ vendors: [{ key: 'antigravity', status: 'ok', windows: [
      { id: 'a', label: 'A', used_percent: 1, reset_epoch: null },
      { id: 'b', window: 'weekly', group: 'B', label: 'B weekly', used_percent: 2, reset_epoch: null },
      { id: 'c', label: 'C', used_percent: 3, reset_epoch: null },
    ] }] });
    const mixedHTML = subHTML();
    assert.deepStrictEqual(labels(mixedHTML), ['A', 'C', '周']);
    assert.ok(mixedHTML.indexOf('zone-label">B</span>') > mixedHTML.indexOf('>C</span>'));

    // 整组窗口都没有用量：不出用量格，也不留空的订阅区
    updateVendorStatus({ tiers: [tier('zhipu')] });
    updateSubscriptions({ vendors: [{ key: 'glm', status: 'ok', windows: [
      { id: '5h', window: '5h', label: '5 小时', used_percent: null },
    ] }] });
    assert.ok(!tierHTML().includes('sub-zone') && !tierHTML().includes('class="qg"'));

    // 档位去重不分大小写、忽略首尾空白；未知 key 以 key 本身作行名；非字符串档位也能显示
    assert.strictEqual(_planLabel({ key: 'antigravity', plan: ' antigravity ' }), '');
    assert.strictEqual(_planLabel({ key: 'antigravity', plan: 'Antigravity Pro' }), 'Antigravity Pro');
    assert.strictEqual(_planLabel({ key: 'newvendor', plan: 'NewVendor' }), '');
    assert.strictEqual(_planLabel({ key: 'glm', plan: 7 }), '7');
    assert.strictEqual(_planLabel(null), '');

    // 失败次数来自载荷：数字照常显示，非数字当作 0 且不进 HTML
    updateVendorStatus({ tiers: [tier('zhipu', { circuit_breaker: { state: 'open', failure_count: '3' } })] });
    assert.ok(tierHTML().includes('<span class="status-badge sb-err">熔断 ×3</span>'));
    updateVendorStatus({ tiers: [tier('zhipu', { circuit_breaker: { state: 'open', failure_count: '<img src=x onerror=alert(5)>' } })] });
    assert.ok(tierHTML().includes('<span class="status-badge sb-err">熔断</span>'));
    assert.ok(!tierHTML().includes('<img'));

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
    "function quotaWindowLabel(",
    "function renderQuotaCell(",
    "function renderQuotaBar(",
    "function cbStateClass(",
    "function cbStateLabel(",
    "function _tierZoneHTML(",
    "function _tierInitial(",
    "function fmtResetCountdown(",
    "function fmtResetShort(",
    "function _resetIn(",
    "function _windowRank(",
    "function windowLabel(",
    "function renderSubWindow(",
    "function _planLabel(",
    "function _subZoneHTML(",
    "function updateVendorStatus(",
    "function updateSubscriptions(",
    "function _renderStatusRows(",
    "function _focusedTierHandle(",
    "function _tierRowByName(",
    "function _focusTierHandle(",
    "function _zoneScrollKey(",
    "function _zoneScroll(",
    "function _restoreZoneScroll(",
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
        "  return id in captured ? { querySelectorAll() { return []; },\n"
        "  set innerHTML(v) { captured[id] = v; },\n"
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


# 最小假 DOM：只实现拖拽状态机与合并渲染用到的表面。约束与真实 DOM 保持一致：行是列表的直接
# 子节点，手柄是行下的独立节点；closest 沿父链匹配；innerHTML 重建后旧节点脱离文档，
# 其上的焦点随之丢失，游离节点不能聚焦（否则把焦点还给旧节点的错误实现也能通过）。
_DRAG_PRELUDE = textwrap.dedent(
    r"""
    class FakeEl {
      constructor(kind, classes, vendor) {
        this.kind = kind; this.classes = classes; this.dataset = vendor ? { vendor: vendor } : {};
        this.children = []; this.parentElement = null; this._h = {};
        this.isConnected = false; this.captured = null;
        const state = new Set();
        this.classList = { add: c => state.add(c), remove: c => state.delete(c), contains: c => state.has(c) || this.classes.includes(c) };
        this._state = state;
      }
      matches(sel) {
        // 只支持本页用到的简单选择器：.class、[data-attr] 及其组合
        const parts = Array.from(sel.matchAll(/\.([\w-]+)|\[data-([\w-]+)\]/g));
        return parts.length > 0 && parts.every(m => (m[1]
          ? (this.classes.includes(m[1]) || this._state.has(m[1]))
          : this.dataset[m[2]] !== undefined));
      }
      closest(sel) {
        for (let el = this; el; el = el.parentElement) if (el.matches(sel)) return el;
        return null;
      }
      addEventListener(type, fn) { (this._h[type] = this._h[type] || []).push(fn); }
      fire(type, ev) { (this._h[type] || []).forEach(fn => fn(Object.assign({ preventDefault() {}, button: 0 }, ev))); }
      setPointerCapture(id) { if (this.isConnected) this.captured = id; }
      releasePointerCapture() { this.captured = null; }
      focus() {
        if (this.isConnected && this.matches('.drag-handle')) document.activeElement = this;
      }
      getBoundingClientRect() {
        const i = this.parentElement ? this.parentElement.children.indexOf(this) : 0;
        return { top: i * 56, height: 56 };
      }
      _setConnected(flag) { this.isConnected = flag; this.children.forEach(c => c._setConnected(flag)); }
      querySelectorAll(sel) {
        return this.children.filter(c => c.matches(sel))
          .concat(...this.children.filter(c => c.kind === 'item').map(c => c.querySelectorAll(sel)));
      }
      querySelector(sel) { return this.querySelectorAll(sel)[0] || null; }
      _detach(node) {
        const p = node.parentElement;
        if (p) p.children.splice(p.children.indexOf(node), 1);
        node.parentElement = null; node._setConnected(false);
      }
      insertBefore(node, ref) {
        this._detach(node);
        const i = ref ? this.children.indexOf(ref) : this.children.length;
        this.children.splice(i, 0, node); node.parentElement = this; node._setConnected(true);
      }
      appendChild(node) {
        this._detach(node);
        this.children.push(node); node.parentElement = this; node._setConnected(true);
      }
      get lastElementChild() { return this.children[this.children.length - 1] || null; }
      get nextElementSibling() {
        const p = this.parentElement;
        return p ? (p.children[p.children.indexOf(this) + 1] || null) : null;
      }
      // 合并渲染写回 innerHTML：按 data-vendor 出现顺序重建行节点，挂上手柄与分区（订阅区 / 路由区，
      // 分区带可读写的 scrollLeft）。旧节点随之脱离文档，其上的焦点丢失；paints 记下每次重绘的行序
      set innerHTML(html) {
        this.html = html;
        const stale = this.children;
        const marks = Array.from(html.matchAll(/data-vendor="([^"]*)"/g));
        this.children = marks.map((m, i) => {
          const slice = html.slice(m.index, i + 1 < marks.length ? marks[i + 1].index : html.length);
          const item = new FakeEl('item', ['vendor-item'], m[1]);
          if (slice.includes('class="drag-handle"')) {
            const handle = new FakeEl('handle', ['drag-handle']);
            handle.parentElement = item; item.children.push(handle);
          }
          Array.from(slice.matchAll(/class="vendor-badges (sub-zone|tier-zone)"/g)).forEach(z => {
            const zone = new FakeEl('zone', ['vendor-badges', z[1]]);
            zone.scrollLeft = 0;
            zone.parentElement = item; item.children.push(zone);
          });
          return item;
        });
        this.children.forEach(c => { c.parentElement = this; });
        this._setConnected(this.isConnected);
        stale.forEach(old => { old.parentElement = null; old._setConnected(false); });
        if (stale.some(old => old === document.activeElement || old.children.includes(document.activeElement))) {
          document.activeElement = null;
        }
        this.paints.push(marks.map(m => m[1]));
      }
      get innerHTML() { return this.html || ''; }
    }
    const tierList = new FakeEl('list', ['vendor-list']);
    const subList = new FakeEl('list', ['vendor-list']);
    tierList.isConnected = subList.isConnected = true;
    tierList.paints = []; subList.paints = [];
    const docHandlers = {};
    const document = {
      activeElement: null,
      getElementById(id) { return id === 'vendor-list' ? tierList : id === 'subscription-list' ? subList : null; },
      addEventListener(type, fn) { (docHandlers[type] = docHandlers[type] || []).push(fn); },
      // 指针在列表外松开：收尾事件只到达 document，不经过 #vendor-list
      releaseOutside(pointerId) { (docHandlers.pointerup || []).forEach(fn => fn({ pointerId: pointerId, preventDefault() {} })); },
    };
    const rowOf = (vendor) => tierList.querySelectorAll('.vendor-item[data-vendor]').find(el => el.dataset.vendor === vendor);
    const handleOf = (vendor) => rowOf(vendor).querySelector('.drag-handle');
    const focusHandle = (vendor) => handleOf(vendor).focus();
    const focusedVendor = () => (document.activeElement ? document.activeElement.closest('.vendor-item').dataset.vendor : null);
    // 在某行手柄上按键，返回是否拦截了默认行为
    const keyDown = (vendor, key) => {
      const ev = { key: key, defaultPrevented: false, preventDefault() { ev.defaultPrevented = true; } };
      tierList.fire('keydown', Object.assign({ target: handleOf(vendor) }, ev));
      return ev.defaultPrevented;
    };
    const domOrder = () => _tierCurrentOrder(tierList);
    const tick = () => new Promise(resolve => setTimeout(resolve, 0));
    const wait = (ms) => new Promise(resolve => setTimeout(resolve, ms));
    const same = (a, b) => JSON.stringify(a) === JSON.stringify(b);
    const server = { order: ['zhipu', 'anthropic', 'copilot'], cb: {} };
    const serverStatus = () => ({ tiers: server.order.map(name => ({
      name: name,
      circuit_breaker: { state: server.cb[name] || 'closed', failure_count: server.cb[name] ? 2 : 0 },
      rate_limit: { is_rate_limited: false } })) });
    const puts = [];
    const pending = { gate: null, fail: false, statusDelay: 0 };
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
    function fetchJSON(url) {
      if (url !== '/api/status') return Promise.resolve({});
      const snapshot = serverStatus();                      // 响应内容取请求发出那一刻的服务端状态
      return pending.statusDelay > 0 ? wait(pending.statusDelay).then(() => snapshot) : Promise.resolve(snapshot);
    }
    // 与页面一致：任一请求失败则整次刷新失败，updateVendorStatus 不会被调用
    async function refreshOverview() {
      if (refreshFails) throw new Error('refresh failed');
      updateVendorStatus(await fetchTierStatus(), true);
      updateSubscriptions({ vendors: [] });
    }
    """
)

_DRAG_HARNESS = textwrap.dedent(
    r"""
    (async () => {
      const pointerDown = (vendor, id) => tierList.fire('pointerdown', { target: handleOf(vendor), pointerId: id, clientY: 100 });
      const pointerMove = (id, y) => tierList.fire('pointermove', { pointerId: id, clientY: y });
      const pointerUp = (id) => document.releaseOutside(id);  // 收尾事件经冒泡到达 document
      const rowTop = (vendor) => rowOf(vendor).getBoundingClientRect().top;
      const startDrag = async (vendor, id) => {
        pointerDown(vendor, id);
        pointerMove(id, 112);                                   // 越过 4px 阈值
      };
      const reset = async (order) => {
        server.order = order.slice(); server.cb = {};
        refreshFails = false; pending.fail = false; pending.gate = null; pending.statusDelay = 0;
        _tierConfirmedOrder = null; _tierOrderGen = 0;
        puts.length = 0; tierList.paints.length = 0;
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
      assert.strictEqual(tierList.querySelectorAll('.vendor-item').filter(el => el.classList.contains('dragging')).length, 0);

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

      // 七、键盘调整优先级：手柄聚焦后 ↑ 上移，乐观重绘、焦点回到同一行的新手柄、提交新顺序
      await reset(['zhipu', 'anthropic', 'copilot']);
      focusHandle('copilot');
      assert.strictEqual(focusedVendor(), 'copilot');
      assert.ok(tierList.innerHTML.includes('aria-label="调整 copilot 的优先级：第 3 位，共 3 个"'));
      const paintsBeforeKey = tierList.paints.length;
      assert.strictEqual(keyDown('copilot', 'ArrowUp'), true);   // 拦截方向键的页面滚动
      assert.deepStrictEqual(domOrder(), ['zhipu', 'copilot', 'anthropic']);
      assert.strictEqual(tierList.paints.length, paintsBeforeKey + 1);
      assert.strictEqual(focusedVendor(), 'copilot');
      assert.strictEqual(document.activeElement, handleOf('copilot'));   // 聚焦的是重建后的新节点
      assert.ok(tierList.innerHTML.includes('aria-label="调整 copilot 的优先级：第 2 位，共 3 个"'));
      await tick(); await tick(); await tick();
      assert.deepStrictEqual(server.order, ['zhipu', 'copilot', 'anthropic']);
      assert.strictEqual(tierList.paints.length, paintsBeforeKey + 2);   // PUT 成功后的刷新只重绘一次
      assert.strictEqual(focusedVendor(), 'copilot');                     // 刷新重绘后焦点仍在原行
      assert.deepStrictEqual(puts, [['zhipu', 'copilot', 'anthropic']]);
      keyDown('copilot', 'ArrowDown');                            // ↓ 下移回原位
      assert.deepStrictEqual(domOrder(), ['zhipu', 'anthropic', 'copilot']);
      await tick(); await tick(); await tick();
      assert.deepStrictEqual(server.order, ['zhipu', 'anthropic', 'copilot']);
      assert.strictEqual(focusedVendor(), 'copilot');

      // 八、两端不再移动：方向键仍被拦截，但不发 PUT、顺序不变
      await reset(['zhipu', 'anthropic', 'copilot']);
      focusHandle('zhipu');
      assert.strictEqual(keyDown('zhipu', 'ArrowUp'), true);
      focusHandle('copilot');
      assert.strictEqual(keyDown('copilot', 'ArrowDown'), true);
      assert.strictEqual(puts.length, 0);
      assert.deepStrictEqual(domOrder(), server.order);

      // 九、PUT 失败：回滚到服务端顺序，焦点仍在该行
      await reset(['zhipu', 'anthropic', 'copilot']);
      pending.fail = true;
      focusHandle('anthropic');
      keyDown('anthropic', 'ArrowUp');
      assert.deepStrictEqual(domOrder(), ['anthropic', 'zhipu', 'copilot']);
      await tick(); await tick(); await tick();
      assert.deepStrictEqual(domOrder(), ['zhipu', 'anthropic', 'copilot']);
      assert.strictEqual(focusedVendor(), 'anthropic');

      // 十、其它按键不拦截默认行为；上一次提交未决时方向键仍被拦截但不移动
      await reset(['zhipu', 'anthropic', 'copilot']);
      focusHandle('zhipu');
      assert.strictEqual(keyDown('zhipu', 'Tab'), false);
      assert.strictEqual(keyDown('zhipu', 'Enter'), false);
      const heldPut = {}; pending.gate = heldPut;                // 扣住 PUT 响应
      focusHandle('copilot');
      keyDown('copilot', 'ArrowUp');
      assert.strictEqual(_tierDrag.inFlight, true);
      assert.strictEqual(keyDown('copilot', 'ArrowUp'), true);   // 未决期间：拦截滚动
      assert.deepStrictEqual(domOrder(), ['zhipu', 'copilot', 'anthropic']);   // 但不叠加移动
      assert.deepStrictEqual(_tierStatus.tiers.map(t => t.name), domOrder());   // 被忽略的按键不改动缓存顺序
      await tick();
      assert.strictEqual(puts.length, 1);
      heldPut.settle();
      await tick(); await tick(); await tick();
      assert.deepStrictEqual(domOrder(), server.order);
      assert.strictEqual(focusedVendor(), 'copilot');

      // 十一、指针按在另一行手柄上时按方向键：不移动（指针与键盘不抢同一次重排）
      await reset(['zhipu', 'anthropic', 'copilot']);
      focusHandle('copilot');
      pointerDown('anthropic', 1);
      assert.strictEqual(keyDown('copilot', 'ArrowUp'), true);
      assert.deepStrictEqual(domOrder(), server.order);
      pointerUp(1);
      await tick();
      assert.strictEqual(puts.length, 0);

      // 十二、连按方向键：上一次提交触发的状态请求迟到时，不得把行序画回上一步
      await reset(['zhipu', 'anthropic', 'copilot']);
      pending.statusDelay = 40;                                 // 状态请求比 PUT 慢
      focusHandle('zhipu');
      keyDown('zhipu', 'ArrowDown');                             // 第 1 步：[anthropic, zhipu, copilot]
      await tick(); await tick();                                // PUT#1 成功，其状态请求已在途
      assert.deepStrictEqual(server.order, ['anthropic', 'zhipu', 'copilot']);
      focusHandle('zhipu');
      keyDown('zhipu', 'ArrowDown');                             // 第 2 步：[anthropic, copilot, zhipu]
      await tick(); await tick();                                // PUT#2 成功
      assert.deepStrictEqual(server.order, ['anthropic', 'copilot', 'zhipu']);
      await wait(150);                                           // 两次状态请求先后返回
      assert.deepStrictEqual(domOrder(), server.order);
      const step2 = ['anthropic', 'copilot', 'zhipu'];
      const firstStep2 = tierList.paints.findIndex(p => same(p, step2));
      assert.ok(firstStep2 >= 0);
      assert.ok(tierList.paints.slice(firstStep2).every(p => same(p, step2)),   // 第 2 步之后不再画回第 1 步
        JSON.stringify(tierList.paints));
      pending.statusDelay = 0;
      assert.strictEqual(focusedVendor(), 'zhipu');

      // 十三、按住手柄未移动时行被重绘：原节点已脱离，按名字重新定位后再拖，不产生重复行
      await reset(['zhipu', 'anthropic', 'copilot']);
      pointerDown('copilot', 1);
      const held = _tierDrag.srcEl;
      await refreshOverview();                                   // 例如上一次重排后的刷新此时落地
      assert.strictEqual(held.isConnected, false);
      pointerMove(1, 112);
      pointerMove(1, rowTop('anthropic') - 10);
      assert.deepStrictEqual(domOrder(), ['zhipu', 'copilot', 'anthropic']);
      assert.strictEqual(_tierDrag.srcEl, rowOf('copilot'));
      pointerUp(1);
      await tick(); await tick();
      assert.deepStrictEqual(puts, [['zhipu', 'copilot', 'anthropic']]);       // 载荷无重复 vendor
      assert.deepStrictEqual(domOrder(), server.order);

      // 十四、按住的行在重绘后已不存在（tier 被移除）：本次拖拽作废
      await reset(['zhipu', 'anthropic', 'copilot']);
      pointerDown('copilot', 1);
      updateVendorStatus({ tiers: [tier('zhipu'), tier('anthropic')] });
      pointerMove(1, 112);
      assert.strictEqual(_tierDrag.active, false);
      assert.strictEqual(_tierDrag.srcEl, null);
      pointerUp(1);
      await tick();
      assert.strictEqual(puts.length, 0);

      // 十五、刷新只重绘一次：行数据与订阅数据到齐后统一重绘
      await reset(['zhipu', 'anthropic', 'copilot']);
      const paints0 = tierList.paints.length;
      updateVendorStatus(serverStatus(), true);                  // 延后重绘
      assert.strictEqual(tierList.paints.length, paints0);
      updateSubscriptions({ vendors: [] });
      assert.strictEqual(tierList.paints.length, paints0 + 1);

      // 十六、重绘保留分区的横向滚动位置：放不下的行被用户滚到右侧后，刷新不把它弹回起点
      await reset(['zhipu', 'anthropic', 'copilot']);
      const glmSub = { vendors: [{ key: 'glm', plan: 'Pro', status: 'ok', windows: [] }] };
      updateSubscriptions(glmSub);
      const zoneOf = (vendor, kind) => rowOf(vendor).querySelectorAll('.vendor-badges').find(z => z.classList.contains(kind));
      zoneOf('zhipu', 'sub-zone').scrollLeft = 120;
      zoneOf('zhipu', 'tier-zone').scrollLeft = 30;                // 同一行的两个分区各记各的
      updateVendorStatus(serverStatus(), true);
      updateSubscriptions(glmSub);                                 // 重绘：行节点全部重建
      assert.strictEqual(zoneOf('zhipu', 'sub-zone').scrollLeft, 120);
      assert.strictEqual(zoneOf('zhipu', 'tier-zone').scrollLeft, 30);
      assert.strictEqual(zoneOf('anthropic', 'tier-zone').scrollLeft, 0);
      assert.strictEqual(zoneOf('copilot', 'tier-zone').scrollLeft, 0);
      // 行已不在列表里：它的滚动位置不会串到别的行
      updateVendorStatus({ tiers: [tier('anthropic'), tier('copilot')] });
      assert.strictEqual(zoneOf('anthropic', 'tier-zone').scrollLeft, 0);
      console.log('tier drag harness OK');
    })().catch(err => { console.error(err); process.exit(1); });
    """
)


@pytest.mark.skipif(_NODE is None, reason="node 不可用")
def test_tier_drag_behavior(client: TestClient, tmp_path):
    """拖拽状态机在最小假 DOM 上执行：列表外松开、多指重入、过期缓存补绘、失败回滚、键盘调整."""
    script = _inline_script(client.get("/dashboard").text)
    drag_sources = [
        _extract_block(script, "const _tierDrag = {", "};"),
        _extract_block(script, "const _TIER_DRAG_THRESHOLD = ", ";"),
        _extract_block(
            script, "let _tierOrderGen = 0;", "let _tierConfirmedOrder = null;"
        ),
    ]
    drag_sources += [
        _extract_function(script, signature)
        for signature in (
            "function _tierCurrentOrder(",
            "function _tierDragMoveTo(",
            "function _tierDragReset(",
            "function initTierDrag(",
            "function _tierMove(",
            "function _syncTierOrder(",
            "async function fetchTierStatus(",
            "function persistTierOrder(",
            "function _tierRevertToList(",
        )
    ]
    prelude = (
        "const tier = (name) => ({ name: name, circuit_breaker: { state: 'closed', "
        "failure_count: 0 }, rate_limit: { is_rate_limited: false } });\n"
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


def test_status_refresh_guards_confirmed_order(client: TestClient):
    """状态请求发出后才被服务端确认的重排：迟到的快照不得把行序画回上一步（连按方向键）.

    fetchTierStatus 记下请求发出时的行序代次，返回时若代次已变，只采用快照里的状态字段，
    行序以已确认的顺序为准；三处拉取路由状态的入口都必须走它。
    """
    script = _inline_script(client.get("/dashboard").text)
    guard = _extract_function(script, "function fetchTierStatus(")
    assert "gen !== _tierOrderGen" in guard
    assert "_tierConfirmedOrder.map(" in guard
    put = _extract_function(script, "function persistTierOrder(")
    assert "_tierConfirmedOrder = names;" in put and "_tierOrderGen++;" in put
    refresh = _extract_function(script, "async function refreshOverview(")
    assert "fetchTierStatus()," in refresh
    # 行数据与订阅数据到齐后只重绘一次（连续替换行节点会让键盘焦点环闪烁）
    assert "updateVendorStatus(status, true);" in refresh
    for signature in ("function _tierRevertToList(", "function resetVendorStatus("):
        source = _extract_function(script, signature)
        assert "fetchTierStatus()" in source
        assert "fetchJSON('/api/status')" not in source
