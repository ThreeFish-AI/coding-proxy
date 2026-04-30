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
      --bg: #0a0e14;
      --bg-card: #12161e;
      --bg-card-hover: #181d27;
      --border: rgba(255,255,255,.06);
      --border-subtle: rgba(255,255,255,.04);
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
      --radius: 14px;
      --radius-sm: 8px;
      --shadow: 0 1px 2px rgba(0,0,0,.3);
      --shadow-md: 0 8px 24px rgba(0,0,0,.3);
      --glow-blue: 0 0 0 1px rgba(88,166,255,.1), 0 8px 32px rgba(88,166,255,.04);
      --gradient-primary: linear-gradient(135deg, #667eea, #764ba2);
    }
    @keyframes fadeInUp {
      from { opacity: 0; transform: translateY(10px); }
      to   { opacity: 1; transform: translateY(0); }
    }
    *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
    body {
      background: var(--bg);
      color: var(--text-primary);
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", "Helvetica Neue", Arial, sans-serif;
      font-size: 15px;
      line-height: 1.5;
      min-height: 100vh;
      -webkit-font-smoothing: antialiased;
      -moz-osx-font-smoothing: grayscale;
      text-rendering: optimizeLegibility;
    }
    /* ── 头部 ── */
    header {
      background: rgba(10,14,20,.9);
      backdrop-filter: blur(6px);
      -webkit-backdrop-filter: blur(6px);
      border-bottom: 1px solid rgba(255,255,255,.04);
      padding: 16px 32px;
      display: flex;
      align-items: center;
      justify-content: space-between;
      position: sticky;
      top: 0;
      z-index: 100;
    }
    .header-left { display: flex; align-items: center; gap: 12px; }
    .logo {
      width: 32px; height: 32px;
      background: var(--gradient-primary);
      border-radius: 10px;
      display: flex; align-items: center; justify-content: center;
      font-size: 15px; font-weight: 700; color: #fff;
      box-shadow: 0 4px 12px rgba(102,126,234,.25);
    }
    h1 { font-size: 18px; font-weight: 600; color: var(--text-primary); letter-spacing: -.3px; }
    .header-right { display: flex; align-items: center; gap: 12px; }
    .badge {
      font-size: 12px; padding: 2px 8px;
      border-radius: 12px;
      background: rgba(88,166,255,.1);
      color: var(--accent-blue);
      border: 1px solid rgba(88,166,255,.2);
      font-family: 'JetBrains Mono', monospace;
    }
    .refresh-time { font-size: 12px; color: var(--text-tertiary); }
    .btn-refresh {
      padding: 5px 12px; border-radius: var(--radius-sm);
      background: rgba(48,54,61,.5);
      border: 1px solid var(--border);
      color: var(--text-secondary);
      font-size: 13px; cursor: pointer;
      transition: all .2s ease;
    }
    .btn-refresh:hover {
      background: var(--bg-card-hover);
      color: var(--text-primary);
      border-color: rgba(88,166,255,.4);
    }
    /* ── 主内容 ── */
    main { padding: 28px 32px; max-width: 1440px; margin: 0 auto; }
    /* ── KPI 卡片 ── */
    .kpi-grid {
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
      gap: 16px;
      margin-bottom: 24px;
    }
    .kpi-card {
      background: rgba(18,22,30,.7);
      border: 1px solid rgba(255,255,255,.05);
      border-radius: var(--radius);
      padding: 20px 22px 18px;
      box-shadow: var(--shadow);
      backdrop-filter: blur(4px);
      -webkit-backdrop-filter: blur(4px);
      transition: all .2s ease;
      animation: fadeInUp .4s ease both;
      position: relative;
      overflow: hidden;
    }
    .kpi-card::before {
      content: '';
      position: absolute;
      top: 0; left: 0; right: 0;
      height: 1px;
      opacity: .6;
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
    }
    .kpi-header { display: flex; align-items: center; gap: 6px; margin-bottom: 8px; }
    .kpi-icon { font-size: 15px; opacity: .8; }
    .kpi-label { font-size: 13px; color: var(--text-secondary); font-weight: 600; letter-spacing: .2px; }
    .kpi-value {
      font-size: 32px; font-weight: 700; line-height: 1.2;
      font-family: 'JetBrains Mono', monospace;
      letter-spacing: -1px;
    }
    #kpi-cost-today { font-size: 20px; white-space: nowrap; }
    .kpi-sub { font-size: 13px; color: var(--text-tertiary); margin-top: 5px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; max-width: 100%; }
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
      gap: 16px;
      margin-bottom: 16px;
    }
    .charts-grid-2 {
      display: grid;
      grid-template-columns: 1fr 2fr;
      gap: 16px;
      margin-bottom: 16px;
    }
    .charts-grid > .card,
    .charts-grid-2 > .card {
      min-width: 0;
      overflow: hidden;
    }
    @media (max-width: 960px) {
      .charts-grid, .charts-grid-2 { grid-template-columns: 1fr; }
    }
    .card {
      background: rgba(18,22,30,.7);
      border: 1px solid rgba(255,255,255,.05);
      border-radius: var(--radius);
      padding: 20px 24px;
      box-shadow: var(--shadow);
      backdrop-filter: blur(4px);
      -webkit-backdrop-filter: blur(4px);
      transition: box-shadow .2s ease;
      animation: fadeInUp .4s ease both;
    }
    .card:hover { box-shadow: var(--shadow-md); }
    .card-title {
      font-size: 14px; font-weight: 600;
      color: var(--text-secondary);
      letter-spacing: .3px;
      margin-bottom: 16px;
      display: flex; align-items: center; justify-content: space-between;
    }
    .chart-wrap { position: relative; height: 260px; min-width: 0; }
    .chart-wrap-lg { position: relative; height: 260px; min-width: 0; }
    .chart-wrap-xl { position: relative; height: 280px; min-width: 0; }
    /* ── HTML Legend（单列可滚动） ── */
    .chart-with-legend {
      display: flex; align-items: stretch; gap: 0;
    }
    .chart-with-legend > .chart-wrap-xl { flex: 1 1 0; min-width: 0; }
    .html-legend-wrap {
      flex: 0 0 200px; max-height: 280px;
      overflow-y: auto; overflow-x: hidden;
      padding: 4px 0 4px 8px;
      scrollbar-width: thin;
      scrollbar-color: rgba(255,255,255,.15) transparent;
    }
    .html-legend-wrap::-webkit-scrollbar { width: 4px; }
    .html-legend-wrap::-webkit-scrollbar-track { background: transparent; }
    .html-legend-wrap::-webkit-scrollbar-thumb { background: rgba(255,255,255,.15); border-radius: 2px; }
    .html-legend-wrap ul {
      list-style: none; margin: 0; padding: 0;
      display: flex; flex-direction: column; gap: 4px;
    }
    .html-legend-wrap li {
      display: flex; align-items: center; gap: 6px;
      padding: 3px 6px; border-radius: 4px; cursor: pointer;
      font-size: 12px; font-weight: 500; color: #8b949e;
      white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
      transition: background .15s ease; user-select: none;
    }
    .html-legend-wrap li:hover { background: rgba(255,255,255,.06); }
    .html-legend-wrap li.legend-hidden { text-decoration: line-through; opacity: 0.4; }
    .html-legend-wrap li .legend-color {
      display: inline-block; width: 10px; height: 10px;
      border-radius: 50%; flex-shrink: 0;
    }
    @media (max-width: 960px) {
      .chart-with-legend { flex-direction: column; }
      .html-legend-wrap { flex: none; max-height: 120px; padding: 8px 0 0 0; }
      .html-legend-wrap ul { flex-direction: row; flex-wrap: wrap; gap: 4px 10px; }
    }
    /* ── 供应商状态 ── */
    .vendor-list { display: flex; flex-direction: column; gap: 8px; }
    .vendor-item {
      display: flex; align-items: center; justify-content: space-between;
      padding: 12px 14px;
      background: rgba(255,255,255,.015);
      border: 1px solid rgba(255,255,255,.04);
      border-radius: var(--radius-sm);
      transition: all .2s ease;
    }
    .vendor-item:hover { background: rgba(255,255,255,.03); border-color: rgba(255,255,255,.08); }
    .vendor-info { display: flex; align-items: center; gap: 10px; }
    .vendor-avatar {
      width: 30px; height: 30px; border-radius: 8px;
      background: var(--gradient-primary);
      display: flex; align-items: center; justify-content: center;
      font-size: 13px; font-weight: 700; color: #fff;
      flex-shrink: 0;
    }
    .vendor-name { font-weight: 600; font-size: 14px; }
    .vendor-badges { display: flex; gap: 5px; flex-wrap: wrap; align-items: center; }
    .status-badge {
      font-size: 11px; padding: 2px 7px;
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
      text-align: left; font-size: 12px; color: var(--text-tertiary);
      font-weight: 600; padding: 6px 10px;
      border-bottom: 1px solid var(--border);
      letter-spacing: .4px; text-transform: uppercase;
    }
    td { padding: 8px 10px; font-size: 14px; border-bottom: 1px solid var(--border-subtle); }
    tr:last-child td { border-bottom: none; }
    tr:hover td { background: rgba(255,255,255,.02); }
    .tag-vendor {
      display: inline-block;
      font-size: 12px; padding: 2px 8px;
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
      margin-bottom: 24px; flex-wrap: wrap;
      padding: 8px 16px;
      background: rgba(18,22,30,.5);
      border: 1px solid rgba(255,255,255,.04);
      border-radius: var(--radius);
      backdrop-filter: blur(4px);
    }
    .time-range-label { font-size: 13px; color: var(--text-tertiary); font-weight: 600; }
    .range-btn {
      padding: 6px 16px; border-radius: 20px;
      background: transparent;
      border: 1px solid transparent;
      color: var(--text-secondary);
      font-size: 14px; cursor: pointer;
      transition: all .25s ease;
    }
    .range-btn:hover { background: rgba(255,255,255,.05); color: var(--text-primary); }
    .range-btn.active {
      background: rgba(88,166,255,.08);
      border-color: rgba(88,166,255,.2);
      color: var(--accent-blue);
      font-weight: 500;
    }
    .range-custom { display: none; align-items: center; gap: 6px; }
    .range-custom.visible { display: flex; }
    .range-date {
      padding: 3px 10px; border-radius: var(--radius-sm);
      background: rgba(48,54,61,.4); border: 1px solid var(--border);
      color: var(--text-primary); font-size: 13px;
      color-scheme: dark;
      transition: border-color .2s;
    }
    .range-date:focus { outline: none; border-color: rgba(88,166,255,.5); }
    .range-sep { font-size: 13px; color: var(--text-tertiary); }
    /* ── 空态 ── */
    .empty {
      text-align: center; padding: 32px;
      color: var(--text-tertiary); font-size: 14px;
    }
    .empty-icon { font-size: 32px; margin-bottom: 8px; opacity: .5; }
    /* ── Sessions Panel ── */
    .sessions-card { grid-column: 1 / -1; animation-delay: .1s; }
    .session-table-wrap { overflow: hidden; }
    .session-table { width: 100%; border-collapse: collapse; font-size: 13px; table-layout: fixed; }
    .session-table th {
      position: sticky; top: 0; z-index: 1;
      background: var(--bg-card); padding: 10px 12px;
      text-align: left; font-weight: 600; font-size: 12px;
      color: var(--text-secondary); text-transform: uppercase; letter-spacing: .5px;
      border-bottom: 1px solid var(--border);
    }
    .session-table td { padding: 8px 12px; border-bottom: 1px solid var(--border-subtle); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
    .session-table tr:hover td { background: var(--bg-card-hover); }
    .session-table .session-key { font-family: 'JetBrains Mono', monospace; font-size: 12px; color: var(--accent-blue); cursor: default; white-space: normal; overflow: visible; }
    .session-id { line-height: 1.4; word-break: break-all; }
    .session-meta { font-size: 10px; color: var(--text-tertiary); line-height: 1.2; margin-top: 2px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
    .session-tag {
      display: inline-block; font-size: 11px; padding: 2px 7px;
      border-radius: 8px; margin: 1px 2px;
      background: rgba(88,166,255,.08); border: 1px solid rgba(88,166,255,.15);
      color: var(--text-secondary);
    }
    .success-bar { width: 56px; height: 4px; border-radius: 2px; background: rgba(255,255,255,.06); display: inline-block; vertical-align: middle; margin-left: 6px; }
    .success-bar-fill { height: 100%; border-radius: 2px; }
    /* ── Vendor Bind 选择器 ── */
    .bind-select {
      padding: 3px 6px; border-radius: 6px;
      background: rgba(48,54,61,.6); border: 1px solid rgba(255,255,255,.1);
      color: var(--text-secondary); font-size: 12px;
      font-family: 'JetBrains Mono', monospace;
      cursor: pointer; outline: none;
      transition: all .2s ease;
      max-width: 120px;
    }
    .bind-select:hover { border-color: rgba(88,166,255,.4); color: var(--text-primary); }
    .bind-select:focus { border-color: rgba(88,166,255,.6); box-shadow: 0 0 0 2px rgba(88,166,255,.1); }
    .bind-select option { background: var(--bg-card); color: var(--text-primary); }
    /* ── 分页 ── */
    .session-pagination {
      display: flex; align-items: center; justify-content: space-between;
      padding: 10px 12px; border-top: 1px solid var(--border-subtle);
      font-size: 12px; color: var(--text-secondary);
    }
    .page-btn {
      padding: 4px 10px; border-radius: 6px;
      background: rgba(48,54,61,.4); border: 1px solid rgba(255,255,255,.08);
      color: var(--text-secondary); font-size: 12px; cursor: pointer;
      transition: all .15s ease;
    }
    .page-btn:hover:not(:disabled) { background: var(--bg-card-hover); color: var(--text-primary); border-color: rgba(88,166,255,.3); }
    .page-btn:disabled { opacity: .35; cursor: default; }
    .page-info { font-family: 'JetBrains Mono', monospace; font-size: 12px; }
    /* ── 加载态 ── */
    .loading { opacity: .4; pointer-events: none; }
    /* ── 图表标签截断 ── */
    .chart-legend-note { font-size: 12px; color: var(--text-tertiary); margin-top: 4px; text-align: center; }
    /* ── 外部 Tooltip ── */
    #chart-tooltip {
      position: fixed;
      pointer-events: none;
      background: rgba(10,14,20,.95);
      border: 1px solid rgba(255,255,255,.08);
      border-radius: 12px;
      padding: 12px 16px;
      font-size: 13px;
      color: var(--text-primary);
      box-shadow: 0 12px 40px rgba(0,0,0,.5);
      backdrop-filter: blur(4px);
      -webkit-backdrop-filter: blur(4px);
      z-index: 1000;
      opacity: 0;
      transition: opacity .15s ease;
      max-width: 520px;
      max-height: 60vh;
      overflow-y: auto;
      backdrop-filter: blur(8px);
      -webkit-backdrop-filter: blur(8px);
    }
    #chart-tooltip.active { opacity: 1; }
    #chart-tooltip-title {
      font-weight: 600; margin-bottom: 6px; padding-bottom: 6px;
      border-bottom: 1px solid var(--border-subtle); color: var(--text-secondary); font-size: 12px;
    }
    #chart-tooltip-items { display: flex; flex-direction: column; gap: 3px; }
    .tt-item { display: flex; align-items: center; gap: 8px; line-height: 1.4; }
    .tt-color { width: 8px; height: 8px; border-radius: 50%; flex-shrink: 0; }
    .tt-label { flex: 1; color: var(--text-primary); white-space: nowrap; }
    .tt-value { font-family: 'JetBrains Mono', monospace; font-size: 12px; color: var(--text-secondary); white-space: nowrap; }
    #chart-tooltip-footer {
      margin-top: 6px; padding-top: 6px; border-top: 1px solid var(--border-subtle);
      font-weight: 500; font-size: 12px; color: var(--text-secondary);
    }
    /* ── Tabs ─────────────────────────────────────────────────── */
    .tabs {
      display: flex;
      gap: 4px;
      margin-bottom: 16px;
      border-bottom: 1px solid var(--border);
      padding: 0 2px;
    }
    .tab-btn {
      appearance: none;
      background: transparent;
      border: none;
      border-bottom: 2px solid transparent;
      color: var(--text-secondary);
      cursor: pointer;
      font-family: inherit;
      font-size: 14px;
      font-weight: 500;
      padding: 10px 16px;
      margin-bottom: -1px;
      transition: color .15s ease, border-color .15s ease, background .15s ease;
      border-radius: 6px 6px 0 0;
    }
    .tab-btn:hover { color: var(--text-primary); background: var(--bg-card-hover); }
    .tab-btn.active { color: var(--text-primary); border-bottom-color: var(--accent-blue); }
    .tab-btn:focus-visible { outline: 2px solid var(--accent-blue); outline-offset: 2px; }
    .tab-pane { display: none; }
    .tab-pane.active { display: block; }
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
  <!-- 页签导航 -->
  <nav class="tabs" role="tablist" aria-label="Dashboard sections">
    <button type="button" class="tab-btn active" id="tab-btn-overview" role="tab" aria-controls="tab-pane-overview" aria-selected="true" data-tab="overview" onclick="switchTab('overview')">Overview</button>
    <button type="button" class="tab-btn" id="tab-btn-sessions" role="tab" aria-controls="tab-pane-sessions" aria-selected="false" data-tab="sessions" onclick="switchTab('sessions')">Recent Active Sessions</button>
  </nav>

  <!-- Overview 页签 -->
  <section class="tab-pane active" id="tab-pane-overview" role="tabpanel" aria-labelledby="tab-btn-overview" data-tab="overview">
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
    <div class="chart-with-legend">
      <div class="chart-wrap-xl">
        <canvas id="chart-model-token-timeline"></canvas>
      </div>
      <div class="html-legend-wrap" id="model-token-legend" style="display:none"></div>
    </div>
  </div>
  </section>

  <!-- Recent Active Sessions 页签 -->
  <section class="tab-pane" id="tab-pane-sessions" role="tabpanel" aria-labelledby="tab-btn-sessions" data-tab="sessions">
  <!-- Recent Active Sessions -->
  <div class="card sessions-card">
    <div class="card-title">
      <span>Recent Active Sessions</span>
      <span style="font-size:12px;color:var(--text-tertiary)" id="sessions-subtitle">Last 24h</span>
    </div>
    <div class="session-table-wrap" id="sessions-table-wrap">
      <table class="session-table">
        <colgroup>
          <col style="width:22%">
          <col style="width:8%">
          <col style="width:7%">
          <col style="width:7%">
          <col style="width:14%">
          <col style="width:12%">
          <col style="width:8%">
          <col style="width:8%">
          <col style="width:8%">
          <col style="width:6%">
        </colgroup>
        <thead>
          <tr>
            <th>Session ID</th>
            <th>Last Active</th>
            <th>Requests</th>
            <th>Tokens</th>
            <th>Models</th>
            <th>Vendors</th>
            <th>Avg Latency</th>
            <th>Success</th>
            <th>Vendor Bind</th>
            <th>Client</th>
          </tr>
        </thead>
        <tbody id="sessions-tbody">
          <tr><td colspan="10" class="empty">Loading...</td></tr>
        </tbody>
      </table>
      <div class="session-pagination" id="session-pagination">
        <span class="page-info" id="page-info"></span>
        <div style="display:flex;gap:6px;align-items:center">
          <button class="page-btn" id="btn-prev" onclick="changePage(-1)">Prev</button>
          <span class="page-info" id="page-num"></span>
          <button class="page-btn" id="btn-next" onclick="changePage(1)">Next</button>
        </div>
      </div>
    </div>
  </div>
  </section>

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

