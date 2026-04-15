"""Dashboard 路由 — 流量与用量可视化看板."""

from __future__ import annotations

import json
import logging
from typing import Any

from fastapi import Request
from fastapi.responses import HTMLResponse, Response

from ..logging.db import TimePeriod


# ── Favicon (16×16, 蓝紫渐变) ────────────────────────────────────────────
def _build_favicon() -> bytes:
    """程序化生成 16×16 ICO，蓝紫渐变与 Dashboard Logo 一致."""
    import struct

    width, height = 16, 16
    pixel_rows: list[bytes] = []
    cx, cy = width / 2.0, height / 2.0
    for y in range(height - 1, -1, -1):  # BMP bottom-up
        row = bytearray()
        for x in range(width):
            dx = x - cx + 0.5
            dy = y - cy + 0.5
            if dx * dx + dy * dy > (width / 2.0) ** 2:
                row.extend([0, 0, 0, 0])  # 圆外透明
            else:
                t = (x + (height - 1 - y)) / (width + height - 2)
                r = int(88 + (188 - 88) * t)
                g = int(166 + (140 - 166) * t)
                b = 255
                row.extend([b, g, r, 255])  # BGRA
        pixel_rows.append(bytes(row))

    bmp_hdr = struct.pack(
        "<IIIHHIIIIII", 40, width, height * 2, 1, 32, 0, 0, 0, 0, 0, 0
    )
    px_data = b"".join(pixel_rows)
    mask_data = b"\x00\x00\x00\x00" * height
    image_data = bmp_hdr + px_data + mask_data

    ico_hdr = struct.pack("<HHH", 0, 1, 1)
    dir_entry = struct.pack(
        "<BBBBHHII", width, height, 0, 0, 1, 32, len(image_data), 22
    )
    return ico_hdr + dir_entry + image_data


_FAVICON_ICO: bytes = _build_favicon()

logger = logging.getLogger(__name__)

# ── HTML 模板 ──────────────────────────────────────────────────────────────

_DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>Coding Proxy Dashboard</title>
  <link rel="icon" type="image/x-icon" href="/favicon.ico" />
  <link rel="preconnect" href="https://fonts.googleapis.com" />
  <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin />
  <link href="https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@400;600&display=swap" rel="stylesheet" />
  <script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.3/dist/chart.umd.min.js"></script>
  <style>
    :root {
      --bg: #0d1117;
      --bg-card: #161b22;
      --bg-card-hover: #1c2128;
      --border: #30363d;
      --border-subtle: rgba(48,54,61,.6);
      --text-primary: #e6edf3;
      --text-secondary: #8b949e;
      --text-tertiary: #6e7681;
      --accent-blue: #58a6ff;
      --accent-green: #3fb950;
      --accent-yellow: #d29922;
      --accent-red: #f85149;
      --accent-purple: #bc8cff;
      --accent-orange: #ffa657;
      --accent-teal: #39d353;
      --radius: 10px;
      --radius-sm: 6px;
      --shadow: 0 1px 3px rgba(0,0,0,.4), 0 1px 2px rgba(0,0,0,.3);
      --shadow-md: 0 4px 12px rgba(0,0,0,.4), 0 2px 4px rgba(0,0,0,.3);
      --glow-blue: 0 0 0 1px rgba(88,166,255,.15), 0 4px 16px rgba(88,166,255,.06);
    }
    @keyframes fadeInUp {
      from { opacity: 0; transform: translateY(10px); }
      to   { opacity: 1; }
    }
    *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
    body {
      background: var(--bg);
      color: var(--text-primary);
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", "Helvetica Neue", Arial, sans-serif;
      font-size: 14px;
      line-height: 1.5;
      min-height: 100vh;
      -webkit-font-smoothing: antialiased;
      -moz-osx-font-smoothing: grayscale;
      text-rendering: optimizeLegibility;
    }
    /* ── 头部 ── */
    header {
      background: rgba(22,27,34,.85);
      backdrop-filter: blur(12px);
      -webkit-backdrop-filter: blur(12px);
      border-bottom: 1px solid var(--border);
      padding: 13px 24px;
      display: flex;
      align-items: center;
      justify-content: space-between;
      position: sticky;
      top: 0;
      z-index: 100;
    }
    .header-left { display: flex; align-items: center; gap: 12px; }
    .logo {
      width: 30px; height: 30px;
      background: linear-gradient(135deg, var(--accent-blue), var(--accent-purple));
      border-radius: 50%;
      display: flex; align-items: center; justify-content: center;
      font-size: 15px; font-weight: 700; color: #fff;
      box-shadow: 0 2px 8px rgba(88,166,255,.3);
    }
    h1 { font-size: 15px; font-weight: 600; color: var(--text-primary); letter-spacing: -.2px; }
    .header-right { display: flex; align-items: center; gap: 12px; }
    .badge {
      font-size: 11px; padding: 2px 8px;
      border-radius: 12px;
      background: rgba(88,166,255,.1);
      color: var(--accent-blue);
      border: 1px solid rgba(88,166,255,.2);
      font-family: 'JetBrains Mono', monospace;
    }
    .refresh-time { font-size: 11px; color: var(--text-tertiary); }
    .btn-refresh {
      padding: 5px 12px; border-radius: var(--radius-sm);
      background: rgba(48,54,61,.5);
      border: 1px solid var(--border);
      color: var(--text-secondary);
      font-size: 12px; cursor: pointer;
      transition: all .2s ease;
    }
    .btn-refresh:hover {
      background: var(--bg-card-hover);
      color: var(--text-primary);
      border-color: rgba(88,166,255,.4);
    }
    /* ── 主内容 ── */
    main { padding: 20px 24px; max-width: 1440px; margin: 0 auto; }
    /* ── KPI 卡片 ── */
    .kpi-grid {
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
      gap: 12px;
      margin-bottom: 18px;
    }
    .kpi-card {
      background: var(--bg-card);
      border: 1px solid var(--border);
      border-radius: var(--radius);
      padding: 16px 18px 14px;
      box-shadow: var(--shadow);
      transition: all .2s ease;
      animation: fadeInUp .4s ease both;
      position: relative;
      overflow: hidden;
    }
    .kpi-card::before {
      content: '';
      position: absolute;
      top: 0; left: 0; right: 0;
      height: 2px;
      border-radius: var(--radius) var(--radius) 0 0;
    }
    .kpi-card:nth-child(1)::before { background: var(--accent-blue); }
    .kpi-card:nth-child(2)::before { background: var(--accent-purple); }
    .kpi-card:nth-child(3)::before { background: var(--accent-green); }
    .kpi-card:nth-child(4)::before { background: var(--accent-yellow); }
    .kpi-card:nth-child(5)::before { background: var(--accent-red); }
    .kpi-card:nth-child(6)::before { background: var(--accent-orange); }
    .kpi-card:hover {
      background: var(--bg-card-hover);
      box-shadow: var(--glow-blue);
      transform: translateY(-1px);
    }
    .kpi-header { display: flex; align-items: center; gap: 6px; margin-bottom: 8px; }
    .kpi-icon { font-size: 13px; opacity: .8; }
    .kpi-label { font-size: 11px; color: var(--text-secondary); font-weight: 500; letter-spacing: .2px; }
    .kpi-value {
      font-size: 24px; font-weight: 700; line-height: 1.2;
      font-family: 'JetBrains Mono', monospace;
      letter-spacing: -0.5px;
    }
    .kpi-sub { font-size: 11px; color: var(--text-tertiary); margin-top: 5px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; max-width: 100%; }
    .color-blue { color: var(--accent-blue); }
    .color-green { color: var(--accent-green); }
    .color-yellow { color: var(--accent-yellow); }
    .color-red { color: var(--accent-red); }
    .color-purple { color: var(--accent-purple); }
    .color-orange { color: var(--accent-orange); }
    /* ── 图表网格 ── */
    .charts-grid {
      display: grid;
      grid-template-columns: 1fr 2fr;
      gap: 12px;
      margin-bottom: 12px;
    }
    .charts-grid-2 {
      display: grid;
      grid-template-columns: 1fr 2fr;
      gap: 12px;
      margin-bottom: 12px;
    }
    @media (max-width: 960px) {
      .charts-grid, .charts-grid-2 { grid-template-columns: 1fr; }
    }
    .card {
      background: var(--bg-card);
      border: 1px solid var(--border);
      border-radius: var(--radius);
      padding: 16px 20px;
      box-shadow: var(--shadow);
      transition: box-shadow .2s ease;
      animation: fadeInUp .4s ease both;
    }
    .card:hover { box-shadow: var(--shadow-md); }
    .card-title {
      font-size: 11px; font-weight: 600;
      color: var(--text-tertiary);
      text-transform: uppercase;
      letter-spacing: .8px;
      margin-bottom: 14px;
      display: flex; align-items: center; justify-content: space-between;
    }
    .chart-wrap { position: relative; height: 220px; }
    .chart-wrap-lg { position: relative; height: 240px; }
    .chart-wrap-xl { position: relative; height: 260px; }
    /* ── 供应商状态 ── */
    .vendor-list { display: flex; flex-direction: column; gap: 8px; }
    .vendor-item {
      display: flex; align-items: center; justify-content: space-between;
      padding: 10px 12px;
      background: rgba(255,255,255,.02);
      border: 1px solid var(--border-subtle);
      border-radius: var(--radius-sm);
      transition: background .15s;
    }
    .vendor-item:hover { background: rgba(255,255,255,.04); }
    .vendor-info { display: flex; align-items: center; gap: 10px; }
    .vendor-avatar {
      width: 28px; height: 28px; border-radius: 50%;
      background: linear-gradient(135deg, var(--accent-blue), var(--accent-purple));
      display: flex; align-items: center; justify-content: center;
      font-size: 11px; font-weight: 700; color: #fff;
      flex-shrink: 0;
    }
    .vendor-name { font-weight: 600; font-size: 12px; }
    .vendor-badges { display: flex; gap: 5px; flex-wrap: wrap; align-items: center; }
    .status-badge {
      font-size: 10px; padding: 2px 7px;
      border-radius: 10px;
      font-weight: 500;
    }
    .sb-ok { background: rgba(63,185,80,.12); color: var(--accent-green); border: 1px solid rgba(63,185,80,.2); }
    .sb-warn { background: rgba(210,153,34,.12); color: var(--accent-yellow); border: 1px solid rgba(210,153,34,.2); }
    .sb-err { background: rgba(248,81,73,.12); color: var(--accent-red); border: 1px solid rgba(248,81,73,.2); }
    .sb-info { background: rgba(88,166,255,.12); color: var(--accent-blue); border: 1px solid rgba(88,166,255,.2); }
    .quota-bar-wrap { flex: 1; margin: 0 10px; max-width: 100px; }
    .quota-bar-bg {
      height: 4px; border-radius: 2px;
      background: rgba(255,255,255,.06);
      overflow: hidden;
    }
    .quota-bar-fill {
      height: 100%; border-radius: 2px;
      transition: width .6s cubic-bezier(.4,0,.2,1);
    }
    /* ── 故障转移表 ── */
    .ft-table-wrap { overflow-x: auto; }
    table { width: 100%; border-collapse: collapse; }
    thead tr { position: sticky; top: 0; background: var(--bg-card); z-index: 1; }
    th {
      text-align: left; font-size: 11px; color: var(--text-tertiary);
      font-weight: 600; padding: 6px 10px;
      border-bottom: 1px solid var(--border);
      letter-spacing: .4px; text-transform: uppercase;
    }
    td { padding: 8px 10px; font-size: 13px; border-bottom: 1px solid var(--border-subtle); }
    tr:last-child td { border-bottom: none; }
    tr:hover td { background: rgba(255,255,255,.02); }
    .tag-vendor {
      display: inline-block;
      font-size: 11px; padding: 2px 8px;
      border-radius: 10px;
      background: rgba(188,140,255,.1);
      color: var(--accent-purple);
      border: 1px solid rgba(188,140,255,.2);
      font-weight: 500;
    }
    .arrow { color: var(--text-tertiary); margin: 0 4px; }
    /* ── 时间区间选择栏 ── */
    .time-range-bar {
      display: flex; align-items: center; gap: 8px;
      margin-bottom: 18px; flex-wrap: wrap;
      padding: 10px 14px;
      background: rgba(22,27,34,.6);
      border: 1px solid var(--border-subtle);
      border-radius: var(--radius);
      backdrop-filter: blur(8px);
    }
    .time-range-label { font-size: 12px; color: var(--text-tertiary); font-weight: 500; }
    .range-btn {
      padding: 4px 14px; border-radius: 14px;
      background: transparent;
      border: 1px solid transparent;
      color: var(--text-secondary);
      font-size: 12px; cursor: pointer;
      transition: all .2s ease;
    }
    .range-btn:hover { background: rgba(255,255,255,.05); color: var(--text-primary); }
    .range-btn.active {
      background: rgba(88,166,255,.12);
      border-color: rgba(88,166,255,.35);
      color: var(--accent-blue);
      font-weight: 500;
    }
    .range-custom { display: none; align-items: center; gap: 6px; }
    .range-custom.visible { display: flex; }
    .range-date {
      padding: 3px 10px; border-radius: var(--radius-sm);
      background: rgba(48,54,61,.4); border: 1px solid var(--border);
      color: var(--text-primary); font-size: 12px;
      color-scheme: dark;
      transition: border-color .2s;
    }
    .range-date:focus { outline: none; border-color: rgba(88,166,255,.5); }
    .range-sep { font-size: 12px; color: var(--text-tertiary); }
    /* ── 空态 ── */
    .empty {
      text-align: center; padding: 32px;
      color: var(--text-tertiary); font-size: 13px;
    }
    .empty-icon { font-size: 28px; margin-bottom: 8px; opacity: .5; }
    /* ── 加载态 ── */
    .loading { opacity: .4; pointer-events: none; }
    /* ── 图表标签截断 ── */
    .chart-legend-note { font-size: 11px; color: var(--text-tertiary); margin-top: 4px; text-align: center; }
    /* ── 外部 Tooltip ── */
    #chart-tooltip {
      position: fixed;
      pointer-events: none;
      background: rgba(13,17,23,.95);
      border: 1px solid var(--border);
      border-radius: var(--radius-sm);
      padding: 10px 14px;
      font-size: 12px;
      color: var(--text-primary);
      box-shadow: var(--shadow-md);
      z-index: 1000;
      opacity: 0;
      transition: opacity .15s ease;
      max-width: 360px;
      max-height: 60vh;
      overflow-y: auto;
      backdrop-filter: blur(8px);
      -webkit-backdrop-filter: blur(8px);
    }
    #chart-tooltip.active { opacity: 1; }
    #chart-tooltip-title {
      font-weight: 600; margin-bottom: 6px; padding-bottom: 6px;
      border-bottom: 1px solid var(--border-subtle); color: var(--text-secondary); font-size: 11px;
    }
    #chart-tooltip-items { display: flex; flex-direction: column; gap: 3px; }
    .tt-item { display: flex; align-items: center; gap: 8px; line-height: 1.4; }
    .tt-color { width: 8px; height: 8px; border-radius: 50%; flex-shrink: 0; }
    .tt-label { flex: 1; color: var(--text-primary); }
    .tt-value { font-family: 'JetBrains Mono', monospace; font-size: 11px; color: var(--text-secondary); }
    #chart-tooltip-footer {
      margin-top: 6px; padding-top: 6px; border-top: 1px solid var(--border-subtle);
      font-weight: 500; font-size: 11px; color: var(--text-secondary);
    }
  </style>
