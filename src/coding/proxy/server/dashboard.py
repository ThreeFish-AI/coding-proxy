"""Dashboard 路由 — 流量与用量可视化看板."""

from __future__ import annotations

import json
import logging
from typing import Any

from fastapi import Request
from fastapi.responses import HTMLResponse, Response

from ..logging.db import TimePeriod

logger = logging.getLogger(__name__)

# ── HTML 模板 ──────────────────────────────────────────────────────────────

_DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>coding-proxy Dashboard</title>
  <script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.3/dist/chart.umd.min.js"></script>
  <style>
    :root {
      --bg: #0d1117;
      --bg-card: #161b22;
      --bg-card-hover: #1c2128;
      --border: #30363d;
      --text-primary: #e6edf3;
      --text-secondary: #8b949e;
      --accent-blue: #58a6ff;
      --accent-green: #3fb950;
      --accent-yellow: #d29922;
      --accent-red: #f85149;
      --accent-purple: #bc8cff;
      --accent-orange: #ffa657;
      --radius: 8px;
      --shadow: 0 1px 3px rgba(0,0,0,.4);
    }
    *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
    body {
      background: var(--bg);
      color: var(--text-primary);
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", "Helvetica Neue", Arial, sans-serif;
      font-size: 14px;
      line-height: 1.5;
      min-height: 100vh;
    }
    /* ── 头部 ── */
    header {
      background: var(--bg-card);
      border-bottom: 1px solid var(--border);
      padding: 14px 24px;
      display: flex;
      align-items: center;
      justify-content: space-between;
      position: sticky;
      top: 0;
      z-index: 100;
    }
    .header-left { display: flex; align-items: center; gap: 12px; }
    .logo {
      width: 28px; height: 28px;
      background: linear-gradient(135deg, var(--accent-blue), var(--accent-purple));
      border-radius: 6px;
      display: flex; align-items: center; justify-content: center;
      font-size: 16px; font-weight: bold; color: #fff;
    }
    h1 { font-size: 16px; font-weight: 600; color: var(--text-primary); }
    .header-right { display: flex; align-items: center; gap: 12px; }
    .badge {
      font-size: 11px; padding: 2px 8px;
      border-radius: 12px;
      background: rgba(88,166,255,.15);
      color: var(--accent-blue);
      border: 1px solid rgba(88,166,255,.25);
    }
    .refresh-time { font-size: 12px; color: var(--text-secondary); }
    .btn-refresh {
      padding: 5px 12px; border-radius: var(--radius);
      background: rgba(48,54,61,.6);
      border: 1px solid var(--border);
      color: var(--text-primary);
      font-size: 12px; cursor: pointer;
      transition: background .15s;
    }
    .btn-refresh:hover { background: var(--bg-card-hover); }
    /* ── 主内容 ── */
    main { padding: 20px 24px; max-width: 1400px; margin: 0 auto; }
    /* ── KPI 卡片 ── */
    .kpi-grid {
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
      gap: 14px;
      margin-bottom: 20px;
    }
    .kpi-card {
      background: var(--bg-card);
      border: 1px solid var(--border);
      border-radius: var(--radius);
      padding: 16px 20px;
      box-shadow: var(--shadow);
      transition: background .15s;
    }
    .kpi-card:hover { background: var(--bg-card-hover); }
    .kpi-label { font-size: 12px; color: var(--text-secondary); margin-bottom: 6px; }
    .kpi-value { font-size: 26px; font-weight: 700; line-height: 1.2; }
    .kpi-sub { font-size: 12px; color: var(--text-secondary); margin-top: 4px; }
    .kpi-delta { font-size: 12px; margin-top: 4px; }
    .kpi-delta.up { color: var(--accent-green); }
    .kpi-delta.down { color: var(--accent-red); }
    .color-blue { color: var(--accent-blue); }
    .color-green { color: var(--accent-green); }
    .color-yellow { color: var(--accent-yellow); }
    .color-red { color: var(--accent-red); }
    .color-purple { color: var(--accent-purple); }
    /* ── 图表网格 ── */
    .charts-grid {
      display: grid;
      grid-template-columns: 1fr 2fr;
      gap: 14px;
      margin-bottom: 14px;
    }
    .charts-grid-3 {
      display: grid;
      grid-template-columns: 1fr 1fr;
      gap: 14px;
      margin-bottom: 14px;
    }
    @media (max-width: 900px) {
      .charts-grid, .charts-grid-3 { grid-template-columns: 1fr; }
    }
    .card {
      background: var(--bg-card);
      border: 1px solid var(--border);
      border-radius: var(--radius);
      padding: 16px 20px;
      box-shadow: var(--shadow);
    }
    .card-title {
      font-size: 13px; font-weight: 600;
      color: var(--text-secondary);
      text-transform: uppercase;
      letter-spacing: .5px;
      margin-bottom: 14px;
      display: flex; align-items: center; justify-content: space-between;
    }
    .chart-wrap { position: relative; height: 220px; }
    .chart-wrap-lg { position: relative; height: 240px; }
    /* ── 供应商状态 ── */
    .vendor-list { display: flex; flex-direction: column; gap: 10px; }
    .vendor-item {
      display: flex; align-items: center; justify-content: space-between;
      padding: 10px 12px;
      background: rgba(255,255,255,.03);
      border: 1px solid var(--border);
      border-radius: 6px;
    }
    .vendor-name { font-weight: 600; font-size: 13px; min-width: 80px; }
    .vendor-badges { display: flex; gap: 6px; flex-wrap: wrap; }
    .status-badge {
      font-size: 11px; padding: 2px 7px;
      border-radius: 10px;
      font-weight: 500;
    }
    .sb-ok { background: rgba(63,185,80,.15); color: var(--accent-green); border: 1px solid rgba(63,185,80,.25); }
    .sb-warn { background: rgba(210,153,34,.15); color: var(--accent-yellow); border: 1px solid rgba(210,153,34,.25); }
    .sb-err { background: rgba(248,81,73,.15); color: var(--accent-red); border: 1px solid rgba(248,81,73,.25); }
    .sb-info { background: rgba(88,166,255,.15); color: var(--accent-blue); border: 1px solid rgba(88,166,255,.25); }
    .quota-bar-wrap { flex: 1; margin: 0 12px; max-width: 120px; }
    .quota-bar-bg {
      height: 5px; border-radius: 3px;
      background: rgba(255,255,255,.08);
      overflow: hidden;
    }
    .quota-bar-fill { height: 100%; border-radius: 3px; transition: width .4s; }
    .quota-pct { font-size: 11px; color: var(--text-secondary); margin-top: 2px; text-align: right; }
    /* ── 故障转移表 ── */
    .ft-table-wrap { overflow-x: auto; }
    table { width: 100%; border-collapse: collapse; }
    th {
      text-align: left; font-size: 12px; color: var(--text-secondary);
      font-weight: 500; padding: 6px 10px;
      border-bottom: 1px solid var(--border);
    }
    td { padding: 8px 10px; font-size: 13px; border-bottom: 1px solid rgba(48,54,61,.5); }
    tr:last-child td { border-bottom: none; }
    tr:hover td { background: rgba(255,255,255,.02); }
    .tag-vendor {
      display: inline-block;
      font-size: 11px; padding: 1px 7px;
      border-radius: 10px;
      background: rgba(188,140,255,.15);
      color: var(--accent-purple);
      border: 1px solid rgba(188,140,255,.25);
    }
    .arrow { color: var(--text-secondary); margin: 0 4px; }
    /* ── 空态 ── */
    .empty {
      text-align: center; padding: 32px;
      color: var(--text-secondary); font-size: 13px;
    }
    /* ── 加载态 ── */
    .loading { opacity: .4; pointer-events: none; }
  </style>