// ── Vendor 展示名映射 ─────────────────────────────────────
// 规则：以 cc/api 前缀区分代理形态，屏蔽后端 -native 消歧后缀。
// _API_VENDORS 需与后端 native_api/handler.py::_VENDOR_LABEL 对齐，
// 新增无 -native 后缀的 native vendor 时同步更新本集合。
const _API_VENDORS = new Set(['anthropic-native', 'openai', 'gemini']);
function formatVendorLabel(v) {
  if (!isValidLabel(v)) return v;
  if (_API_VENDORS.has(v)) {
    const name = v.endsWith('-native') ? v.slice(0, -'-native'.length) : v;
    return 'api | ' + name;
  }
  return 'cc | ' + v;
}

// ── 渐变填充工具 ──────────────────────────────────────────
function makeGradient(ctx, color) {
  const h = ctx.canvas.height;
  const grad = ctx.createLinearGradient(0, 0, 0, h);
  grad.addColorStop(0, color + '30');
  grad.addColorStop(1, color + '00');
  return grad;
}

// ── Chart.js 全局默认 ─────────────────────────────────────
Chart.defaults.color = '#8b949e';
Chart.defaults.borderColor = 'rgba(255,255,255,.03)';
Chart.defaults.font.family = '-apple-system, BlinkMacSystemFont, "Segoe UI", "PingFang SC", "Microsoft YaHei", sans-serif';
Chart.defaults.font.size = 13;
Chart.defaults.plugins.tooltip.usePointStyle = true;
Chart.defaults.devicePixelRatio = window.devicePixelRatio || 1;