</head>
<body>
<header>
  <div class="header-left">
    <div class="logo">C</div>
    <h1>Coding Proxy Dashboard</h1>
    <span class="badge" id="version-badge">v-.-.-</span>
  </div>
  <div class="header-right">
    <span class="refresh-time" id="refresh-time">正在加载…</span>
    <button class="btn-refresh" onclick="refresh()">⟳ 刷新</button>
  </div>
</header>

<main>
  <!-- 时间区间选择器 -->
  <div class="time-range-bar">
    <span class="time-range-label">时间区间</span>
    <button class="range-btn active" onclick="setTimeRange(7, this)">近 7 天</button>
    <button class="range-btn" onclick="setTimeRange(30, this)">近 30 天</button>
    <button class="range-btn" onclick="setTimeRange(0, this)">自选区间</button>
    <div class="range-custom" id="range-custom">
      <input type="date" id="range-start" class="range-date" onchange="applyCustomRange()" />
      <span class="range-sep">→</span>
      <input type="date" id="range-end" class="range-date" onchange="applyCustomRange()" />
    </div>
  </div>

  <!-- KPI 卡片 -->
  <div class="kpi-grid" id="kpi-grid">
    <div class="kpi-card">
      <div class="kpi-header"><span class="kpi-icon">📊</span><span class="kpi-label">今日请求数</span></div>
      <div class="kpi-value color-blue" id="kpi-req-today">–</div>
      <div class="kpi-sub" id="kpi-req-week">本周 –</div>
    </div>
    <div class="kpi-card">
      <div class="kpi-header"><span class="kpi-icon">🔢</span><span class="kpi-label">今日 Token 总量</span></div>
      <div class="kpi-value color-purple" id="kpi-tok-today">–</div>
      <div class="kpi-sub" id="kpi-tok-week">本周 –</div>
    </div>
    <div class="kpi-card">
      <div class="kpi-header"><span class="kpi-icon">💬</span><span class="kpi-label">今日输出 Token</span></div>
      <div class="kpi-value color-green" id="kpi-out-today">–</div>
      <div class="kpi-sub" id="kpi-out-week">本周 –</div>
    </div>
    <div class="kpi-card">
      <div class="kpi-header"><span class="kpi-icon">💰</span><span class="kpi-label">今日费用估算</span></div>
      <div class="kpi-value color-yellow" id="kpi-cost-today">–</div>
      <div class="kpi-sub" id="kpi-cost-week">本周 –</div>
    </div>
    <div class="kpi-card">
      <div class="kpi-header"><span class="kpi-icon">🔄</span><span class="kpi-label">故障转移（今日）</span></div>
      <div class="kpi-value color-red" id="kpi-fo-today">–</div>
      <div class="kpi-sub" id="kpi-fo-week">本周 –</div>
    </div>
    <div class="kpi-card">
      <div class="kpi-header"><span class="kpi-icon">⚡</span><span class="kpi-label">平均延迟（今日）</span></div>
      <div class="kpi-value color-orange" id="kpi-lat-today">–</div>
      <div class="kpi-sub" id="kpi-lat-week">本周 –</div>
    </div>
  </div>

  <!-- 供应商状态 + 请求量趋势折线图 -->
  <div class="charts-grid">
    <div class="card">
      <div class="card-title">供应商状态</div>
      <div class="vendor-list" id="vendor-list">
        <div class="empty">加载中…</div>
      </div>
    </div>
    <div class="card">
      <div class="card-title" id="title-timeline">近 7 天请求量趋势</div>
      <div class="chart-wrap-lg">
        <canvas id="chart-timeline"></canvas>
      </div>
    </div>
  </div>

  <!-- 供应商分布 + Token 量趋势（按 vendor） -->
  <div class="charts-grid-2">
    <div class="card">
      <div class="card-title" id="title-vendor-dist">供应商请求分布（近 7 天）</div>
      <div class="chart-wrap">
        <canvas id="chart-vendor-dist"></canvas>
      </div>
    </div>
    <div class="card">
      <div class="card-title" id="title-token-timeline">近 7 天 Token 量趋势（按供应商）</div>
      <div class="chart-wrap-lg">
        <canvas id="chart-token-timeline"></canvas>
      </div>
    </div>
  </div>

  <!-- Token 用量（按 Vendor / 模型）堆叠图 -->
  <div class="card" style="margin-bottom:12px">
    <div class="card-title" id="title-model-token-timeline">近 7 天 Token 用量（按 Vendor / 模型）</div>
    <div class="chart-wrap-xl">
      <canvas id="chart-model-token-timeline"></canvas>
    </div>
  </div>