</head>
<body>
<header>
  <div class="header-left">
    <div class="logo">C</div>
    <h1>coding-proxy Dashboard</h1>
    <span class="badge" id="version-badge">v-.-.-</span>
  </div>
  <div class="header-right">
    <span class="refresh-time" id="refresh-time">正在加载…</span>
    <button class="btn-refresh" onclick="refresh()">⟳ 刷新</button>
  </div>
</header>

<main>
  <!-- KPI 卡片 -->
  <div class="kpi-grid" id="kpi-grid">
    <div class="kpi-card"><div class="kpi-label">今日请求数</div><div class="kpi-value color-blue" id="kpi-req-today">–</div><div class="kpi-sub" id="kpi-req-week">本周 –</div></div>
    <div class="kpi-card"><div class="kpi-label">今日 Token 总量</div><div class="kpi-value color-purple" id="kpi-tok-today">–</div><div class="kpi-sub" id="kpi-tok-week">本周 –</div></div>
    <div class="kpi-card"><div class="kpi-label">今日输出 Token</div><div class="kpi-value color-green" id="kpi-out-today">–</div><div class="kpi-sub" id="kpi-out-week">本周 –</div></div>
    <div class="kpi-card"><div class="kpi-label">今日费用估算</div><div class="kpi-value color-yellow" id="kpi-cost-today">–</div><div class="kpi-sub" id="kpi-cost-week">本周 –</div></div>
    <div class="kpi-card"><div class="kpi-label">故障转移（今日）</div><div class="kpi-value color-red" id="kpi-fo-today">–</div><div class="kpi-sub" id="kpi-fo-week">本周 –</div></div>
    <div class="kpi-card"><div class="kpi-label">平均延迟（今日）</div><div class="kpi-value" id="kpi-lat-today">–</div><div class="kpi-sub" id="kpi-lat-week">本周 –</div></div>
  </div>

  <!-- 供应商状态 + 趋势折线图 -->
  <div class="charts-grid">
    <div class="card">
      <div class="card-title">供应商状态</div>
      <div class="vendor-list" id="vendor-list">
        <div class="empty">加载中…</div>
      </div>
    </div>
    <div class="card">
      <div class="card-title">近 7 天请求量趋势</div>
      <div class="chart-wrap-lg">
        <canvas id="chart-timeline"></canvas>
      </div>
    </div>
  </div>

  <!-- 供应商分布 + Token 类型分布 -->
  <div class="charts-grid-3">
    <div class="card">
      <div class="card-title">供应商请求分布（近 7 天）</div>
      <div class="chart-wrap">
        <canvas id="chart-vendor-dist"></canvas>
      </div>
    </div>
    <div class="card">
      <div class="card-title">Token 类型分布（近 7 天）</div>
      <div class="chart-wrap">
        <canvas id="chart-token-type"></canvas>
      </div>
    </div>
  </div>

  <!-- 故障转移明细表 -->
  <div class="card">
    <div class="card-title">故障转移明细</div>
    <div class="ft-table-wrap">
      <table>
        <thead>
          <tr>
            <th>来源供应商</th>
            <th>目标供应商</th>
            <th>次数</th>
          </tr>
        </thead>
        <tbody id="ft-tbody">
          <tr><td colspan="3" class="empty">加载中…</td></tr>
        </tbody>
      </table>
    </div>
  </div>