const COMMON_SCALE_X = { grid: { display: false }, ticks: { maxTicksLimit: 10, font: { size: 12 } } };
const COMMON_SCALE_Y = { grid: { color: 'rgba(255,255,255,.03)' }, beginAtZero: true, ticks: { font: { size: 12 } } };
const COMMON_LEGEND = {
  position: 'bottom',
  labels: {
    boxWidth: 10,
    boxHeight: 10,
    padding: 14,
    usePointStyle: true,
    pointStyle: 'circle',
    font: { size: 13, weight: '500' },
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
const COMMON_LINE_DATASET = { tension: .4, pointRadius: 0, pointHoverRadius: 4, borderWidth: 1.5 };

// ── HTML Legend 插件（model-token-timeline 专用）─────────────
const htmlLegendPlugin = {
  id: 'htmlLegend',
  afterUpdate(chart, args, options) {
    const containerID = options.containerID;
    if (!containerID) return;
    const container = document.getElementById(containerID);
    if (!container) return;
    let ul = container.querySelector('ul');
    if (!ul) { ul = document.createElement('ul'); container.appendChild(ul); }
    while (ul.firstChild) ul.firstChild.remove();

    const genLabels = chart.options.plugins.legend.labels.generateLabels;
    const items = genLabels ? genLabels(chart) : [];
    items.forEach(item => {
      const li = document.createElement('li');
      if (item.hidden) li.classList.add('legend-hidden');

      const dot = document.createElement('span');
      dot.className = 'legend-color';
      dot.style.backgroundColor = item.hidden ? 'rgba(255,255,255,.15)' : (item.fillStyle || item.strokeStyle);

      const txt = document.createElement('span');
      txt.style.overflow = 'hidden';
      txt.style.textOverflow = 'ellipsis';
      txt.textContent = item.text || '';
      txt.title = item.fullText || item.text || '';

      li.appendChild(dot);
      li.appendChild(txt);
      li.addEventListener('click', e => {
        legendOnClick({ native: e }, { datasetIndex: item.datasetIndex, index: item.index, text: item.text, hidden: item.hidden }, { chart });
      });
      ul.appendChild(li);
    });
  },
};

// ── 外部 Tooltip 工厂（数据项较多时可溢出卡片边界）────────
function makeExternalTooltipHandler(opts = {}) {
  const {
    fmtValue = fmtTokens,
    showTotal = true,
    fmtTotal = null,
  } = opts;
  const fmtTotalFn = fmtTotal || fmtValue;

  return function(context) {
    const { chart, tooltip } = context;
    const el = document.getElementById('chart-tooltip');
    if (!el) return;

    if (!tooltip.opacity) {
      el.classList.remove('active');
      return;
    }

    const titleLines = tooltip.title || [];
    const dataPoints = tooltip.dataPoints || [];
    const visiblePoints = dataPoints.filter(dp => dp.raw);

    let html = '';
    if (titleLines.length) {
      html += '<div id="chart-tooltip-title">' + titleLines.join('<br>') + '</div>';
    }
    if (visiblePoints.length) {
      html += '<div id="chart-tooltip-items">';
      visiblePoints.forEach(dp => {
        // 折线图: borderColor 为字符串；甜甜圈图: backgroundColor 为数组
        const bg = dp.dataset.backgroundColor;
        const color = dp.dataset.borderColor
          || (Array.isArray(bg) ? bg[dp.dataIndex] : bg)
          || '#8b949e';
        const label = dp.dataset.label || dp.label || '';
        const value = fmtValue(dp.raw);
        html += '<div class="tt-item">' +
          '<span class="tt-color" style="background:' + color + '"></span>' +
          '<span class="tt-label">' + label + '</span>' +
          '<span class="tt-value">' + value + '</span>' +
          '</div>';
      });
      html += '</div>';
    }
    if (showTotal && visiblePoints.length > 1) {
      const total = visiblePoints.reduce((s, dp) => s + (dp.raw || 0), 0);
      if (total > 0) {
        html += '<div id="chart-tooltip-footer">合计: ' + fmtTotalFn(total) + '</div>';
      }
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

    if (left + elW > window.innerWidth - 10) {
      left = canvasRect.left + caretX - elW - 14;
    }
    if (top + elH > window.innerHeight - 10) {
      top = window.innerHeight - elH - 10;
    }
    if (top < 10) top = 10;

    el.style.left = left + 'px';
    el.style.top = top + 'px';
    el.classList.add('active');
  };
}

const EXTERNAL_TOOLTIP = { enabled: false, external: makeExternalTooltipHandler() };

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
  const legendEl = document.getElementById('model-token-legend');
  if (legendEl) legendEl.innerHTML = '';
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

// ── 按 tiers 顺序排序 vendor 列表 ─────────────────────────
function sortByTierOrder(vendors, tierOrder) {
  if (!tierOrder || !tierOrder.length) return vendors.sort();
  const orderMap = {};
  tierOrder.forEach((name, i) => { orderMap[name] = i; });
  const maxIdx = tierOrder.length;
  return vendors.sort((a, b) => {
    const ia = orderMap[a] ?? maxIdx;
    const ib = orderMap[b] ?? maxIdx;
    return ia !== ib ? ia - ib : a.localeCompare(b);
  });
}

// ── 时序折线图（请求量，按 vendor）────────────────────────
function buildTimeline(rows, tierOrder) {
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
  const vendors = sortByTierOrder(Object.keys(vendorDateMap), tierOrder);

  if (chartTimeline) chartTimeline.destroy();
  const ctx = document.getElementById('chart-timeline').getContext('2d');
  const datasets = vendors.map((v, i) => {
    const color = VENDOR_COLORS[i % VENDOR_COLORS.length];
    return {
      ...COMMON_LINE_DATASET,
      label: formatVendorLabel(v),
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
          enabled: false,
          external: makeExternalTooltipHandler({ fmtValue: fmtNum }),
          itemSort: (a, b) => (b.raw || 0) - (a.raw || 0),
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
function buildVendorDist(rows, tierOrder) {
  const vendorTotals = {};
  for (const r of rows) {
    const v = r.vendor;
    if (!isValidLabel(v)) continue;
    vendorTotals[v] = (vendorTotals[v] || 0) + (r.total_requests || 0);
  }
  const labels = sortByTierOrder(Object.keys(vendorTotals), tierOrder);
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
      labels: labels.map(formatVendorLabel),
      datasets: [{
        data,
        backgroundColor: labels.map((_,i) => VENDOR_COLORS[i % VENDOR_COLORS.length]),
        borderWidth: 0,
        hoverOffset: 8,
      }],
    },
    options: {
      responsive: true, maintainAspectRatio: false,
      cutout: '55%',
      plugins: {
        legend: {
          position: 'right',
          onClick: legendOnClick,
          labels: {
            ...COMMON_LEGEND.labels,
            generateLabels: chart => {
              const ds = chart.data.datasets[0];
              return chart.data.labels.map((label, i) => ({
                text: label,
                fillStyle: ds.backgroundColor[i],
                strokeStyle: ds.backgroundColor[i],
                fontColor: '#e6edf3',
                color: '#e6edf3',
                lineWidth: 0,
                hidden: !chart.getDataVisibility(i),
                index: i,
                pointStyle: 'circle',
              }));
            },
          },
        },
        tooltip: {
          enabled: false,
          external: makeExternalTooltipHandler({
            fmtValue: v => v.toLocaleString() + ' 次',
            showTotal: false,
          }),
        },
      },
    },
  });
}

// ── Token 量趋势折线图（按 vendor）───────────────────────
function buildTokenTimeline(rows, tierOrder) {
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
  const vendors = sortByTierOrder(Object.keys(vendorDateMap), tierOrder);

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
      label: formatVendorLabel(v),
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
          ...EXTERNAL_TOOLTIP,
          itemSort: (a, b) => (b.raw || 0) - (a.raw || 0),
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
    const key = formatVendorLabel(v) + ' / ' + m;
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

  // 清理 HTML Legend 容器
  const legendEl = document.getElementById('model-token-legend');
  if (legendEl) legendEl.innerHTML = '';

  if (!dates.length || !keys.length) {
    if (legendEl) legendEl.style.display = 'none';
    ctx.canvas.parentElement.innerHTML = '<div class="empty"><div class="empty-icon">📭</div>暂无数据</div>';
    return;
  }

  // 数据集 > 8 时启用 HTML Legend（单列可滚动）
  const useHtmlLegend = keys.length > 8;
  if (legendEl) legendEl.style.display = useHtmlLegend ? 'block' : 'none';

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

  const modelLegendLabels = {
    ...COMMON_LEGEND.labels,
    generateLabels: chart => {
      const items = COMMON_LEGEND.labels.generateLabels(chart);
      const maxLen = 32;
      items.forEach(item => {
        item.fullText = item.text;
        if (item.text.length > maxLen) item.text = item.text.slice(0, maxLen) + '…';
        item.pointStyle = 'circle';
        item.lineWidth = 0;
        item.fillStyle = item.strokeStyle;
      });
      return items;
    },
  };

  chartModelTokenTimeline = new Chart(ctx, {
    type: 'line',
    data: { labels: dates, datasets },
    plugins: useHtmlLegend ? [htmlLegendPlugin] : [],
    options: {
      responsive: true, maintainAspectRatio: false,
      interaction: { mode: 'index', intersect: false },
      plugins: {
        legend: {
          ...COMMON_LEGEND,
          display: !useHtmlLegend,
          onClick: legendOnClick,
          labels: modelLegendLabels,
        },
        htmlLegend: { containerID: 'model-token-legend' },
        tooltip: {
          ...EXTERNAL_TOOLTIP,
          itemSort: (a, b) => (b.raw || 0) - (a.raw || 0),
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

// ── Sessions Panel ──────────────────────────────────────────────
function relativeTime(tsStr) {
  if (!tsStr) return '–';
  var d = new Date(tsStr.replace('Z', '+00:00'));
  var diff = (Date.now() - d.getTime()) / 1000;
  if (diff < 60) return 'just now';
  if (diff < 3600) return Math.floor(diff / 60) + 'm ago';
  if (diff < 86400) return Math.floor(diff / 3600) + 'h ago';
  return Math.floor(diff / 86400) + 'd ago';
}
function escapeHtml(s) {
  if (!s) return '';
  return s.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;').replace(/'/g,'&#39;');
}
function truncateKey(key, maxLen) {
  if (!key || key.length <= maxLen) return escapeHtml(key) || '–';
  return escapeHtml(key.slice(0, maxLen - 3)) + '…';
}
function parseSessionKey(raw) {
  try { var o = JSON.parse(raw); return { device_id: o.device_id||'', account_uuid: o.account_uuid||'', session_id: o.session_id||'' }; }
  catch(e) { return { device_id:'', account_uuid:'', session_id: raw || '' }; }
}
function shortId(s, n) { return s ? (s.length <= n ? s : s.slice(0, n) + '…') : ''; }
function successBarHtml(pct) {
  if (pct == null) return '–';
  var p = Math.round(pct);
  var color = p >= 95 ? 'var(--accent-green)' : (p >= 80 ? 'var(--accent-yellow)' : 'var(--accent-red)');
  return '<span style="font-family:JetBrains Mono,monospace;font-size:12px">' + p + '%</span>' +
    '<span class="success-bar"><span class="success-bar-fill" style="width:' + p + '%;background:' + color + '"></span></span>';
}
function formatSessionTags(str, max) {
  if (!str) return '–';
  var list = str.split(',');
  var html = list.slice(0, max).map(function(c) {
    return '<span class="session-tag">' + escapeHtml(c.trim()) + '</span>';
  }).join('');
  if (list.length > max) html += '<span class="session-tag">+' + (list.length - max) + '</span>';
  return html;
}
function formatCategories(cats) {
  if (!cats) return '–';
  return cats.split(',').map(function(c) {
    var t = c.trim();
    var label = t === 'cc' ? 'Claude Code' : (t === 'api' ? 'API' : escapeHtml(t));
    return '<span class="session-tag">' + label + '</span>';
  }).join('');
}
function formatVendorTags(vendors) {
  if (!vendors) return '–';
  return vendors.split(',').map(function(v) {
    return '<span class="session-tag">' + formatVendorLabel(v.trim()) + '</span>';
  }).join('');
}
// ── Sessions Pagination State ──
var allSessions = [];
var sessionPage = 0;
var sessionPageSize = 30;
var sessionBindMap = {};
var sessionAvailableVendors = [];

async function updateSessions() {
  try {
    var results = await Promise.allSettled([
      fetchJSON('/api/dashboard/sessions?hours=24&limit=200'),
      fetchJSON('/api/session-vendor'),
      fetchJSON('/api/status'),
    ]);
    if (results[0].status === 'rejected') throw results[0].reason;
    var data = results[0].value;
    var bindData = results[1].status === 'fulfilled' ? results[1].value : {bindings: []};
    var statusData = results[2].status === 'fulfilled' ? results[2].value : {tiers: []};
    allSessions = data.sessions || [];
    sessionBindMap = {};
    (bindData.bindings || []).forEach(function(b) { sessionBindMap[b.session_key] = b.vendors; });
    sessionAvailableVendors = (statusData.tiers || []).map(function(t) { return t.name; });
    var subtitle = document.getElementById('sessions-subtitle');
    if (subtitle) subtitle.textContent = 'Last ' + data.hours + 'h';
    sessionPage = 0;
    renderSessionPage();
  } catch (e) {
    console.error('Sessions refresh error:', e);
  }
}

function renderSessionPage() {
  var total = allSessions.length;
  var totalPages = Math.max(1, Math.ceil(total / sessionPageSize));
  if (sessionPage >= totalPages) sessionPage = totalPages - 1;
  var start = sessionPage * sessionPageSize;
  var page = allSessions.slice(start, start + sessionPageSize);
  var tbody = document.getElementById('sessions-tbody');

  if (!total) {
    tbody.innerHTML = '<tr><td colspan="10" class="empty"><div class="empty-icon">📭</div>No session data</td></tr>';
  } else {
    tbody.innerHTML = page.map(function(s) {
      var parsed = parseSessionKey(s.session_key);
      var boundVendors = sessionBindMap[s.session_key];
      var selectHtml = buildBindSelect(s.session_key, boundVendors, sessionAvailableVendors);
      return '<tr>' +
        '<td class="session-key">' +
          '<div class="session-id" title="' + escapeHtml(s.session_key) + '">' + escapeHtml(parsed.session_id || s.session_key) + '</div>' +
          '<div class="session-meta" title="device: ' + escapeHtml(parsed.device_id) + ' | account: ' + escapeHtml(parsed.account_uuid) + '">' +
            'dev:' + escapeHtml(shortId(parsed.device_id, 8)) + ' · acct:' + escapeHtml(shortId(parsed.account_uuid, 8)) +
          '</div>' +
        '</td>' +
        '<td>' + relativeTime(s.last_active_ts) + '</td>' +
        '<td style="font-family:JetBrains Mono,monospace">' + fmtNum(s.total_requests) + '</td>' +
        '<td style="font-family:JetBrains Mono,monospace">' + fmtTokens(s.total_tokens) + '</td>' +
        '<td>' + formatSessionTags(s.models, 2) + '</td>' +
        '<td>' + formatVendorTags(s.vendors) + '</td>' +
        '<td style="font-family:JetBrains Mono,monospace">' + (s.avg_duration_ms ? Math.round(s.avg_duration_ms) + 'ms' : '–') + '</td>' +
        '<td>' + successBarHtml(s.success_rate) + '</td>' +
        '<td>' + selectHtml + '</td>' +
        '<td>' + formatCategories(s.client_categories) + '</td>' +
        '</tr>';
    }).join('');
  }

  document.getElementById('page-info').textContent = total + ' sessions';
  document.getElementById('page-num').textContent = (sessionPage + 1) + ' / ' + totalPages;
  document.getElementById('btn-prev').disabled = (sessionPage === 0);
  document.getElementById('btn-next').disabled = (sessionPage >= totalPages - 1);
}

function changePage(delta) {
  var totalPages = Math.max(1, Math.ceil(allSessions.length / sessionPageSize));
  sessionPage = Math.max(0, Math.min(totalPages - 1, sessionPage + delta));
  renderSessionPage();
}

function buildBindSelect(sessionKey, boundVendors, availableVendors) {
  var isBound = boundVendors && boundVendors.length > 0;
  var multiBound = isBound && boundVendors.length > 1;
  var selected = isBound ? boundVendors[0] : '';
  var html = '<select class="bind-select" data-session-key="' + escapeHtml(sessionKey) + '">';
  html += '<option value=""' + (!isBound ? ' selected' : '') + '>Default</option>';
  availableVendors.forEach(function(v) {
    var label = multiBound && v === selected ? escapeHtml(v) + ' (+' + (boundVendors.length - 1) + ')' : escapeHtml(v);
    html += '<option value="' + escapeHtml(v) + '"' + (v === selected ? ' selected' : '') + '>' + label + '</option>';
  });
  html += '</select>';
  return html;
}

async function handleBindChange(sel) {
  var sessionKey = sel.getAttribute('data-session-key');
  var vendor = sel.value;
  var previousValue = sel.getAttribute('data-previous') || '';
  try {
    var resp;
    if (vendor) {
      resp = await fetch('/api/session-vendor', {
        method: 'PUT',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({session_key: sessionKey, vendors: [vendor]}),
      });
    } else {
      resp = await fetch('/api/session-vendor/' + encodeURIComponent(sessionKey), {method: 'DELETE'});
    }
    if (!resp.ok) {
      sel.value = previousValue;
      console.error('Bind change rejected:', resp.status, await resp.text());
    }
  } catch (e) {
    sel.value = previousValue;
    console.error('Bind change failed:', e);
  }
}

var sessionsTbody = document.getElementById('sessions-tbody');
sessionsTbody.addEventListener('focus', function(e) {
  if (e.target.classList.contains('bind-select')) {
    e.target.setAttribute('data-previous', e.target.value);
  }
}, true);
sessionsTbody.addEventListener('change', function(e) {
  if (e.target.classList.contains('bind-select')) {
    handleBindChange(e.target);
  }
});

// ── 主刷新逻辑（按 Tab 分发） ──────────────────────────────
let refreshing = false;
let currentTab = 'overview';
const tabLoaded = { overview: false, sessions: false };
const TAB_LABELS = { overview: 'Overview', sessions: 'Recent Active Sessions' };

async function refreshOverview() {
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
  const tierOrder = (status.tiers || []).map(t => t.name);
  buildTimeline(rows, tierOrder);
  buildVendorDist(rows, tierOrder);
  buildTokenTimeline(rows, tierOrder);
  buildModelTokenTimeline(rows);
}

async function refreshSessions() {
  await updateSessions();
}

async function refresh() {
  if (refreshing) return;
  refreshing = true;
  try {
    // 循环：若 await 期间用户切到了尚未加载的另一页签，补一次刷新，避免 tabLoaded 错位。
    while (true) {
      const tab = currentTab;
      document.getElementById('refresh-time').textContent = '刷新中…';
      try {
        if (tab === 'sessions') {
          await refreshSessions();
        } else {
          await refreshOverview();
        }
        tabLoaded[tab] = true;
        if (tab === currentTab) {
          document.getElementById('refresh-time').textContent =
            '上次刷新: ' + now() + '（' + TAB_LABELS[tab] + '）';
        }
      } catch (e) {
        console.error('Dashboard refresh error:', e);
        document.getElementById('refresh-time').textContent = '刷新失败 ' + now();
      }
      if (currentTab !== tab && !tabLoaded[currentTab]) continue;
      break;
    }
  } finally {
    refreshing = false;
  }
}

// ── 页签切换（懒加载 + URL 同步） ─────────────────────────
function syncTabUrl(name) {
  try {
    const url = new URL(window.location.href);
    if (url.searchParams.get('tab') === name) return;
    url.searchParams.set('tab', name);
    window.history.replaceState({}, '', url);
  } catch (e) { /* no-op */ }
}

function applyTabState(name) {
  document.querySelectorAll('.tab-btn').forEach(function (b) {
    const active = b.getAttribute('data-tab') === name;
    b.classList.toggle('active', active);
    b.setAttribute('aria-selected', active ? 'true' : 'false');
  });
  document.querySelectorAll('.tab-pane').forEach(function (p) {
    p.classList.toggle('active', p.getAttribute('data-tab') === name);
  });
}

function switchTab(name) {
  if (name !== 'overview' && name !== 'sessions') name = 'overview';
  if (name === currentTab) {
    syncTabUrl(name);
    return;
  }
  currentTab = name;
  applyTabState(name);
  syncTabUrl(name);
  if (!tabLoaded[name]) {
    refresh();
  }
}

// ── 初始化 ────────────────────────────────────────────────
(function bootstrap() {
  let initial = 'overview';
  try {
    const t = new URL(window.location.href).searchParams.get('tab');
    if (t === 'sessions') initial = 'sessions';
  } catch (e) { /* no-op */ }
  currentTab = initial;
  applyTabState(initial);
  syncTabUrl(initial);
  refresh();                     // 仅加载初始页签的数据
  setInterval(refresh, 600000);  // 每 10 分钟刷新当前页签
})();
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
    return " + ".join(f"{cur.symbol}{amt:.2f}" for cur, amt in cost_totals.items())


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

    @app.get("/api/dashboard/sessions")
    async def dashboard_sessions(
        request: Request, hours: float = 24.0, limit: int = 20
    ) -> Response:
        """返回近期活跃会话聚合数据."""
        token_logger = getattr(request.app.state, "token_logger", None)
        if token_logger is None:
            return Response(
                content=b'{"error":"token_logger not available"}',
                status_code=503,
                media_type="application/json",
            )
        hours = max(1.0, min(hours, 168.0))
        limit = max(1, min(limit, 200))
        try:
            sessions = await token_logger.query_recent_sessions(
                limit=limit, hours=hours
            )
        except Exception as exc:
            logger.error("dashboard_sessions query error: %s", exc, exc_info=True)
            return Response(
                content=b'{"error":"query failed"}',
                status_code=500,
                media_type="application/json",
            )
        result = {"sessions": sessions, "hours": hours}
        return Response(
            content=json.dumps(result, ensure_ascii=False).encode(),
            status_code=200,
            media_type="application/json",
        )