</main>

<div id="chart-tooltip"></div>

<script>
// ── 颜色配置 ──────────────────────────────────────────────
// 调色盘参考 Tailwind CSS 400-level，深色背景高区分度最佳实践
const VENDOR_COLORS = [
  '#60A5FA',  // blue-400
  '#FB923C',  // orange-400
  '#34D399',  // emerald-400
  '#A78BFA',  // violet-400
  '#F87171',  // red-400
  '#38BDF8',  // sky-400
  '#FBBF24',  // amber-400
  '#F472B6',  // pink-400
  '#4ADE80',  // green-400
  '#E879F9',  // fuchsia-400
  '#818CF8',  // indigo-400
  '#2DD4BF',  // teal-400
  '#FB7185',  // rose-400
  '#FCD34D',  // yellow-300
  '#6EE7B7',  // emerald-300
  '#C4B5FD',  // violet-300
  '#7DD3FC',  // sky-300
  '#FED7AA',  // orange-200
  '#FECDD3',  // rose-200
  '#BBF7D0',  // green-200
];

// ── 工具函数 ──────────────────────────────────────────────
function fmtTokens(n) {
  if (!n) return '0';
  if (n >= 1e9) return (n/1e9).toFixed(2).replace(/\\.?0+$/,'') + 'B';
  if (n >= 1e6) return (n/1e6).toFixed(2).replace(/\\.?0+$/,'') + 'M';
  if (n >= 1e3) return (n/1e3).toFixed(1).replace(/\\.?0+$/,'') + 'K';
  return String(n);
}
function fmtNum(n) { return n == null ? '–' : n.toLocaleString(); }
function isValidLabel(s) { return typeof s === 'string' && s !== 'undefined' && s !== 'null' && s.trim() !== ''; }
function now() {
  return new Date().toLocaleTimeString('zh-CN', {hour:'2-digit',minute:'2-digit',second:'2-digit'});
}

// ── 渐变填充工具 ──────────────────────────────────────────
function makeGradient(ctx, color) {
  const h = ctx.canvas.height;
  const grad = ctx.createLinearGradient(0, 0, 0, h);
  grad.addColorStop(0, color + '44');
  grad.addColorStop(1, color + '04');
  return grad;
}

// ── Chart.js 全局默认 ─────────────────────────────────────
Chart.defaults.color = '#8b949e';
Chart.defaults.borderColor = 'rgba(255,255,255,.04)';
Chart.defaults.font.family = '-apple-system, BlinkMacSystemFont, "Segoe UI", "PingFang SC", "Microsoft YaHei", sans-serif';
Chart.defaults.font.size = 11;
Chart.defaults.plugins.tooltip.usePointStyle = true;
Chart.defaults.devicePixelRatio = window.devicePixelRatio || 1;