</main>

<script>
// ── 颜色配置 ──────────────────────────────────────────────
const VENDOR_COLORS = [
  '#58a6ff','#bc8cff','#3fb950','#ffa657','#f85149',
  '#79c0ff','#d2a8ff','#56d364','#ffb77c','#ff7b72',
];
const TOKEN_COLORS = {
  input: '#58a6ff',
  output: '#3fb950',
  cache_creation: '#d29922',
  cache_read: '#bc8cff',
};

// ── 工具函数 ──────────────────────────────────────────────
function fmtTokens(n) {
  if (!n) return '0';
  if (n >= 1e9) return (n/1e9).toFixed(2).replace(/\\.?0+$/,'') + 'B';
  if (n >= 1e6) return (n/1e6).toFixed(2).replace(/\\.?0+$/,'') + 'M';
  if (n >= 1e3) return (n/1e3).toFixed(1).replace(/\\.?0+$/,'') + 'K';
  return String(n);
}
function fmtNum(n) { return n == null ? '–' : n.toLocaleString(); }
function now() {
  return new Date().toLocaleTimeString('zh-CN', {hour:'2-digit',minute:'2-digit',second:'2-digit'});
}

// ── Chart.js 全局默认 ─────────────────────────────────────
Chart.defaults.color = '#8b949e';
Chart.defaults.borderColor = '#30363d';
Chart.defaults.font.family = '-apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif';
Chart.defaults.font.size = 12;

// ── 图表实例 ──────────────────────────────────────────────
let chartTimeline = null;
let chartVendorDist = null;
let chartTokenType = null;

function destroyCharts() {
  [chartTimeline, chartVendorDist, chartTokenType].forEach(c => c && c.destroy());
  chartTimeline = chartVendorDist = chartTokenType = null;
}

// ── 数据拉取 ──────────────────────────────────────────────
async function fetchJSON(url) {
  const r = await fetch(url);
  if (!r.ok) throw new Error(r.status);
  return r.json();
}

// ── KPI 更新 ──────────────────────────────────────────────
function updateKPI(summary) {
  const t = summary.today, w = summary.week, m = summary.month;

  document.getElementById('kpi-req-today').textContent = fmtNum(t.requests);
  document.getElementById('kpi-req-week').textContent = '本周 ' + fmtNum(w.requests);

  const tokT = t.tokens, tokW = w.tokens;
  const totalT = tokT.input + tokT.output + tokT.cache_creation + tokT.cache_read;
  const totalW = tokW.input + tokW.output + tokW.cache_creation + tokW.cache_read;
  document.getElementById('kpi-tok-today').textContent = fmtTokens(totalT);
  document.getElementById('kpi-tok-week').textContent = '本周 ' + fmtTokens(totalW);

  document.getElementById('kpi-out-today').textContent = fmtTokens(tokT.output);
  document.getElementById('kpi-out-week').textContent = '本周 ' + fmtTokens(tokW.output);

  document.getElementById('kpi-cost-today').textContent = t.cost || '–';
  document.getElementById('kpi-cost-week').textContent = '本周 ' + (w.cost || '–');

  document.getElementById('kpi-fo-today').textContent = fmtNum(t.failovers);
  document.getElementById('kpi-fo-week').textContent = '本周 ' + fmtNum(w.failovers);

  document.getElementById('kpi-lat-today').textContent = t.avg_duration_ms ? t.avg_duration_ms + 'ms' : '–';
  document.getElementById('kpi-lat-week').textContent = '本周 ' + (w.avg_duration_ms ? w.avg_duration_ms + 'ms' : '–');
}

// ── 供应商状态 ────────────────────────────────────────────
function cbStateClass(state) {
  if (!state) return 'sb-ok';
  const s = state.toUpperCase();
  if (s === 'OPEN') return 'sb-err';
  if (s === 'HALF_OPEN') return 'sb-warn';
  return 'sb-ok';
}
function cbStateLabel(state) {
  if (!state) return 'CLOSED';
  const s = state.toUpperCase();
  if (s === 'OPEN') return '熔断';
  if (s === 'HALF_OPEN') return '半开';
  return '正常';
}
function quotaClass(pct) {
  if (pct >= 90) return 'sb-err';
  if (pct >= 70) return 'sb-warn';
  return 'sb-ok';
}
function quotaBarColor(pct) {
  if (pct >= 90) return 'var(--accent-red)';
  if (pct >= 70) return 'var(--accent-yellow)';
  return 'var(--accent-green)';
}

function updateVendorStatus(status) {
  const tiers = status.tiers || [];
  const list = document.getElementById('vendor-list');
  if (!tiers.length) {
    list.innerHTML = '<div class="empty">无供应商数据</div>';
    return;
  }
  list.innerHTML = tiers.map(tier => {
    const cb = tier.circuit_breaker || {};
    const qg = tier.quota_guard || {};
    const wqg = tier.weekly_quota_guard || {};
    const cbClass = cbStateClass(cb.state);
    const cbLabel = cbStateLabel(cb.state);
    const pct = qg.usage_percent != null ? Math.round(qg.usage_percent) : null;
    const wpct = wqg.usage_percent != null ? Math.round(wqg.usage_percent) : null;

    let quotaHTML = '';
    if (pct != null) {
      quotaHTML += `
        <span class="status-badge ${quotaClass(pct)}">日配额 ${pct}%</span>
        <div class="quota-bar-wrap">
          <div class="quota-bar-bg"><div class="quota-bar-fill" style="width:${Math.min(pct,100)}%;background:${quotaBarColor(pct)}"></div></div>
        </div>`;
    }
    if (wpct != null) {
      quotaHTML += `<span class="status-badge ${quotaClass(wpct)}">周配额 ${wpct}%</span>`;
    }

    const rlInfo = tier.rate_limit || {};
    const rlHtml = rlInfo.limited
      ? `<span class="status-badge sb-warn">限速中</span>` : '';

    return `<div class="vendor-item">
      <span class="vendor-name">${tier.name}</span>
      <div class="vendor-badges">
        <span class="status-badge ${cbClass}">${cbLabel}${cb.failure_count ? ' ×'+cb.failure_count : ''}</span>
        ${quotaHTML}
        ${rlHtml}
      </div>
    </div>`;
  }).join('');
}