const COMMON_SCALE_X = { grid: { display: false }, ticks: { maxTicksLimit: 10 } };
const COMMON_SCALE_Y = { grid: { color: 'rgba(255,255,255,.04)' }, beginAtZero: true };
const COMMON_LEGEND = {
  position: 'bottom',
  labels: {
    boxWidth: 10,
    boxHeight: 10,
    padding: 14,
    usePointStyle: true,
    pointStyle: 'circle',
    font: { size: 11 },
    generateLabels: chart => {
      const items = Chart.defaults.plugins.legend.labels.generateLabels(chart);
      return items.filter(item => isValidLabel(item.text)).map(item => {
        item.pointStyle = 'circle';
        item.lineWidth = 0;
        item.fillStyle = item.strokeStyle;
        return item;
      });
    },
  },
};
const COMMON_LINE_DATASET = { tension: .35, pointRadius: 0, pointHoverRadius: 5, borderWidth: 2 };

// ── 外部 Tooltip（数据项较多时可溢出卡片边界）─────────────
function createExternalTooltipHandler(context) {
  const { chart, tooltip } = context;
  const el = document.getElementById('chart-tooltip');
  if (!el) return;

  if (!tooltip.opacity) {
    el.classList.remove('active');
    return;
  }

  // 构建 HTML
  const titleLines = tooltip.title || [];
  const dataPoints = tooltip.dataPoints || [];
  const footerLines = tooltip.footer || [];

  let html = '';
  if (titleLines.length) {
    html += '<div id="chart-tooltip-title">' + titleLines.join('<br>') + '</div>';
  }
  if (dataPoints.length) {
    html += '<div id="chart-tooltip-items">';
    dataPoints.forEach(dp => {
      const color = dp.dataset.borderColor || dp.backgroundColor || '#8b949e';
      const label = dp.dataset.label || '';
      const value = fmtTokens(dp.raw);
      html += '<div class="tt-item">' +
        '<span class="tt-color" style="background:' + color + '"></span>' +
        '<span class="tt-label">' + label + '</span>' +
        '<span class="tt-value">' + value + '</span>' +
        '</div>';
    });
    html += '</div>';
  }
  if (footerLines.length) {
    html += '<div id="chart-tooltip-footer">' + footerLines.join('<br>') + '</div>';
  }
  el.innerHTML = html;

  // 定位（fixed，基于 canvas 视口坐标）
  const canvasRect = chart.canvas.getBoundingClientRect();
  const elW = el.offsetWidth || 200;
  const elH = el.offsetHeight || 100;
  const caretX = tooltip.caretX || 0;
  const caretY = tooltip.caretY || 0;

  let left = canvasRect.left + caretX + 14;
  let top = canvasRect.top + caretY - 14;

  // 边界修正
  if (left + elW > window.innerWidth - 10) {
    left = canvasRect.left + caretX - elW - 14;
  }
  if (top + elH > window.innerHeight - 10) {
    top = window.innerHeight - elH - 10;
  }
  if (top < 10) {
    top = 10;
  }

  el.style.left = left + 'px';
  el.style.top = top + 'px';
  el.classList.add('active');
}

const EXTERNAL_TOOLTIP = { enabled: false, external: createExternalTooltipHandler };

// ── Legend 点击交互：单击=仅选该项，Ctrl/Meta+单击=多选追加，Shift+单击=排除 ──
function legendOnClick(e, legendItem, legend) {
  const chart = legend.chart;
  const isShift = e.native.shiftKey;
  const isCtrl  = e.native.ctrlKey || e.native.metaKey;
  if (chart.config.type === 'doughnut' || chart.config.type === 'pie') {
    const idx = legendItem.index;
    const dataLen = chart.data.labels.length;
    if (isShift) {
      chart.toggleDataVisibility(idx);
    } else if (isCtrl) {
      if (!chart.getDataVisibility(idx)) chart.toggleDataVisibility(idx);
    } else {
      const allOthersHidden = [...Array(dataLen).keys()].filter(i => i !== idx).every(i => !chart.getDataVisibility(i));
      if (allOthersHidden) {
        for (let i = 0; i < dataLen; i++) { if (!chart.getDataVisibility(i)) chart.toggleDataVisibility(i); }
      } else {
        for (let i = 0; i < dataLen; i++) {
          const vis = chart.getDataVisibility(i);
          if (i === idx && !vis) chart.toggleDataVisibility(i);
          if (i !== idx && vis)  chart.toggleDataVisibility(i);
        }
      }
    }
  } else {
    const idx = legendItem.datasetIndex;
    const datasets = chart.data.datasets;
    if (isShift) {
      const meta = chart.getDatasetMeta(idx);
      meta.hidden = !meta.hidden;
    } else if (isCtrl) {
      chart.getDatasetMeta(idx).hidden = false;
    } else {
      const allOthersHidden = datasets.every((_, i) => i === idx || !!chart.getDatasetMeta(i).hidden);
      if (allOthersHidden) {
        datasets.forEach((_, i) => { chart.getDatasetMeta(i).hidden = false; });
      } else {
        datasets.forEach((_, i) => { chart.getDatasetMeta(i).hidden = (i !== idx); });
      }
    }
  }
  chart.update();
}

// ── 图表实例 ──────────────────────────────────────────────
let chartTimeline = null;
let chartVendorDist = null;
let chartTokenTimeline = null;
let chartModelTokenTimeline = null;

function destroyCharts() {
  [chartTimeline, chartVendorDist, chartTokenTimeline, chartModelTokenTimeline]
    .forEach(c => c && c.destroy());
  chartTimeline = chartVendorDist = chartTokenTimeline = chartModelTokenTimeline = null;
  const tip = document.getElementById('chart-tooltip');
  if (tip) tip.classList.remove('active');
}

// ── 数据拉取 ──────────────────────────────────────────────
async function fetchJSON(url) {
  const r = await fetch(url);
  if (!r.ok) throw new Error(r.status);
  return r.json();
}