// ── 时序折线图 ────────────────────────────────────────────
function buildTimeline(rows) {
  // 按 vendor 分组，按 date 汇总
  const vendorDateMap = {}; // vendor → {date → count}
  const allDates = new Set();
  for (const r of rows) {
    const v = r.vendor, d = r.date;
    if (!v || !d) continue;
    if (!vendorDateMap[v]) vendorDateMap[v] = {};
    vendorDateMap[v][d] = (vendorDateMap[v][d] || 0) + (r.total_requests || 0);
    allDates.add(d);
  }
  const dates = [...allDates].sort();
  const vendors = Object.keys(vendorDateMap).sort();

  const datasets = vendors.map((v, i) => ({
    label: v,
    data: dates.map(d => vendorDateMap[v][d] || 0),
    borderColor: VENDOR_COLORS[i % VENDOR_COLORS.length],
    backgroundColor: VENDOR_COLORS[i % VENDOR_COLORS.length] + '22',
    fill: true,
    tension: .3,
    pointRadius: 3,
    pointHoverRadius: 5,
  }));

  if (chartTimeline) chartTimeline.destroy();
  const ctx = document.getElementById('chart-timeline').getContext('2d');
  chartTimeline = new Chart(ctx, {
    type: 'line',
    data: { labels: dates, datasets },
    options: {
      responsive: true, maintainAspectRatio: false,
      interaction: { mode: 'index', intersect: false },
      plugins: { legend: { position: 'bottom', labels: { boxWidth: 10, padding: 12 } } },
      scales: {
        x: { grid: { color: '#30363d' } },
        y: { grid: { color: '#30363d' }, beginAtZero: true, ticks: { precision: 0 } },
      },
    },
  });
}

// ── 供应商分布环形图 ──────────────────────────────────────
function buildVendorDist(rows) {
  const vendorTotals = {};
  for (const r of rows) {
    const v = r.vendor;
    if (!v) continue;
    vendorTotals[v] = (vendorTotals[v] || 0) + (r.total_requests || 0);
  }
  const labels = Object.keys(vendorTotals).sort((a,b) => vendorTotals[b]-vendorTotals[a]);
  const data = labels.map(v => vendorTotals[v]);

  if (chartVendorDist) chartVendorDist.destroy();
  const ctx = document.getElementById('chart-vendor-dist').getContext('2d');
  if (!labels.length) {
    ctx.canvas.parentElement.innerHTML = '<div class="empty">暂无数据</div>';
    return;
  }
  chartVendorDist = new Chart(ctx, {
    type: 'doughnut',
    data: {
      labels,
      datasets: [{
        data,
        backgroundColor: labels.map((_,i) => VENDOR_COLORS[i % VENDOR_COLORS.length]),
        borderWidth: 0,
        hoverOffset: 6,
      }],
    },
    options: {
      responsive: true, maintainAspectRatio: false,
      plugins: {
        legend: { position: 'bottom', labels: { boxWidth: 10, padding: 10 } },
        tooltip: { callbacks: { label: ctx => ` ${ctx.label}: ${ctx.raw} 次` } },
      },
    },
  });
}

// ── Token 类型堆叠柱图 ────────────────────────────────────
function buildTokenType(rows) {
  // 按 date 汇总 token 类型
  const byDate = {};
  const allDates = new Set();
  for (const r of rows) {
    const d = r.date;
    if (!d) continue;
    if (!byDate[d]) byDate[d] = {input:0,output:0,cache_creation:0,cache_read:0};
    byDate[d].input += r.total_input || 0;
    byDate[d].output += r.total_output || 0;
    byDate[d].cache_creation += r.total_cache_creation || 0;
    byDate[d].cache_read += r.total_cache_read || 0;
    allDates.add(d);
  }
  const dates = [...allDates].sort();

  if (chartTokenType) chartTokenType.destroy();
  const ctx = document.getElementById('chart-token-type').getContext('2d');
  if (!dates.length) {
    ctx.canvas.parentElement.innerHTML = '<div class="empty">暂无数据</div>';
    return;
  }

  const typeLabels = { input:'输入', output:'输出', cache_creation:'缓存写入', cache_read:'缓存读取' };
  const datasets = Object.entries(typeLabels).map(([key, label]) => ({
    label,
    data: dates.map(d => byDate[d]?.[key] || 0),
    backgroundColor: TOKEN_COLORS[key],
    stack: 'tokens',
    borderWidth: 0,
  }));

  chartTokenType = new Chart(ctx, {
    type: 'bar',
    data: { labels: dates, datasets },
    options: {
      responsive: true, maintainAspectRatio: false,
      plugins: { legend: { position: 'bottom', labels: { boxWidth: 10, padding: 10 } } },
      scales: {
        x: { stacked: true, grid: { color: '#30363d' } },
        y: {
          stacked: true, grid: { color: '#30363d' }, beginAtZero: true,
          ticks: { callback: v => fmtTokens(v) },
        },
      },
    },
  });
}

// ── 故障转移明细表 ────────────────────────────────────────
function updateFtTable(failoverStats) {
  const tbody = document.getElementById('ft-tbody');
  if (!failoverStats || !failoverStats.length) {
    tbody.innerHTML = '<tr><td colspan="3" class="empty">暂无故障转移记录</td></tr>';
    return;
  }
  tbody.innerHTML = failoverStats.map(r => `
    <tr>
      <td><span class="tag-vendor">${r.failover_from || 'unknown'}</span></td>
      <td><span class="tag-vendor">${r.vendor || ''}</span></td>
      <td>${fmtNum(r.count)}</td>
    </tr>`).join('');
}

// ── 主刷新逻辑 ────────────────────────────────────────────
let refreshing = false;
async function refresh() {
  if (refreshing) return;
  refreshing = true;
  document.getElementById('refresh-time').textContent = '刷新中…';
  try {
    const [summary, timeline, status] = await Promise.all([
      fetchJSON('/api/dashboard/summary'),
      fetchJSON('/api/dashboard/timeline?days=7'),
      fetchJSON('/api/status'),
    ]);

    // 版本号
    if (summary.version) {
      document.getElementById('version-badge').textContent = 'v' + summary.version;
    }

    updateKPI(summary);
    updateVendorStatus(status);

    const rows = timeline.rows || [];
    buildTimeline(rows);
    buildVendorDist(rows);
    buildTokenType(rows);

    updateFtTable(summary.failover_stats || []);

    document.getElementById('refresh-time').textContent = '上次刷新: ' + now();
  } catch (e) {
    console.error('Dashboard refresh error:', e);
    document.getElementById('refresh-time').textContent = '刷新失败 ' + now();
  } finally {
    refreshing = false;
  }
}