// ── KPI 更新 ──────────────────────────────────────────────
function updateKPI(summary) {
  const t = summary.today, r = summary.range;
  const lbl = currentRangeLabel;

  document.getElementById('kpi-req-today').textContent = fmtNum(t.requests);
  document.getElementById('kpi-req-week').textContent = lbl + ' ' + fmtNum(r.requests);

  const tokT = t.tokens, tokR = r.tokens;
  const totalT = tokT.input + tokT.output + tokT.cache_creation + tokT.cache_read;
  const totalR = tokR.input + tokR.output + tokR.cache_creation + tokR.cache_read;
  document.getElementById('kpi-tok-today').textContent = fmtTokens(totalT);
  document.getElementById('kpi-tok-week').textContent = lbl + ' ' + fmtTokens(totalR);

  document.getElementById('kpi-out-today').textContent = fmtTokens(tokT.output);
  document.getElementById('kpi-out-week').textContent = lbl + ' ' + fmtTokens(tokR.output);

  document.getElementById('kpi-cost-today').textContent = t.cost || '–';
  document.getElementById('kpi-cost-week').textContent = lbl + ' ' + (r.cost || '–');

  document.getElementById('kpi-fo-today').textContent = fmtNum(t.failovers);
  document.getElementById('kpi-fo-week').textContent = lbl + ' ' + fmtNum(r.failovers);

  document.getElementById('kpi-lat-today').textContent = t.avg_duration_ms ? t.avg_duration_ms + 'ms' : '–';
  document.getElementById('kpi-lat-week').textContent = lbl + ' ' + (r.avg_duration_ms ? r.avg_duration_ms + 'ms' : '–');
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
function quotaWindowLabel(wh) {
  if (!wh) return '配额';
  const h = parseFloat(wh);
  if (h >= 24) return Math.round(h / 24) + 'd配额';
  return Math.round(h) + 'h配额';
}
function renderQuotaBar(qg) {
  if (!qg || qg.usage_percent == null) return '';
  const pct = Math.round(qg.usage_percent);
  const label = quotaWindowLabel(qg.window_hours);
  return `<span class="status-badge ${quotaClass(pct)}">${label} ${pct}%</span>` +
    `<div class="quota-bar-wrap"><div class="quota-bar-bg">` +
    `<div class="quota-bar-fill" style="width:${Math.min(pct,100)}%;background:${quotaBarColor(pct)}"></div>` +
    `</div></div>`;
}

function updateVendorStatus(status) {
  const tiers = status.tiers || [];
  const list = document.getElementById('vendor-list');
  if (!tiers.length) {
    list.innerHTML = '<div class="empty"><div class="empty-icon">🔌</div>无供应商数据</div>';
    return;
  }
  list.innerHTML = tiers.map(tier => {
    const cb = tier.circuit_breaker || {};
    const cbClass = cbStateClass(cb.state);
    const cbLabel = cbStateLabel(cb.state);
    const initial = (tier.name || '?').charAt(0).toUpperCase();

    let quotaHTML = '';
    if (tier.quota_guard) quotaHTML += renderQuotaBar(tier.quota_guard);
    if (tier.weekly_quota_guard) quotaHTML += renderQuotaBar(tier.weekly_quota_guard);

    const rlInfo = tier.rate_limit || {};
    const rlHtml = rlInfo.limited ? `<span class="status-badge sb-warn">限速中</span>` : '';

    return `<div class="vendor-item">
      <div class="vendor-info">
        <div class="vendor-avatar">${initial}</div>
        <span class="vendor-name">${tier.name}</span>
      </div>
      <div class="vendor-badges">
        <span class="status-badge ${cbClass}">${cbLabel}${cb.failure_count ? ' ×'+cb.failure_count : ''}</span>
        ${quotaHTML}
        ${rlHtml}
      </div>
    </div>`;
  }).join('');
}

// ── 时序折线图（请求量，按 vendor）────────────────────────
function buildTimeline(rows) {
  const vendorDateMap = {};
  const allDates = new Set();
  for (const r of rows) {
    const v = r.vendor, d = r.date;
    if (!isValidLabel(v) || !d) continue;
    if (!vendorDateMap[v]) vendorDateMap[v] = {};
    vendorDateMap[v][d] = (vendorDateMap[v][d] || 0) + (r.total_requests || 0);
    allDates.add(d);
  }
  const dates = [...allDates].sort();
  const vendors = Object.keys(vendorDateMap).sort();

  if (chartTimeline) chartTimeline.destroy();
  const ctx = document.getElementById('chart-timeline').getContext('2d');
  const datasets = vendors.map((v, i) => {
    const color = VENDOR_COLORS[i % VENDOR_COLORS.length];
    return {
      ...COMMON_LINE_DATASET,
      label: v,
      data: dates.map(d => vendorDateMap[v][d] || 0),
      borderColor: color,
      backgroundColor: color + '30',
      fill: true,
    };
  });

  chartTimeline = new Chart(ctx, {
    type: 'line',
    data: { labels: dates, datasets },
    options: {
      responsive: true, maintainAspectRatio: false,
      interaction: { mode: 'index', intersect: false },
      plugins: {
        legend: { ...COMMON_LEGEND, onClick: legendOnClick },
        tooltip: {
          itemSort: (a, b) => (b.raw || 0) - (a.raw || 0),
          callbacks: {
            label: c => ` ${c.dataset.label}: ${fmtNum(c.raw)}`,
            footer: items => {
              const total = items.reduce((s, i) => s + (i.raw || 0), 0);
              return total > 0 ? '合计: ' + fmtNum(total) : '';
            },
          },
        },
      },
      scales: {
        x: COMMON_SCALE_X,
        y: { ...COMMON_SCALE_Y, stacked: true, ticks: { precision: 0 } },
      },
    },
  });
}

// ── 供应商分布环形图 ──────────────────────────────────────
function buildVendorDist(rows) {
  const vendorTotals = {};
  for (const r of rows) {
    const v = r.vendor;
    if (!isValidLabel(v)) continue;
    vendorTotals[v] = (vendorTotals[v] || 0) + (r.total_requests || 0);
  }
  const labels = Object.keys(vendorTotals).sort((a,b) => vendorTotals[b]-vendorTotals[a]);
  const data = labels.map(v => vendorTotals[v]);

  if (chartVendorDist) chartVendorDist.destroy();
  const ctx = document.getElementById('chart-vendor-dist').getContext('2d');
  if (!labels.length) {
    ctx.canvas.parentElement.innerHTML = '<div class="empty"><div class="empty-icon">📭</div>暂无数据</div>';
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
        hoverOffset: 8,
      }],
    },
    options: {
      responsive: true, maintainAspectRatio: false,
      plugins: {
        legend: { ...COMMON_LEGEND, onClick: legendOnClick },
        tooltip: { callbacks: { label: c => ` ${c.label}: ${c.raw.toLocaleString()} 次` } },
      },
    },
  });
}

// ── Token 量趋势折线图（按 vendor）───────────────────────
function buildTokenTimeline(rows) {
  const vendorDateMap = {};
  const allDates = new Set();
  for (const r of rows) {
    const v = r.vendor, d = r.date;
    if (!isValidLabel(v) || !d) continue;
    if (!vendorDateMap[v]) vendorDateMap[v] = {};
    const total = (r.total_input || 0) + (r.total_output || 0)
                + (r.total_cache_creation || 0) + (r.total_cache_read || 0);
    vendorDateMap[v][d] = (vendorDateMap[v][d] || 0) + total;
    allDates.add(d);
  }
  const dates = [...allDates].sort();
  const vendors = Object.keys(vendorDateMap).sort();

  if (chartTokenTimeline) chartTokenTimeline.destroy();
  const ctx = document.getElementById('chart-token-timeline').getContext('2d');
  if (!dates.length) {
    ctx.canvas.parentElement.innerHTML = '<div class="empty"><div class="empty-icon">📭</div>暂无数据</div>';
    return;
  }

  const datasets = vendors.map((v, i) => {
    const color = VENDOR_COLORS[i % VENDOR_COLORS.length];
    return {
      ...COMMON_LINE_DATASET,
      label: v,
      data: dates.map(d => vendorDateMap[v][d] || 0),
      borderColor: color,
      backgroundColor: color + '30',
      fill: true,
    };
  });

  chartTokenTimeline = new Chart(ctx, {
    type: 'line',
    data: { labels: dates, datasets },
    options: {
      responsive: true, maintainAspectRatio: false,
      interaction: { mode: 'index', intersect: false },
      plugins: {
        legend: { ...COMMON_LEGEND, onClick: legendOnClick },
        tooltip: {
          itemSort: (a, b) => (b.raw || 0) - (a.raw || 0),
          callbacks: {
            label: c => ` ${c.dataset.label}: ${fmtTokens(c.raw)}`,
            footer: items => {
              const total = items.reduce((s, i) => s + (i.raw || 0), 0);
              return total > 0 ? '合计: ' + fmtTokens(total) : '';
            },
          },
        },
      },
      scales: {
        x: COMMON_SCALE_X,
        y: { ...COMMON_SCALE_Y, stacked: true, ticks: { callback: v => fmtTokens(v) } },
      },
    },
  });
}

// ── Token 用量趋势（按 Vendor / 模型，堆叠面积图）────────
function buildModelTokenTimeline(rows) {
  const modelDateMap = {};
  const allDates = new Set();
  for (const r of rows) {
    const v = r.vendor;
    const m = r.model_served;
    if (!isValidLabel(v) || !isValidLabel(m)) continue;
    const key = v + ' / ' + m;
    const d = r.date;
    if (!d) continue;
    if (!modelDateMap[key]) modelDateMap[key] = {};
    const total = (r.total_input || 0) + (r.total_output || 0)
                + (r.total_cache_creation || 0) + (r.total_cache_read || 0);
    modelDateMap[key][d] = (modelDateMap[key][d] || 0) + total;
    allDates.add(d);
  }
  const dates = [...allDates].sort();
  // 按总量降序排列 key
  const keys = Object.keys(modelDateMap).sort((a, b) => {
    const sumA = Object.values(modelDateMap[a]).reduce((s, v) => s + v, 0);
    const sumB = Object.values(modelDateMap[b]).reduce((s, v) => s + v, 0);
    return sumB - sumA;
  });

  if (chartModelTokenTimeline) chartModelTokenTimeline.destroy();
  const canvasEl = document.getElementById('chart-model-token-timeline');
  if (!canvasEl) return;
  const ctx = canvasEl.getContext('2d');
  if (!dates.length || !keys.length) {
    ctx.canvas.parentElement.innerHTML = '<div class="empty"><div class="empty-icon">📭</div>暂无数据</div>';
    return;
  }

  const datasets = keys.map((key, i) => {
    const color = VENDOR_COLORS[i % VENDOR_COLORS.length];
    return {
      ...COMMON_LINE_DATASET,
      label: key,
      data: dates.map(d => modelDateMap[key][d] || 0),
      borderColor: color,
      backgroundColor: color + '30',
      fill: true,
    };
  });

  chartModelTokenTimeline = new Chart(ctx, {
    type: 'line',
    data: { labels: dates, datasets },
    options: {
      responsive: true, maintainAspectRatio: false,
      interaction: { mode: 'index', intersect: false },
      plugins: {
        legend: {
          position: keys.length > 8 ? 'right' : 'bottom',
          onClick: legendOnClick,
          labels: {
            ...COMMON_LEGEND.labels,
            generateLabels: chart => {
              const items = COMMON_LEGEND.labels.generateLabels(chart);
              const maxLen = 32;
              items.forEach(item => {
                if (item.text.length > maxLen) item.text = item.text.slice(0, maxLen) + '…';
                item.pointStyle = 'circle';
                item.lineWidth = 0;
                item.fillStyle = item.strokeStyle;
              });
              return items;
            },
          },
        },
        tooltip: {
          ...EXTERNAL_TOOLTIP,
          itemSort: (a, b) => (b.raw || 0) - (a.raw || 0),
          callbacks: {
            label: c => ' ' + c.dataset.label + ': ' + fmtTokens(c.raw),
            footer: items => {
              const total = items.reduce((s, i) => s + (i.raw || 0), 0);
              return total > 0 ? '合计: ' + fmtTokens(total) : '';
            },
          },
        },
      },
      scales: {
        x: COMMON_SCALE_X,
        y: {
          ...COMMON_SCALE_Y,
          stacked: true,
          ticks: { callback: v => fmtTokens(v) },
        },
      },
    },
  });
}