// 页面加载 + 每 30 秒自动刷新
refresh();
setInterval(refresh, 30000);
</script>
</body>
</html>
"""


# ── 数据计算工具 ──────────────────────────────────────────────────────────


def _sum_rows(rows: list[dict]) -> dict:
    """汇总一组查询行的关键指标."""
    total_requests = 0
    total_input = 0
    total_output = 0
    total_cache_creation = 0
    total_cache_read = 0
    total_failovers = 0
    weighted_duration = 0.0

    for row in rows:
        req = row.get("total_requests") or 0
        total_requests += req
        total_input += row.get("total_input") or 0
        total_output += row.get("total_output") or 0
        total_cache_creation += row.get("total_cache_creation") or 0
        total_cache_read += row.get("total_cache_read") or 0
        total_failovers += row.get("total_failovers") or 0
        weighted_duration += (row.get("avg_duration_ms") or 0) * req

    avg_ms = int(weighted_duration / total_requests) if total_requests else 0
    return {
        "requests": total_requests,
        "tokens": {
            "input": total_input,
            "output": total_output,
            "cache_creation": total_cache_creation,
            "cache_read": total_cache_read,
        },
        "failovers": total_failovers,
        "avg_duration_ms": avg_ms,
    }


def _compute_cost_str(rows: list[dict], pricing_table: Any) -> str:
    """计算多行的总费用字符串."""
    if pricing_table is None:
        return "–"
    cost_totals: dict = {}
    for row in rows:
        vendor = str(row.get("vendor") or "")
        model = str(row.get("model_served") or "")
        cv = pricing_table.compute_cost(
            vendor,
            model,
            row.get("total_input") or 0,
            row.get("total_output") or 0,
            row.get("total_cache_creation") or 0,
            row.get("total_cache_read") or 0,
        )
        if cv is not None:
            cur = cv.currency
            cost_totals[cur] = cost_totals.get(cur, 0.0) + cv.amount

    if not cost_totals:
        return "–"
    return " + ".join(f"{cur.symbol}{amt:.4f}" for cur, amt in cost_totals.items())


# ── 路由注册 ──────────────────────────────────────────────────────────────


def register_dashboard_routes(app: Any) -> None:
    """注册 Dashboard 相关路由."""
    from .. import __version__

    @app.get("/dashboard", response_class=HTMLResponse, include_in_schema=False)
    async def dashboard() -> HTMLResponse:
        """返回 Dashboard HTML 页面."""
        return HTMLResponse(content=_DASHBOARD_HTML)

    @app.get("/api/dashboard/summary")
    async def dashboard_summary(request: Request) -> Response:
        """返回 Dashboard 汇总数据（今日 / 本周 / 本月）."""
        token_logger = getattr(request.app.state, "token_logger", None)
        pricing_table = getattr(request.app.state, "pricing_table", None)

        if token_logger is None:
            return Response(
                content=b'{"error":"token_logger not available"}',
                status_code=503,
                media_type="application/json",
            )

        try:
            # 今日（最近 1 天）
            today_rows = await token_logger.query_usage(
                period=TimePeriod.DAY, count=1
            )
            # 本周（最近 7 天）
            week_rows = await token_logger.query_usage(
                period=TimePeriod.DAY, count=7
            )
            # 本月（最近 30 天）
            month_rows = await token_logger.query_usage(
                period=TimePeriod.DAY, count=30
            )
            # 故障转移（最近 7 天）
            failover_stats = await token_logger.query_failover_stats(days=7)
        except Exception as exc:
            logger.error("dashboard_summary query error: %s", exc, exc_info=True)
            return Response(
                content=b'{"error":"query failed"}',
                status_code=500,
                media_type="application/json",
            )

        today = _sum_rows(today_rows)
        week = _sum_rows(week_rows)
        month = _sum_rows(month_rows)

        today["cost"] = _compute_cost_str(today_rows, pricing_table)
        week["cost"] = _compute_cost_str(week_rows, pricing_table)
        month["cost"] = _compute_cost_str(month_rows, pricing_table)

        result = {
            "version": __version__,
            "today": today,
            "week": week,
            "month": month,
            "failover_stats": failover_stats,
        }
        return Response(
            content=json.dumps(result, ensure_ascii=False).encode(),
            status_code=200,
            media_type="application/json",
        )

    @app.get("/api/dashboard/timeline")
    async def dashboard_timeline(request: Request, days: int = 7) -> Response:
        """返回按天分组的时序数据（用于图表绘制）."""
        token_logger = getattr(request.app.state, "token_logger", None)

        if token_logger is None:
            return Response(
                content=b'{"error":"token_logger not available"}',
                status_code=503,
                media_type="application/json",
            )

        days = max(1, min(days, 90))  # 限制范围 1~90 天

        try:
            rows = await token_logger.query_usage(
                period=TimePeriod.DAY, count=days
            )
        except Exception as exc:
            logger.error("dashboard_timeline query error: %s", exc, exc_info=True)
            return Response(
                content=b'{"error":"query failed"}',
                status_code=500,
                media_type="application/json",
            )

        result = {
            "period": "day",
            "count": days,
            "rows": rows,
        }
        return Response(
            content=json.dumps(result, ensure_ascii=False).encode(),
            status_code=200,
            media_type="application/json",
        )