// ── 时间区间控制 ──────────────────────────────────────────
let currentDays = 7;
let currentRangeLabel = '本周';

function setTimeRange(days, btn) {
  currentDays = days;
  if (days === 7) currentRangeLabel = '本周';
  else if (days === 30) currentRangeLabel = '本月';
  document.querySelectorAll('.range-btn').forEach(b => b.classList.remove('active'));
  if (btn) btn.classList.add('active');
  const customEl = document.getElementById('range-custom');
  if (days === 0) {
    customEl.classList.add('visible');
    const today = new Date();
    const weekAgo = new Date(today);
    weekAgo.setDate(weekAgo.getDate() - 6);
    document.getElementById('range-end').value = today.toISOString().slice(0, 10);
    document.getElementById('range-start').value = weekAgo.toISOString().slice(0, 10);
    applyCustomRange();
  } else {
    customEl.classList.remove('visible');
    refresh();
  }
}

function applyCustomRange() {
  const s = document.getElementById('range-start').value;
  const e = document.getElementById('range-end').value;
  if (!s || !e) return;
  const startMs = new Date(s).getTime();
  const endMs = new Date(e).getTime();
  if (endMs < startMs) return;
  currentDays = Math.ceil((endMs - startMs) / 86400000) + 1;
  currentRangeLabel = s + '—' + e;
  refresh();
}

function updateChartTitles(days) {
  const label = days <= 7 ? '近 7 天' : (days <= 30 ? '近 30 天' : '近 ' + days + ' 天');
  const tl = document.getElementById('title-timeline');
  const tt = document.getElementById('title-token-timeline');
  const vd = document.getElementById('title-vendor-dist');
  const mt = document.getElementById('title-model-token-timeline');
  if (tl) tl.textContent = label + ' 请求量趋势';
  if (tt) tt.textContent = label + ' Token 量趋势（按供应商）';
  if (vd) vd.textContent = '供应商请求分布（' + label + '）';
  if (mt) mt.textContent = label + ' Token 用量（按 Vendor / 模型）';
}

// ── 主刷新逻辑 ────────────────────────────────────────────
let refreshing = false;
async function refresh() {
  if (refreshing) return;
  refreshing = true;
  document.getElementById('refresh-time').textContent = '刷新中…';
  try {
    const days = currentDays > 0 ? currentDays : 7;
    const [summary, timeline, status] = await Promise.all([
      fetchJSON('/api/dashboard/summary?days=' + days),
      fetchJSON('/api/dashboard/timeline?days=' + days),
      fetchJSON('/api/status'),
    ]);

    if (summary.version) {
      document.getElementById('version-badge').textContent = 'v' + summary.version;
    }

    updateKPI(summary);
    updateVendorStatus(status);
    updateChartTitles(days);

    const rows = timeline.rows || [];
    buildTimeline(rows);
    buildVendorDist(rows);
    buildTokenTimeline(rows);
    buildModelTokenTimeline(rows);

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
setInterval(refresh, 600000);
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

    @app.get("/favicon.ico", include_in_schema=False)
    async def favicon() -> Response:
        """返回内嵌 favicon."""
        return Response(content=_FAVICON_ICO, media_type="image/x-icon")

    @app.get("/dashboard", response_class=HTMLResponse, include_in_schema=False)
    async def dashboard() -> HTMLResponse:
        """返回 Dashboard HTML 页面."""
        return HTMLResponse(content=_DASHBOARD_HTML)

    @app.get("/api/dashboard/summary")
    async def dashboard_summary(request: Request, days: int = 7) -> Response:
        """返回 Dashboard 汇总数据（今日 / 所选区间）."""
        token_logger = getattr(request.app.state, "token_logger", None)
        pricing_table = getattr(request.app.state, "pricing_table", None)

        if token_logger is None:
            return Response(
                content=b'{"error":"token_logger not available"}',
                status_code=503,
                media_type="application/json",
            )

        days = max(1, min(days, 90))  # 限制范围 1~90 天

        try:
            # 今日（最近 1 天）
            today_rows = await token_logger.query_usage(period=TimePeriod.DAY, count=1)
            # 所选区间
            range_rows = await token_logger.query_usage(
                period=TimePeriod.DAY, count=days
            )
            # 故障转移（所选区间）
            failover_stats = await token_logger.query_failover_stats(days=days)
        except Exception as exc:
            logger.error("dashboard_summary query error: %s", exc, exc_info=True)
            return Response(
                content=b'{"error":"query failed"}',
                status_code=500,
                media_type="application/json",
            )

        today = _sum_rows(today_rows)
        range_stat = _sum_rows(range_rows)

        today["cost"] = _compute_cost_str(today_rows, pricing_table)
        range_stat["cost"] = _compute_cost_str(range_rows, pricing_table)

        result = {
            "version": __version__,
            "today": today,
            "range": range_stat,
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
            rows = await token_logger.query_usage(period=TimePeriod.DAY, count=days)
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
