"""Dashboard 路由 — 流量与用量可视化看板."""

from __future__ import annotations

import hashlib
import json
import logging
import traceback
from pathlib import Path
from typing import Any, get_args

from fastapi import Request
from fastapi.responses import HTMLResponse, Response
from pydantic import BaseModel
from pydantic_core import PydanticUndefined, to_jsonable_python
from starlette.staticfiles import StaticFiles

from ..config.routing import VendorConfig
from ..config.schema import ProxyConfig
from ..config.ui_registry import REGISTRY
from ..logging.db import TimePeriod

# ── 品牌图标：Tabler prompt 核心笔划（> 与 _）+ 通透青绿圆环 ──────────────
# 设计（方案 d05）：全透明底 + 青绿渐变细圆环（stroke 0.5，仅原 1/3，通透若隐）+
# 饱和青绿渐变 >_ 笔划（stroke 2.5）。极细环与饱和笔划形成权重对比，>_ 为绝对主角；
# 青绿（#22d3ee -> #2dd4bf）承载终端/代码语义，通透明快。基础笔划取自 Tabler prompt
# （chevron M5 7l5 5l-5 5 + 下划线 M13 17l6 0，下划线长 6 单位、不会小尺寸点化）。
# 渐变用 gradientUnits="userSpaceOnUse" + 绝对坐标：纯水平下划线包围盒高度为 0，若用
# 默认 objectBoundingBox 渐变会映射失效而不着色（下划线消失），userSpaceOnUse 规避。
# 单一事实源：_PROMPT_PATHS 与 _LOGO_DEFS 同时供 favicon SVG（f-string）与页面
# logo（{{PROMPT}} / {{LOGO_DEFS}} 模板替换）消费。
_BRAND_FROM, _BRAND_TO = "#22d3ee", "#2dd4bf"
_LOGO_DEFS = (
    '<defs><linearGradient id="cpBrand" gradientUnits="userSpaceOnUse" '
    'x1="4" y1="4" x2="20" y2="20">'
    f'<stop offset="0" stop-color="{_BRAND_FROM}"/>'
    f'<stop offset="1" stop-color="{_BRAND_TO}"/>'
    "</linearGradient></defs>"
)
_PROMPT_PATHS = '<path d="M5 7l5 5l-5 5" /><path d="M13 17l6 0" />'


# ── Favicon (SVG, 现代浏览器主选) ──────────────────────────────────────────
def _build_favicon_svg() -> str:
    """生成 24×24 SVG favicon：通透青绿圆环 + 品牌渐变 prompt 笔划（方案 d05）.

    全透明底 + 青绿渐变细圆环（``r=11``、``stroke-width=0.5``、``stroke-opacity=.55``，
    通透若隐）+ 饱和青绿渐变 ``>`` 与 ``_`` 笔划（``stroke-width=2.5``）。极细环与饱和
    笔划形成权重对比，``>_`` 为视觉主角。独立 SVG 文档无 CSS 上下文，``currentColor``
    不可靠，故渐变复用 ``_LOGO_DEFS``（userSpaceOnUse，规避水平下划线不着色）单一事实源。
    """
    return (
        '<svg xmlns="http://www.w3.org/2000/svg" width="24" height="24" viewBox="0 0 24 24">'
        f"{_LOGO_DEFS}"
        '<circle cx="12" cy="12" r="11" fill="none" stroke="url(#cpBrand)" '
        'stroke-width="0.5" stroke-opacity=".55"/>'
        f'<g fill="none" stroke="url(#cpBrand)" stroke-width="2.5" '
        f'stroke-linecap="round" stroke-linejoin="round">{_PROMPT_PATHS}</g>'
        "</svg>"
    )


# ── Favicon (ICO, 32×32 光栅回退) ─────────────────────────────────────────
def _build_favicon() -> bytes:
    """程序化生成 32×32 ICO：全透明底 + 青绿渐变 prompt 笔划（旧浏览器/Safari 回退）.

    项目无 Pillow/cairosvg 等图像库，故以纯 Python 按像素光栅化：全透明底 + 青绿渐变
    （#22d3ee -> #2dd4bf）``>`` 折线与 ``_`` 下划线。沿用「全零 AND-mask + per-pixel
    alpha」透明方案（现代渲染器按 alpha 通道处理透明）。方案 d05 的细圆环（0.5px）在
    32px 下不足 1 像素、无法渲染，故 ICO 省略圆环、仅保留饱和 ``>_`` 笔划（符合「图标
    为主角、环极弱」的设计意图），完整外观由 SVG 主版承载。
    """
    import math
    import struct

    width = height = 32
    scale = width / 24.0  # SVG 24 单位 -> 像素

    # 图标笔划青绿渐变：#22d3ee -> #2dd4bf（与 _LOGO_DEFS 同源）
    r0, g0, b0 = 0x22, 0xD3, 0xEE
    r1, g1, b1 = 0x2D, 0xD4, 0xBF

    # 像素缓冲：[B, G, R, A]，默认全透明（全透底，无容器填充）
    buf: list[list[list[int]]] = [
        [[0, 0, 0, 0] for _ in range(width)] for _ in range(height)
    ]

    def brand_at(px: float, py: float) -> tuple[int, int, int]:
        """按对角参数 t 返回 (px,py) 处的青绿渐变 RGB（与 SVG url(#cpBrand) 同向）."""
        t = (px + py) / (width + height - 2)
        return (
            int(r0 + (r1 - r0) * t),
            int(g0 + (g1 - g0) * t),
            int(b0 + (b1 - b0) * t),
        )

    def stamp(px: float, py: float) -> None:
        """以 (px, py) 为中心盖 ~3px 青绿渐变方块（笔划加粗，保证小尺寸可辨识）."""
        ix, iy = int(round(px)), int(round(py))
        cr, cg, cb = brand_at(px, py)
        for oy in (-1, 0, 1):
            for ox in (-1, 0, 1):
                nx, ny = ix + ox, iy + oy
                if 0 <= nx < width and 0 <= ny < height:
                    buf[ny][nx] = [cb, cg, cr, 255]

    def line(x0: float, y0: float, x1: float, y1: float) -> None:
        """稠密采样 + stamp 形成青绿渐变粗线段."""
        dist = math.hypot(x1 - x0, y1 - y0)
        steps = max(1, int(dist * 3))
        for i in range(steps + 1):
            s = i / steps
            stamp(x0 + (x1 - x0) * s, y0 + (y1 - y0) * s)

    # prompt 笔划（SVG 24 单位 -> 像素）
    #    > 折线 (5,7)->(10,12)->(5,17)；_ 下划线 (13,17)->(19,17)
    line(5 * scale, 7 * scale, 10 * scale, 12 * scale)
    line(10 * scale, 12 * scale, 5 * scale, 17 * scale)
    line(13 * scale, 17 * scale, 19 * scale, 17 * scale)

    # 3) 打包 ICO（BMP bottom-up；全零 AND-mask，信任 per-pixel alpha 实现圆角透明）
    pixel_rows: list[bytes] = []
    for y in range(height - 1, -1, -1):  # BMP bottom-up
        row = bytearray()
        for x in range(width):
            b, g, r, a = buf[y][x]
            row.extend([b, g, r, a])  # BGRA
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
_FAVICON_SVG: str = _build_favicon_svg()

logger = logging.getLogger(__name__)

# ── 静态资源与安全头 ───────────────────────────────────────────────────────

_STATIC_DIR = Path(__file__).parent / "static"


def _asset_url(rel: str) -> str:
    """``/static/<rel>?v=<sha256[:8]>``：内容变化即换 URL（含 editable 安装）."""
    digest = hashlib.sha256((_STATIC_DIR / rel).read_bytes()).hexdigest()[:8]
    return f"/static/{rel}?v={digest}"


# 精确版本 + SRI：哈希取自 npm registry tarball（dist.integrity 校验通过）中的
# dist/chart.umd.js，jsDelivr 同路径文件逐字节一致（.min.js 为 CDN 动态生成，不可用 SRI）
_CHART_JS_URL = "https://cdn.jsdelivr.net/npm/chart.js@4.4.3/dist/chart.umd.js"
_CHART_JS_SRI = (
    "sha384-tgbB5AKnszdcfwcZtTfuhR3Ko1XZdlDfsLtkxiiAZiVkkXCkFmp+FQFh+V/UTo54"
)
_DASHBOARD_CSP = (
    "default-src 'self'; "
    "script-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; "
    "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
    "font-src https://fonts.gstatic.com; img-src 'self' data:; connect-src 'self'; "
    "frame-ancestors 'none'; object-src 'none'; base-uri 'none'"
)


def _inner_model(annotation: Any) -> type[BaseModel] | None:
    """``M`` / ``M | None`` / ``list[M]`` → ``M``."""
    for arg in (annotation, *get_args(annotation)):
        if isinstance(arg, type) and issubclass(arg, BaseModel):
            return arg
    return None


def _schema_field(path: str) -> Any:
    """registry 路径（``*`` 为 vendor 类型 / 列表下标）→ Pydantic FieldInfo."""
    parts = path.split(".")
    model: type[BaseModel] | None = ProxyConfig
    if parts[0] == "vendors":
        model, parts = VendorConfig, parts[2:]
    info = None
    for part in (p for p in parts if p != "*"):
        info = model.model_fields.get(part) if model is not None else None
        if info is None:
            return None
        model = _inner_model(info.annotation)
    return info


def _schema_defaults_json() -> str:
    """可编辑字段的 schema 缺省值（前端据此补齐未显式设置的键）；转义 ``<`` 以嵌入 script."""
    fields: dict[str, Any] = {}
    groups: dict[str, Any] = {}
    for spec in REGISTRY:
        info = _schema_field(spec.path)
        if spec.readonly or spec.widget in ("secret", "list_section") or info is None:
            continue
        if spec.widget == "optional_group":
            groups[spec.path] = _inner_model(info.annotation)().model_dump(mode="json")
        elif not info.is_required():
            value = info.get_default(call_default_factory=True)
            if value is not PydanticUndefined:
                fields[spec.path] = to_jsonable_python(value)
    payload = json.dumps({"fields": fields, "groups": groups}, ensure_ascii=False)
    return payload.replace("<", "\\u003c")


class _StaticAssets(StaticFiles):
    """静态资源：URL 带内容哈希，响应 ``no-cache``（ETag 协商，304 无 body）."""

    def file_response(self, *args: Any, **kwargs: Any) -> Response:
        response = super().file_response(*args, **kwargs)
        response.headers["Cache-Control"] = "no-cache"
        response.headers["X-Content-Type-Options"] = "nosniff"
        return response


# ── HTML 模板 ──────────────────────────────────────────────────────────────

_DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>Coding Proxy Dashboard</title>
  <link rel="icon" type="image/svg+xml" href="/favicon.svg" />
  <link rel="icon" type="image/x-icon" href="/favicon.ico" />
  <link rel="preconnect" href="https://fonts.googleapis.com" />
  <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin />
  <link href="https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@400;600;700&display=swap" rel="stylesheet" />
  <script src="{{CHART_JS_URL}}" integrity="{{CHART_JS_SRI}}" crossorigin="anonymous"></script>
  <style>
    :root {
      --bg: #0a0e14;
      --bg-card: #12161e;
      --bg-card-hover: #181d27;
      --bg-secondary: #161b24;
      --border: rgba(255,255,255,.06);
      --border-subtle: rgba(255,255,255,.04);
      --text-primary: #e6edf3;
      --text-secondary: #8b949e;
      --text-tertiary: #6e7681;
      --text-muted: #6e7681;
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
      --gap-section: 12px;
      /* 表格固定列宽 token（Sessions 与 Config 共用：同一属性同一宽度；
         宽度 ≥ 大写表头实测宽 + 左右 padding，并容纳典型内容；超长内容省略号 + tooltip；
         Sessions 合计 1328px = main 内容区） */
      --col-session: 136px;
      --col-title: 152px;
      --col-time: 112px;
      --col-count: 100px;
      --col-models: 144px;
      --col-vendors: 96px;
      --col-latency: 120px;
      --col-success: 120px;
      --col-client: 128px;
      --col-order: 88px;
      --col-vendor: 120px;
      --col-name: 160px;
      --col-model: 240px;
      --col-pattern: 240px;
      --col-target: 200px;
      --col-list: 200px;
      --col-state: 128px;
      --col-currency: 104px;
      --col-price: 96px;
      --col-bool: 88px;
      --col-actions: 128px;
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
      flex-wrap: wrap;
      gap: 8px 16px;
      position: sticky;
      top: 0;
      z-index: 100;
    }
    .header-left { display: flex; align-items: center; gap: 12px; min-width: 0; }
    .header-left h1 { white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
    .logo {
      width: 32px; height: 32px;
      display: flex; align-items: center; justify-content: center;
    }
    .logo svg { width: 28px; height: 28px; display: block; filter: drop-shadow(0 0 3px rgba(45,212,191,.3)); }
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
    /* ── KPI：一条 stat strip ──
       六格合成一个表面，格间只留 hairline。数值穿文字色；图标、彩色顶线、hover 光晕这类装饰一概不加 */
    .kpi-grid {
      display: grid;
      /* 费用可能是双币种长串（¥1,234.56 + $987.65），那一列略宽，其余五列等宽 */
      grid-template-columns: repeat(3, minmax(0, 1fr)) minmax(0, 1.45fr) repeat(2, minmax(0, 1fr));
      overflow: hidden;
      background: rgba(18,22,30,.7);
      border: 1px solid rgba(255,255,255,.05);
      border-radius: var(--radius);
      box-shadow: var(--shadow);
      backdrop-filter: blur(4px);
      -webkit-backdrop-filter: blur(4px);
      margin-bottom: var(--gap-section);
    }
    .kpi-card {
      position: relative;
      min-width: 0;
      padding: 16px 20px 14px;
      border-left: 1px solid rgba(255,255,255,.06);
      animation: fadeInUp .4s ease both;
    }
    .kpi-card:nth-child(6n+1) { border-left: 0; }
    /* 首末两格的外侧内边距取卡片标准内边距：首格文字与其它卡片标题左对齐 */
    .kpi-card:first-child { padding-left: 24px; }
    .kpi-card:last-child { padding-right: 24px; }
    .kpi-label {
      font-size: 13px; font-weight: 500; color: var(--text-secondary); letter-spacing: .2px;
      white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
    }
    .kpi-value {
      margin-top: 6px;
      font-size: clamp(22px, 1.95vw, 28px); font-weight: 700; line-height: 1.15; letter-spacing: -.5px;
      font-family: 'JetBrains Mono', monospace;
      color: var(--text-primary);
      white-space: nowrap;
    }
    /* 费用：updateKPI 按「 + 」拆成两段，每段整体不断开，放不下时在段之间换行；数字比其它格小一号 */
    .kpi-cost .kpi-value {
      display: flex; flex-wrap: wrap; column-gap: 6px;
      font-size: clamp(18px, 1.5vw, 22px); line-height: 1.25; letter-spacing: -.3px;
    }
    .kpi-cost .kpi-value > span { white-space: nowrap; }
    /* 副行 = 区间标签 + 数值：放不下时在两段之间换行（数值自身不断开），不截断 */
    .kpi-sub {
      display: flex; flex-wrap: wrap; column-gap: 5px;
      margin-top: 6px; font-size: 12px; line-height: 1.35; color: var(--text-secondary);
    }
    .kpi-sub-label { min-width: 0; white-space: normal; overflow-wrap: anywhere; }
    .kpi-sub-val { white-space: nowrap; }
    /* ── KPI 响应式：平板横屏及以下转 3 列两行，手机转 2 列三行；分隔线跟着换行位置走 ── */
    @media (max-width: 1023px) {
      .kpi-grid { grid-template-columns: repeat(3, minmax(0, 1fr)); }
      .kpi-card:nth-child(3n+1) { border-left: 0; padding-left: 24px; }
      .kpi-card:nth-child(3n) { padding-right: 24px; }
      .kpi-card:nth-child(n+4) { border-top: 1px solid rgba(255,255,255,.06); }
    }
    @media (max-width: 519px) {
      .kpi-grid { grid-template-columns: repeat(2, minmax(0, 1fr)); }
      .kpi-card { padding: 14px 16px 12px; }
      .kpi-card:nth-child(3n+1) { border-left: 1px solid rgba(255,255,255,.06); padding-left: 16px; padding-right: 16px; }
      .kpi-card:nth-child(2n+1) { border-left: 0; padding-left: 24px; }
      .kpi-card:nth-child(2n) { padding-right: 24px; }
      .kpi-card:nth-child(n+3) { border-top: 1px solid rgba(255,255,255,.06); }
    }
    /* ── 图表网格 ── */
    .charts-grid-2 {
      display: grid;
      grid-template-columns: 1fr 2fr;
      gap: 16px;
      margin-bottom: var(--gap-section);
    }
    .charts-grid-2 > .card {
      min-width: 0;
      overflow: hidden;
    }
    @media (max-width: 960px) {
      .charts-grid-2 { grid-template-columns: 1fr; }
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
    /* 卡片标题栏右侧操作控件（.card-title 的 space-between 自动推至右端） */
    .btn-card-action {
      padding: 4px 10px; border-radius: 6px;
      background: rgba(48,54,61,.4); border: 1px solid rgba(255,255,255,.08);
      color: var(--text-secondary); font-size: 12px; font-weight: 500;
      letter-spacing: 0; cursor: pointer; white-space: nowrap;
      transition: all .15s ease;
    }
    .btn-card-action:hover:not(:disabled) {
      background: var(--bg-card-hover); color: var(--text-primary);
      border-color: rgba(88,166,255,.3);
    }
    .btn-card-action:disabled { opacity: .35; cursor: default; }
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
    /* ── 订阅状态（订阅档位 / 用量窗口 × 路由 tier 运行状态）── */
    .vendor-list { display: flex; flex-direction: column; gap: 8px; }
    .vendor-item {
      display: flex; align-items: center; justify-content: space-between;
      column-gap: 10px;
      /* 左内边距为 24px 宽的手柄留位（最小命中区），有无手柄的行头像左缘对齐 */
      padding: 12px 14px 12px 38px;
      background: rgba(255,255,255,.015);
      border: 1px solid rgba(255,255,255,.04);
      border-radius: var(--radius-sm);
      transition: all .2s ease;
    }
    .vendor-item:hover { background: rgba(255,255,255,.03); border-color: rgba(255,255,255,.08); }
    /* 标识区定宽（容得下最长的行名），各行的订阅区从同一竖线起排；行名不折行 */
    .vendor-info { display: flex; align-items: center; gap: 10px; flex: 0 0 auto; min-width: 168px; }
    .vendor-avatar {
      width: 30px; height: 30px; border-radius: 8px;
      background: var(--gradient-primary);
      display: flex; align-items: center; justify-content: center;
      font-size: 13px; font-weight: 700; color: #fff;
      flex-shrink: 0;
    }
    .vendor-name { font-weight: 600; font-size: 14px; white-space: nowrap; }
    /* 分区（订阅区 / 路由区）：单行硬约束，放不下时分区内横向滚动而不是折行。滚动条隐藏：
       经典滚动条平台（Windows / Linux、macOS 接鼠标）上它会占掉分区的行内高度，让放不下
       的那一行文字比行名高出几个像素 */
    .vendor-badges {
      display: flex; align-items: center; column-gap: 10px;
      flex-wrap: nowrap; white-space: nowrap;
      min-width: 0; overflow-x: auto;
      scrollbar-width: none;
    }
    .vendor-badges::-webkit-scrollbar { display: none; }
    /* 分区标签（tier 名 / 模型族）统一灰字；档位是订阅的关键属性，重一级 */
    .zone-label { font-size: 11px; font-weight: 500; color: var(--text-secondary); white-space: nowrap; }
    .zone-label.plan { font-size: 12px; font-weight: 600; color: var(--text-primary); }
    /* 同一模型族的标签与窗口成组 */
    .qg { display: inline-flex; align-items: center; gap: 6px; }
    /* 用量格：窗口标签 + 细进度条 + 百分比（订阅窗口再缀重置倒计时）。格底色与进度条按严重度取色，
       数值一律穿文字色 */
    .quota-group {
      display: inline-flex; align-items: center; gap: 5px; white-space: nowrap;
      border: 1px solid transparent; border-radius: 6px;
    }
    .quota-group.q-ok { background: rgba(63,185,80,.12); border-color: rgba(63,185,80,.2); }
    .quota-group.q-warn { background: rgba(210,153,34,.12); border-color: rgba(210,153,34,.2); }
    .quota-group.q-err { background: rgba(248,81,73,.12); border-color: rgba(248,81,73,.2); }
    .quota-label { font-size: 11px; color: var(--text-tertiary); min-width: 16px; text-align: right; }
    .quota-val { font-size: 11px; color: var(--text-secondary); font-variant-numeric: tabular-nums; min-width: 24px; text-align: right; }
    .sub-reset { display: inline-flex; align-items: center; gap: 3px; font-size: 11px; color: var(--text-tertiary); white-space: nowrap; }
    .status-badge {
      font-size: 11px; padding: 2px 7px;
      border-radius: 10px;
      font-weight: 500;
    }
    .sb-ok { background: rgba(63,185,80,.12); color: var(--accent-green); border: 1px solid rgba(63,185,80,.2); }
    .sb-warn { background: rgba(210,153,34,.12); color: var(--accent-yellow); border: 1px solid rgba(210,153,34,.2); }
    .sb-err { background: rgba(248,81,73,.12); color: var(--accent-red); border: 1px solid rgba(248,81,73,.2); }
    .sb-info { background: rgba(88,166,255,.12); color: var(--accent-blue); border: 1px solid rgba(88,166,255,.2); }
    .quota-bar-wrap { flex: 0 0 40px; width: 40px; }
    .quota-bar-bg {
      display: block; height: 4px; border-radius: 2px;
      background: rgba(255,255,255,.08);
      overflow: hidden;
    }
    .quota-bar-fill {
      display: block; height: 100%; border-radius: 2px;
      transition: width .6s cubic-bezier(.4,0,.2,1);
    }
    .quota-group.q-ok .quota-bar-fill { background: var(--accent-green); }
    .quota-group.q-warn .quota-bar-fill { background: var(--accent-yellow); }
    .quota-group.q-err .quota-bar-fill { background: var(--accent-red); }
    /* ── 订阅状态：拖拽排序（Pointer Events，手柄发起） ── */
    .vendor-item.dragging { opacity: .4; }
    .drag-handle {
      display: flex; align-items: center; justify-content: center;
      position: absolute; left: 14px; top: 0; width: 24px; height: 100%;
      padding: 0; border: 0; background: none; font-family: inherit;
      color: var(--text-tertiary); font-size: 15px; line-height: 1;
      cursor: grab; user-select: none; touch-action: none;
      opacity: 0; transition: opacity .2s ease;
    }
    .drag-handle:active { cursor: grabbing; }
    .vendor-item:hover .drag-handle { opacity: .55; }
    .drag-handle:hover, .drag-handle:focus-visible { opacity: 1 !important; color: var(--text-secondary); }
    /* 键盘焦点环不做淡入：调整优先级后行节点会被重建，淡入会让焦点环闪烁 */
    .drag-handle:focus-visible { transition: none; }
    /* 同一实体一行：[标识] [订阅区] [路由区]。订阅区吃掉余宽，路由区不收缩、靠行尾右对齐成一列；
       余量不足时只由订阅区内横向滚动（单行硬约束）。手柄脱离文档流落在行左内边距里 */
    .vendor-item[data-vendor] { position: relative; }
    .has-sub > .sub-zone { flex: 1 1 auto; }
    .tier-zone { flex: 0 0 auto; justify-content: flex-end; }
    /* 路由区只剩可用状态：无异常是「圆点 + 正常」，异常才是彩色胶囊（.status-badge） */
    .tier-avail { display: inline-flex; align-items: center; column-gap: 6px; white-space: nowrap; }
    .avail-ok { display: inline-flex; align-items: center; gap: 6px; font-size: 12px; color: var(--text-secondary); white-space: nowrap; padding: 2px 0; }
    .avail-dot { width: 6px; height: 6px; border-radius: 50%; background: var(--accent-green); }
    .zone-note { font-size: 11px; color: var(--text-tertiary); white-space: nowrap; }
    .list-note { padding: 0 14px 0 38px; font-size: 12px; }
    .subscription-card .vendor-list + .vendor-list { margin-top: 8px; }
    .subscription-card .vendor-list + .vendor-list:empty { display: none; }
    @media (max-width: 960px) {
      /* 窄屏：标识与路由区占首行，订阅区独占次行（整行放不下才折行，分区内仍不折行） */
      .subscription-card .vendor-item { flex-wrap: wrap; row-gap: 6px; }
      .subscription-card .vendor-info { flex: 1 1 auto; min-width: 0; }
      .subscription-card .has-sub > .sub-zone { order: 3; flex: 1 1 100%; }
      .subscription-card .has-sub > .tier-zone,
      .subscription-card .has-sub > .sub-only { order: 2; flex: 0 1 auto; }
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
      margin-bottom: var(--gap-section); flex-wrap: wrap;
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
    .session-table-wrap { overflow-x: auto; }
    .session-table { min-width: max-content; }
    .session-table th { white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
    .session-table { width: 100%; border-collapse: collapse; font-size: 13px; table-layout: fixed; }
    .session-table th {
      position: sticky; top: 0; z-index: 1;
      background: var(--bg-card); padding: 10px 12px;
      text-align: left; font-weight: 600; font-size: 12px;
      color: var(--text-secondary); text-transform: uppercase; letter-spacing: .5px;
      border-bottom: 1px solid var(--border);
    }
    .session-table td { padding: 8px 12px; border-bottom: 1px solid var(--border-subtle); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
    .session-table td.cell-tags { white-space: normal; overflow: visible; text-overflow: clip; line-height: 1.8; vertical-align: middle; }
    .session-table tr:hover td { background: var(--bg-card-hover); }
    .session-table .session-key { font-family: 'JetBrains Mono', monospace; font-size: 12px; color: var(--accent-blue); cursor: default; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
    .session-table .session-title { font-size: 12px; color: var(--text-secondary); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; max-width: 0; }
    .session-id { display: flex; align-items: center; gap: 4px; }
    .session-id-text { overflow: hidden; text-overflow: ellipsis; }
    .copy-btn { background: none; border: none; color: var(--text-tertiary); cursor: pointer; padding: 2px; border-radius: 4px; font-size: 12px; line-height: 1; opacity: .5; flex-shrink: 0; }
    .copy-btn:hover { opacity: 1; color: var(--accent-blue); background: rgba(88,166,255,.1); }
    .copy-btn.copied { color: var(--accent-green); opacity: 1; }
    .session-meta { font-size: 10px; color: var(--text-tertiary); line-height: 1.2; margin-top: 2px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
    .session-tag {
      display: inline-block; font-size: 11px; padding: 2px 7px;
      border-radius: 8px; margin: 1px 2px;
      background: rgba(88,166,255,.08); border: 1px solid rgba(88,166,255,.15);
      color: var(--text-secondary);
    }
    .session-tag-cc {
      background: rgba(63,185,80,.08); border-color: rgba(63,185,80,.15);
    }
    .session-table td.cell-success { overflow: visible; text-overflow: clip; }
    /* ── 展开行 ── */
    .session-table tr.row-detail { display: none; }
    .session-table tr.row-detail.open { display: table-row; }
    .session-table tr.row-detail td { padding: 0; }
    .detail-card {
      padding: 16px 24px; margin: 6px 0;
      background: linear-gradient(135deg, rgba(30,37,54,.95), rgba(22,28,40,.95));
      border: 1px solid rgba(88,166,255,.15); border-radius: 12px;
      font-size: 13px;
      white-space: normal; overflow: hidden;
      box-shadow: 0 4px 16px rgba(0,0,0,.3);
    }
    .detail-card .detail-item { display: flex; flex-direction: column; gap: 2px; min-width: 0; }
    .detail-card .detail-label { font-size: 11px; color: var(--text-tertiary); text-transform: uppercase; letter-spacing: .3px; }
    .detail-card .detail-value { color: var(--text-primary); line-height: 1.4; word-break: break-all; overflow-wrap: break-word; }
    .detail-title-row {
      padding-bottom: 10px; margin-bottom: 10px;
      border-bottom: 1px solid var(--border);
    }
    .detail-title-row .detail-value {
      white-space: normal;
      word-break: break-word;
      overflow-wrap: break-word;
      line-height: 1.5;
      max-height: 4.5em;
      overflow: hidden;
      display: -webkit-box;
      -webkit-line-clamp: 3;
      -webkit-box-orient: vertical;
    }
    .detail-identity-row {
      display: flex; gap: 16px;
      padding-bottom: 10px; margin-bottom: 10px;
      border-bottom: 1px solid var(--border);
    }
    .detail-identity-row .detail-item { flex: 3 1 0; }
    .detail-identity-row .detail-item:first-child { flex: 2 1 0; }
    .detail-identity-row .detail-value { font-family: 'JetBrains Mono', monospace; font-size: 12px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; word-break: normal; }
    .detail-metrics-grid {
      display: grid;
      grid-template-columns: repeat(8, 1fr);
      gap: 10px 16px;
    }
    .detail-inline-pair { display: flex; gap: 16px; }
    .detail-inline-pair > div { flex: 1; display: flex; flex-direction: column; gap: 2px; min-width: 0; }
    .session-table tbody tr[data-row]:not(.row-detail) { cursor: pointer; }
    .success-bar { width: 56px; height: 4px; border-radius: 2px; background: rgba(255,255,255,.12); display: inline-block; vertical-align: middle; margin-left: 6px; }
    .success-bar-fill { height: 100%; border-radius: 2px; display: block; }
    /* ── Model Bind 选择器 ── */
    .bind-select {
      padding: 3px 6px; border-radius: 6px;
      background: rgba(48,54,61,.6); border: 1px solid rgba(255,255,255,.1);
      color: var(--text-secondary); font-size: 12px;
      font-family: 'JetBrains Mono', monospace;
      cursor: pointer; outline: none;
      transition: all .2s ease;
      max-width: 100%;
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
      gap: 2px;
      padding: 0;
    }
    .tab-btn {
      appearance: none;
      background: transparent;
      border: 1px solid transparent;
      color: var(--text-secondary);
      cursor: pointer;
      font-family: inherit;
      font-size: 13px;
      font-weight: 500;
      padding: 4px 12px;
      transition: color .15s ease, background .15s ease, border-color .15s ease;
      border-radius: var(--radius-sm);
    }
    .tab-btn:hover { color: var(--text-primary); background: var(--bg-card-hover); }
    .tab-btn.active {
      color: var(--text-primary);
      background: rgba(88,166,255,.1);
      border-color: rgba(88,166,255,.2);
    }
    .tab-btn:focus-visible { outline: 2px solid var(--accent-blue); outline-offset: 2px; }
    .range-btn:focus-visible,
    .btn-refresh:focus-visible,
    .page-btn:focus-visible,
    .btn-card-action:focus-visible,
    .drag-handle:focus-visible,
    .copy-btn:focus-visible { outline: 2px solid var(--accent-blue); outline-offset: 2px; }
    .tab-pane { display: none; }
    .tab-pane.active { display: block; }

    /* ── Model Calling 实时状态：对齐成列的 meter list ─────
       一个表面内按行列出：vendor | 模型 | 并行度条 | 在途/上限 | 排队，五列逐行对齐 */
    .model-calling-card,
    .subscription-card,
    .timeline-card {
      margin-bottom: var(--gap-section);
    }
    .model-token-card {
      margin-bottom: var(--gap-section);
    }
    .card-note { font-size: 12px; font-weight: 400; color: var(--text-secondary); letter-spacing: 0; white-space: nowrap; }
    /* 仅供读屏的文本 */
    .sr-only {
      position: absolute; width: 1px; height: 1px; margin: -1px; padding: 0;
      overflow: hidden; clip: rect(0 0 0 0); white-space: nowrap; border: 0;
    }
    .mc-empty {
      text-align: center;
      color: var(--text-secondary);
      padding: 16px 0;
      font-size: 13px;
    }
    .mc-list {
      display: grid;
      grid-template-columns: minmax(0, 1fr);
      border: 1px solid var(--border-subtle);
      border-radius: var(--radius-sm);
      background: var(--bg-secondary);
      overflow: hidden;
    }
    .mc-row {
      display: grid;
      /* 计数列定宽并右对齐：条形终点逐行对齐，不随「4/10」「5/8」的字数变化 */
      grid-template-columns: minmax(72px, 96px) minmax(0, 1.5fr) minmax(80px, 1fr) 64px minmax(80px, auto);
      align-items: center;
      column-gap: 12px;
      min-height: 38px;
      padding: 6px 16px;
    }
    .mc-row + .mc-row { border-top: 1px solid rgba(255,255,255,.04); }
    /* 视口不少于 1280px 且不少于 3 行时排两列（再窄模型名会被挤到截断）：列间竖线，行间横线 */
    @media (min-width: 1280px) {
      .mc-list.mc-cols-2 { grid-template-columns: repeat(2, minmax(0, 1fr)); }
      .mc-list.mc-cols-2 .mc-row + .mc-row { border-top: 0; }
      .mc-list.mc-cols-2 .mc-row:nth-child(n+3) { border-top: 1px solid rgba(255,255,255,.04); }
      .mc-list.mc-cols-2 .mc-row:nth-child(even) { border-left: 1px solid rgba(255,255,255,.04); }
    }
    .mc-vendor,
    .mc-model-name {
      font-family: 'JetBrains Mono', monospace;
      white-space: nowrap; overflow: hidden; text-overflow: ellipsis; min-width: 0;
    }
    .mc-vendor { font-size: 11px; color: var(--text-secondary); }
    .mc-model-name { font-size: 12px; color: var(--text-primary); }
    .mc-meter {
      height: 6px; border-radius: 3px; overflow: hidden;
      background: rgba(255,255,255,.16);
    }
    .mc-fill {
      display: block; height: 100%; border-radius: 3px;
      transition: width .6s cubic-bezier(.4,0,.2,1), background .3s ease;
    }
    .mc-fill.mc-low { background: var(--accent-green); }
    .mc-fill.mc-mid { background: var(--accent-yellow); }
    .mc-fill.mc-high { background: var(--accent-red); }
    /* monitor 模式没有上限：不出进度条，条的位置写明只监控 */
    .mc-meter-note { display: none; font-size: 11px; color: var(--text-secondary); white-space: nowrap; }
    .mc-monitor .mc-meter { display: none; }
    .mc-monitor .mc-meter-note { display: block; }
    .mc-note-narrow { display: none; }
    .mc-use {
      display: flex; align-items: center; justify-content: flex-end;
      font-family: 'JetBrains Mono', monospace; font-size: 12px; color: var(--text-secondary);
      white-space: nowrap; font-variant-numeric: tabular-nums;
    }
    .mc-use .mc-in { font-weight: 600; color: var(--text-primary); }
    .mc-use-unit { margin-left: 4px; }
    /* 可编辑的上限：虚线下划线是「可点」的常驻提示，行悬停或按钮聚焦时露出铅笔图标 */
    .mc-limit-editable {
      appearance: none;
      display: inline-flex; align-items: center; justify-content: flex-start;
      /* 一位数与两位数的上限同宽：斜杠后的数字左缘逐行对齐，命中区不小于 24×24 */
      width: 40px;
      background: none;
      border: 0;
      border-bottom: 1px dashed rgba(88,166,255,.8);
      border-radius: 0;
      padding: 3px 2px 3px 0;
      margin: -3px 0;
      min-height: 24px;
      font: inherit; color: inherit; text-align: left;
      cursor: pointer;
      transition: border-color .2s, color .2s;
    }
    .mc-limit-editable:hover { border-bottom-color: var(--accent-blue); color: var(--accent-blue); }
    .mc-limit-editable:focus-visible { outline: 2px solid var(--accent-blue); outline-offset: 1px; border-radius: 3px; }
    /* 编辑结果用静态色标而不是闪烁动画：系统开启「减少动态效果」时同样看得见 */
    .mc-limit-editable.mc-limit-ok { color: var(--accent-green); border-bottom-color: var(--accent-green); }
    .mc-limit-editable.mc-limit-err { color: var(--accent-red); border-bottom-color: var(--accent-red); }
    .mc-pencil { opacity: 0; margin-left: auto; flex: 0 0 auto; transition: opacity .15s ease; }
    .mc-row:hover .mc-pencil,
    .mc-limit-editable:focus-visible .mc-pencil { opacity: .75; }
    /* 编辑输入框：与它替换掉的数字同字号，宽度与按钮相同（进入编辑态列不跳） */
    .mc-limit-input {
      width: 40px; height: 24px; padding: 0 5px;
      background: var(--bg);
      border: 1px solid var(--accent-blue);
      border-radius: 4px;
      color: var(--text-primary);
      font-family: 'JetBrains Mono', monospace; font-size: 12px; text-align: left;
      outline: none;
    }
    .mc-queue { display: flex; justify-content: flex-end; white-space: nowrap; min-height: 18px; }
    .mc-badge { font-size: 11px; font-weight: 500; padding: 1px 8px; border-radius: 10px; }
    .mc-badge-pending { background: rgba(251,146,60,.15); color: #fb923c; }
    .mc-peak { font-size: 11px; color: var(--text-secondary); }
    /* 窄屏：一行放不下五列，折成两行（模型名与排队一行，vendor、并行度条与计数一行） */
    @media (max-width: 759px) {
      .mc-row {
        grid-template-columns: minmax(64px, 88px) minmax(0, 1fr) 88px;
        grid-template-areas: "model model queue" "vendor meter use";
        column-gap: 10px;
        row-gap: 8px;
        padding: 10px 14px;
      }
      .mc-vendor { grid-area: vendor; }
      .mc-model-name { grid-area: model; }
      .mc-meter { grid-area: meter; }
      .mc-monitor .mc-meter-note { grid-area: meter; }
      .mc-use { grid-area: use; }
      .mc-queue { grid-area: queue; }
      .mc-note-wide { display: none; }
      .mc-note-narrow { display: inline; }
    }
    /* ── 尊重「减少动态效果」系统偏好（前庭敏感用户）── */
    @media (prefers-reduced-motion: reduce) {
      .kpi-card, .card { animation: none !important; }
      *, *::before, *::after {
        transition-duration: .01ms !important;
        animation-duration: .01ms !important;
        animation-iteration-count: 1 !important;
      }
    }
  </style>
  <link rel="stylesheet" href="{{CONFIG_CSS}}" />
</head>
<body>
<header>
  <div class="header-left">
    <div class="logo"><svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="url(#cpBrand)" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round">{{LOGO_DEFS}}<circle cx="12" cy="12" r="11" fill="none" stroke="url(#cpBrand)" stroke-width="0.5" stroke-opacity=".55"/><g>{{PROMPT}}</g></svg></div>
    <h1>Coding Proxy Dashboard</h1>
    <span class="badge" id="version-badge">v-.-.-</span>
  </div>
  <div class="header-right">
    <nav class="tabs" role="tablist" aria-label="Dashboard sections">
      <button type="button" class="tab-btn active" id="tab-btn-overview" role="tab" aria-controls="tab-pane-overview" aria-selected="true" data-tab="overview" onclick="switchTab('overview')">Overview</button>
      <button type="button" class="tab-btn" id="tab-btn-sessions" role="tab" aria-controls="tab-pane-sessions" aria-selected="false" data-tab="sessions" onclick="switchTab('sessions')">Sessions</button>
      <button type="button" class="tab-btn" id="tab-btn-config" role="tab" aria-controls="tab-pane-config" aria-selected="false" data-tab="config" onclick="switchTab('config')">Config</button>
    </nav>
    <span class="refresh-time" id="refresh-time">正在加载…</span>
    <button class="btn-refresh" onclick="refresh()">⟳ 刷新</button>
  </div>
</header>

<main>
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

  <!-- KPI：一条 stat strip，每格 = 今日数值 + 所选区间累计 -->
  <div class="kpi-grid" id="kpi-grid">
    <div class="kpi-card">
      <div class="kpi-label">今日请求数</div>
      <div class="kpi-value" id="kpi-req-today">–</div>
      <div class="kpi-sub" id="kpi-req-week">–</div>
    </div>
    <div class="kpi-card">
      <div class="kpi-label">今日 Token 总量</div>
      <div class="kpi-value" id="kpi-tok-today">–</div>
      <div class="kpi-sub" id="kpi-tok-week">–</div>
    </div>
    <div class="kpi-card">
      <div class="kpi-label">今日输出 Token</div>
      <div class="kpi-value" id="kpi-out-today">–</div>
      <div class="kpi-sub" id="kpi-out-week">–</div>
    </div>
    <div class="kpi-card kpi-cost">
      <div class="kpi-label">今日费用估算</div>
      <div class="kpi-value" id="kpi-cost-today">–</div>
      <div class="kpi-sub" id="kpi-cost-week">–</div>
    </div>
    <div class="kpi-card">
      <div class="kpi-label">今日故障转移</div>
      <div class="kpi-value" id="kpi-fo-today">–</div>
      <div class="kpi-sub" id="kpi-fo-week">–</div>
    </div>
    <div class="kpi-card">
      <div class="kpi-label">今日平均延迟</div>
      <div class="kpi-value" id="kpi-lat-today">–</div>
      <div class="kpi-sub" id="kpi-lat-week">–</div>
    </div>
  </div>

  <!-- Model Calling 实时状态 -->
  <div class="card model-calling-card" id="model-calling-card">
    <div class="card-title">Model Calling 实时状态<span class="card-note">每 10 秒刷新</span></div>
    <div class="model-calling-wrap" id="model-calling-wrap">
      <div class="mc-empty">加载中…</div>
    </div>
  </div>

  <!-- 订阅状态：订阅档位 / 用量窗口 + 路由 tier 运行状态，同一实体合并为一行 -->
  <div class="card subscription-card" id="subscription-card">
    <div class="card-title">🎫 订阅状态<button type="button" class="btn-card-action" id="btn-vendor-reset" onclick="resetVendorStatus(this)" aria-label="状态复位：复位所有供应商的熔断 / 限流 / 配额守卫状态（保留额度用量与优先级）" title="复位所有供应商的熔断 / 限流 / 配额守卫状态（保留额度用量与优先级）">⟲ 状态复位</button></div>
    <div class="vendor-list" id="vendor-list">
      <div class="empty">加载中…</div>
    </div>
    <div class="vendor-list" id="subscription-list"></div>
  </div>

  <!-- 请求量趋势折线图 -->
  <div class="card timeline-card">
    <div class="card-title" id="title-timeline">近 7 天请求量趋势</div>
    <div class="chart-wrap-lg">
      <canvas id="chart-timeline"></canvas>
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
  <div class="card model-token-card">
    <div class="card-title" id="title-model-token-timeline">近 7 天 Token 用量（按 Vendor / 模型）</div>
    <div class="chart-with-legend">
      <div class="chart-wrap-xl">
        <canvas id="chart-model-token-timeline"></canvas>
      </div>
      <div class="html-legend-wrap" id="model-token-legend" style="display:none"></div>
    </div>
  </div>
  </section>

  <!-- Sessions 页签 -->
  <section class="tab-pane" id="tab-pane-sessions" role="tabpanel" aria-labelledby="tab-btn-sessions" data-tab="sessions">
  <!-- Sessions -->
  <div class="card sessions-card">
    <div class="session-table-wrap" id="sessions-table-wrap">
      <table class="session-table">
        <colgroup>
          <col style="width:var(--col-session)">
          <col style="width:var(--col-title)">
          <col style="width:var(--col-time)">
          <col style="width:var(--col-count)">
          <col style="width:var(--col-count)">
          <col style="width:var(--col-models)">
          <col style="width:var(--col-vendors)">
          <col style="width:var(--col-latency)">
          <col style="width:var(--col-success)">
          <col style="width:var(--col-vendor)">
          <col style="width:var(--col-client)">
        </colgroup>
        <thead>
          <tr>
            <th>Session ID</th>
            <th>Title</th>
            <th>Last Active</th>
            <th>Requests</th>
            <th>Tokens</th>
            <th>Models</th>
            <th>Vendors</th>
            <th>Avg Latency</th>
            <th>Success</th>
            <th>Model Bind</th>
            <th>Client</th>
          </tr>
        </thead>
        <tbody id="sessions-tbody">
          <tr><td colspan="11" class="empty">Loading...</td></tr>
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

  <!-- Config 页签（static/config/config.js 渲染；不参与定时刷新） -->
  <section class="tab-pane" id="tab-pane-config" role="tabpanel" aria-labelledby="tab-btn-config" data-tab="config">
    <div id="config-root"><div class="card empty">Config 页脚本未加载，请刷新页面重试</div></div>
  </section>

</main>

<div id="chart-tooltip"></div>

<!-- Config 页：先于内联脚本加载，?tab=config 启动时 window.ConfigTab 已就绪 -->
<script type="application/json" id="config-schema-defaults">{{CONFIG_SCHEMA_DEFAULTS}}</script>
<script src="{{CONFIG_JS}}"></script>
<script>
// ── 页签状态（置于脚本最前：后续任何顶层语句抛错都不影响页签切换） ──
const TABS = ['overview', 'sessions', 'config'];
const TAB_LABELS = { overview: 'Overview', sessions: 'Sessions', config: 'Config' };
const tabLoaded = { overview: false, sessions: false, config: false };
let currentTab = 'overview';
let refreshing = false;

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
function copyFromParent(btn) {
  var text = btn.parentElement.getAttribute('data-key') || btn.parentElement.getAttribute('title') || '';
  navigator.clipboard.writeText(text).then(function() {
    btn.classList.add('copied');
    btn.textContent = '✓';
    setTimeout(function() { btn.classList.remove('copied'); btn.textContent = '⧉'; }, 1500);
  });
}
function toggleRow(tr) {
  var detail = tr.nextElementSibling;
  if (!detail || !detail.classList.contains('row-detail')) return;
  var wasOpen = detail.classList.contains('open');
  // close all open rows first
  document.querySelectorAll('.session-table tr.row-detail.open').forEach(function(r) { r.classList.remove('open'); });
  if (!wasOpen) detail.classList.add('open');
}
function isValidLabel(s) { return typeof s === 'string' && s !== 'undefined' && s !== 'null' && s.trim() !== ''; }
function fmtDuration(ms) {
  if (ms == null) return '–';
  var s = ms / 1000;
  if (s < 1) return Math.round(ms) + 'ms';
  if (s < 60) return s.toFixed(1).replace(/\\.0$/, '') + 's';
  var m = Math.floor(s / 60);
  var sec = Math.round(s % 60);
  return sec > 0 ? m + 'min ' + sec + 's' : m + 'min';
}
function now() {
  return new Date().toLocaleTimeString('zh-CN', {hour:'2-digit',minute:'2-digit',second:'2-digit'});
}

// ── Vendor 展示名映射 ─────────────────────────────────────
// 规则：以 cc/api/agy 前缀区分代理形态，屏蔽后端 -native 消歧后缀。
// _API_VENDORS 需与后端 native_api/handler.py::_VENDOR_LABEL 及
// antigravity_gateway/handler.py::VENDOR_LABEL 对齐，新增透传 vendor 时同步更新。
const _API_VENDORS = new Set(['anthropic-native', 'openai', 'gemini', 'antigravity-gateway']);
function isApiVendor(v) { return _API_VENDORS.has(v); }
function vendorShortName(v) {
  if (!isValidLabel(v)) return v;
  if (isApiVendor(v)) {
    if (v === 'antigravity-gateway') return 'antigravity';
    return v.endsWith('-native') ? v.slice(0, -'-native'.length) : v;
  }
  return v;
}
function formatVendorLabel(v) {
  if (!isValidLabel(v)) return v;
  if (v === 'antigravity-gateway') return 'agy | antigravity';
  if (isApiVendor(v)) return 'api | ' + vendorShortName(v);
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

// ── Chart.js 全局默认（CDN 不可达时 window.Chart 缺失，图表降级为空态） ──
if (window.Chart) {
  Chart.defaults.color = '#8b949e';
  Chart.defaults.borderColor = 'rgba(255,255,255,.03)';
  Chart.defaults.font.family = '-apple-system, BlinkMacSystemFont, "Segoe UI", "PingFang SC", "Microsoft YaHei", sans-serif';
  Chart.defaults.font.size = 13;
  Chart.defaults.plugins.tooltip.usePointStyle = true;
  Chart.defaults.devicePixelRatio = window.devicePixelRatio || 1;
}

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

    // DOM 构建（label 含 vendor / 模型名，禁止拼接 HTML）
    const div = (id, cls, text) => {
      const d = document.createElement('div');
      if (id) d.id = id;
      if (cls) d.className = cls;
      if (text != null) d.textContent = text;
      return d;
    };
    el.replaceChildren();
    if (titleLines.length) {
      const title = div('chart-tooltip-title');
      titleLines.forEach((line, i) => {
        if (i) title.appendChild(document.createElement('br'));
        title.appendChild(document.createTextNode(String(line)));
      });
      el.appendChild(title);
    }
    if (visiblePoints.length) {
      const items = div('chart-tooltip-items');
      visiblePoints.forEach(dp => {
        // 折线图: borderColor 为字符串；甜甜圈图: backgroundColor 为数组
        const bg = dp.dataset.backgroundColor;
        const color = dp.dataset.borderColor
          || (Array.isArray(bg) ? bg[dp.dataIndex] : bg)
          || '#8b949e';
        const row = div(null, 'tt-item');
        const swatch = document.createElement('span');
        swatch.className = 'tt-color';
        swatch.style.background = String(color);
        const label = document.createElement('span');
        label.className = 'tt-label';
        label.textContent = dp.dataset.label || dp.label || '';
        const value = document.createElement('span');
        value.className = 'tt-value';
        value.textContent = fmtValue(dp.raw);
        row.append(swatch, label, value);
        items.appendChild(row);
      });
      el.appendChild(items);
    }
    if (showTotal && visiblePoints.length > 1) {
      const total = visiblePoints.reduce((s, dp) => s + (dp.raw || 0), 0);
      if (total > 0) {
        el.appendChild(div('chart-tooltip-footer', null, '合计: ' + fmtTotalFn(total)));
      }
    }

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

// 图表 canvas 恒留在 DOM 中：空态只隐藏 canvas 并显示提示，数据到达后原位重建。
// 不能用空态提示覆盖 canvas 父节点的内容（innerHTML）：canvas 一旦被移除，下一次构建取不到元素直接抛错。
function chartContext(id) {
  const canvas = document.getElementById(id);
  const note = canvas.parentElement.querySelector('.empty');
  if (note) note.remove();
  canvas.style.display = '';
  return canvas.getContext('2d');
}
function chartEmpty(ctx) {
  ctx.canvas.style.display = 'none';
  const note = document.createElement('div');
  note.className = 'empty';
  note.innerHTML = '<div class="empty-icon">📭</div>暂无数据';
  ctx.canvas.parentElement.appendChild(note);
}

// ── 数据拉取 ──────────────────────────────────────────────
async function fetchJSON(url) {
  const r = await fetch(url);
  if (!r.ok) throw new Error(r.status);
  return r.json();
}

// ── KPI 更新 ──────────────────────────────────────────────
// 费用串形如「¥298.96 + $412.37」：按「 + 」拆段，每段是一个整体（放不下时在段之间换行）
function costParts(cost) {
  const parts = String(cost || '–').split(' + ');
  return parts.map((part, i) => (i ? '+\u00a0' : '') + part);
}

function updateKPI(summary) {
  const t = summary.today, r = summary.range;
  const lbl = rangeLabel();
  // 副行 = 区间标签 + 若干数值段，用 DOM API 写入（文案来自接口）
  const sub = (id, label, ...values) => {
    const node = document.getElementById(id);
    if (!node) return;
    node.replaceChildren();
    const tag = document.createElement('span');
    tag.className = 'kpi-sub-label';
    tag.textContent = label;
    node.appendChild(tag);
    values.forEach((value) => {
      const val = document.createElement('span');
      val.className = 'kpi-sub-val';
      val.textContent = value;
      node.appendChild(val);
    });
  };
  const text = (id, value) => {
    const node = document.getElementById(id);
    if (node) node.textContent = value;
  };
  const parts = (id, values) => {
    const node = document.getElementById(id);
    if (!node) return;
    node.replaceChildren();
    values.forEach((value) => {
      const val = document.createElement('span');
      val.textContent = value;
      node.appendChild(val);
    });
  };

  text('kpi-req-today', fmtNum(t.requests));
  sub('kpi-req-week', lbl, fmtNum(r.requests));

  const tokT = t.tokens, tokR = r.tokens;
  const totalT = tokT.input + tokT.output + tokT.cache_creation + tokT.cache_read;
  const totalR = tokR.input + tokR.output + tokR.cache_creation + tokR.cache_read;
  text('kpi-tok-today', fmtTokens(totalT));
  sub('kpi-tok-week', lbl, fmtTokens(totalR));
  text('kpi-out-today', fmtTokens(tokT.output));
  sub('kpi-out-week', lbl, fmtTokens(tokR.output));

  parts('kpi-cost-today', costParts(t.cost));
  sub('kpi-cost-week', lbl, ...costParts(r.cost));

  text('kpi-fo-today', fmtNum(t.failovers));
  sub('kpi-fo-week', lbl, fmtNum(r.failovers));
  text('kpi-lat-today', t.avg_duration_ms ? t.avg_duration_ms + 'ms' : '–');
  sub('kpi-lat-week', lbl, r.avg_duration_ms ? r.avg_duration_ms + 'ms' : '–');
}

// ── 订阅状态：用量格（订阅窗口共用）─────────────────────────
// 用量格严重度（同时决定格底色与进度条填充色）：≥ 90% 红 / ≥ 70% 黄 / 其余绿
function quotaClass(pct) {
  if (pct >= 90) return 'q-err';
  if (pct >= 70) return 'q-warn';
  return 'q-ok';
}
// 配额守卫窗口短标签：按窗口小时数折成 5h / 1d / 7d
function quotaWindowLabel(wh) {
  const h = parseFloat(wh);
  if (!isFinite(h)) return '配额';
  return h >= 24 ? Math.round(h / 24) + 'd' : Math.round(h) + 'h';
}
// 用量格：窗口标签 + 进度条 + 百分比（订阅窗口再缀重置倒计时）；title 给完整写法
function renderQuotaCell(label, pct, reset, title) {
  return `<span class="quota-group ${quotaClass(pct)}" title="${escapeHtml(title)}">` +
    `<span class="quota-label">${escapeHtml(label)}</span>` +
    `<span class="quota-bar-wrap"><span class="quota-bar-bg">` +
    `<span class="quota-bar-fill" style="width:${Math.min(pct,100)}%"></span>` +
    `</span></span>` +
    `<span class="quota-val">${pct}%</span>` +
    (reset ? `<span class="sub-reset">${RESET_ICON}${escapeHtml(reset)}</span>` : '') +
    `</span>`;
}

// 供应商行头像首字母（撞字母时取前两个字母，见 SUB_INITIALS）
function _tierInitial(name) {
  const text = name || '?';
  return SUB_INITIALS.has(text.charAt(0).toUpperCase()) ? text.charAt(0).toUpperCase() + text.charAt(1) : text.charAt(0).toUpperCase();
}

// 路由区的可用状态：该 tier 现在能不能正常接请求。条件按熔断器、限速、配额守卫排列，多个并存时全部列出
// （各自是独立的不可用原因）；没有异常返回 null，由调用方渲染成「圆点 + 正常」
function tierAvailability(tier) {
  const cb = tier.circuit_breaker || {};
  const rlInfo = tier.rate_limit || {};
  const state = String(cb.state || '').toUpperCase();
  // 失败次数来自载荷：转成数字再拼，非数字一律当作 0
  const failures = Number(cb.failure_count) || 0;
  const exceeded = (g) => String((g && g.state) || '').toUpperCase() === 'QUOTA_EXCEEDED';
  const out = [];
  if (state === 'OPEN') out.push({ cls: 'sb-err', text: failures ? '熔断 ×' + failures : '熔断' });
  if (state === 'HALF_OPEN') out.push({ cls: 'sb-warn', text: '半开' });
  if (rlInfo.is_rate_limited) out.push({ cls: 'sb-warn', text: '限速中' });
  if (exceeded(tier.quota_guard) || exceeded(tier.weekly_quota_guard)) out.push({ cls: 'sb-err', text: '配额超限' });
  return out.length ? out : null;
}

// 悬停详情：不再常驻的 tier 名、熔断失败次数、限速剩余时间与配额守卫用量
// （配额守卫是本 proxy 的滑动窗口预算，与订阅区上游额度不同源）
function tierAvailabilityTitle(tier) {
  const cb = tier.circuit_breaker || {};
  const rlInfo = tier.rate_limit || {};
  const state = String(cb.state || '').toUpperCase();
  const failures = Number(cb.failure_count) || 0;
  const parts = [String(tier.name || '') + ' 的路由状态'];
  parts.push('熔断器 ' + ({ OPEN: '熔断', HALF_OPEN: '半开' }[state] || '正常') + (failures ? '（失败 ' + failures + ' 次）' : ''));
  if (rlInfo.is_rate_limited) parts.push('限速中，剩余 ' + Math.ceil(Number(rlInfo.remaining_seconds) || 0) + ' 秒');
  [tier.quota_guard, tier.weekly_quota_guard].forEach((g) => {
    if (!g || g.usage_percent == null) return;
    parts.push('配额守卫 ' + quotaWindowLabel(g.window_hours) + ' 窗口已用 ' + Math.round(g.usage_percent) + '%（阈值 ' + Math.round(Number(g.threshold_percent) || 0) + '%）');
  });
  return parts.join('；');
}

// 路由区只留可用状态：无异常是「圆点 + 正常」，异常才是彩色胶囊，整列里异常一眼可见
function _tierZoneHTML(tier) {
  const states = tierAvailability(tier);
  const inner = states
    ? states.map((s) => `<span class="status-badge ${s.cls}">${s.text}</span>`).join('')
    : '<span class="avail-ok"><i class="avail-dot" aria-hidden="true"></i>正常</span>';
  return `<span class="tier-avail" title="${escapeHtml(tierAvailabilityTitle(tier))}">${inner}</span>`;
}

let _tierStatus = { tiers: [] };   // 最近一次 /api/status 载荷
let _subData = { vendors: [] };    // 最近一次订阅载荷；tier 定向重渲染时复用，订阅区不被清空

function updateVendorStatus(status, deferRender) {
  // 仅带 tier 数据的定向重渲染入口（refreshOverview / 拖拽回滚 / 复位后刷新），订阅区取缓存。
  // deferRender：随后还有订阅数据到达时延后重绘，由 updateSubscriptions 统一重绘
  _tierStatus = status || { tiers: [] };
  if (!deferRender) _renderStatusRows();
}

// ── 订阅状态：订阅区（Claude Code / GLM / Antigravity）与合并渲染 ─────────────
const SUB_META = {
  claude: { label: 'Claude Code 订阅', initial: 'C', tier: 'anthropic' },
  glm: { label: 'GLM Coding Plan', initial: 'G', tier: 'zhipu' },
  antigravity: { label: 'Antigravity', initial: 'A', tier: 'antigravity' },
};
// tier 名 → 订阅元信息：合并行按 tier 反查显示名，订阅数据未到达时行名也不跳变。
// tier.name 恒等于 vendor 类型字面量且运行时同类型至多一个 tier，按名归并不会串行。
const SUB_BY_TIER = {};
Object.keys(SUB_META).forEach(function(key) {
  if (SUB_META[key].tier) SUB_BY_TIER[SUB_META[key].tier] = SUB_META[key];
});
// 供应商行头像首字母与订阅行头像相同时改用前两个字母（copilot → Co，alibaba → Al），
// 只取决于固定的订阅头像集合，不随行序变化
const SUB_INITIALS = new Set(Object.keys(SUB_META).map(function(key) { return SUB_META[key].initial; }));
// 窗口类别 → 短标签（三家同一套写法，类别由后端归一）；认不出的类别回退窗口的 label。
// 用 Map 而不是对象字面量：上游给到 constructor / __proto__ 一类的值时不会命中继承成员
const WINDOW_SHORT = new Map([['5h', '5h'], ['weekly', '周']]);
// 组内窗口排序：先短窗后长窗，未知类别殿后（保持上游相对顺序）
const WINDOW_RANK = new Map([['5h', 0], ['weekly', 1]]);
// 距重置的剩余时长单位：紧凑写法用于行内（30m / 5h / 7d），完整写法用于 Tooltip
const RESET_LONG = { m: ' 分钟后重置', h: ' 小时后重置', d: ' 天后重置' };
const RESET_ICON = '<svg viewBox="0 0 24 24" width="11" height="11" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/></svg>';
function fmtResetCountdown(resetEpoch) {
  const left = _resetIn(resetEpoch);
  return left === undefined ? '' : left ? left[0] + RESET_LONG[left[1]] : '即将重置';
}
function fmtResetShort(resetEpoch) {
  const left = _resetIn(resetEpoch);
  return left === undefined ? '' : left ? left[0] + left[1] : '即将';
}
// 距重置还剩多久 → [数值, 单位]；重置时间不是有限数字返回 undefined（不出倒计时），
// 已过重置点或不足半分钟返回 null（显示「即将」）
function _resetIn(resetEpoch) {
  if (!Number.isFinite(resetEpoch)) return undefined;
  const m = Math.round((resetEpoch * 1000 - Date.now()) / 60000);
  if (m <= 0) return null;
  if (m < 60) return [m, 'm'];
  const h = Math.floor(m / 60);
  return h < 48 ? [h, 'h'] : [Math.round(h / 24), 'd'];
}
function _windowRank(w) { return WINDOW_RANK.get(w.window) ?? 2; }
function windowLabel(w) {
  return WINDOW_SHORT.get(w.window) || String(w.label || w.id || '配额');
}
function renderSubWindow(w) {
  if (!w || w.used_percent == null) return '';
  const pct = Math.round(w.used_percent);
  const name = windowLabel(w);
  const reset = fmtResetShort(w.reset_epoch);
  const full = fmtResetCountdown(w.reset_epoch);
  return renderQuotaCell(name, pct, reset, (w.label || name) + '：已用 ' + pct + '%' + (full ? '，' + full : ''));
}
// 档位标签：档位名与行名相同（不分大小写）时不重复显示，如 Antigravity 的档位名就是 Antigravity
function _planLabel(v) {
  const meta = v && SUB_META[v.key];
  const plan = String((v && v.plan) || '').trim();
  const name = meta ? meta.label : (v ? v.key : '');
  return plan.toLowerCase() === String(name).trim().toLowerCase() ? '' : plan;
}
function _subZoneHTML(v) {
  let html = '';
  const plan = _planLabel(v);
  if (plan) html += `<span class="zone-label plan">${escapeHtml(plan)}</span>`;
  if (v.status === 'error' && v.error) {
    html += `<span class="status-badge sb-err" title="${escapeHtml(v.error)}">查询失败</span>`;
  } else if (v.status === 'not_configured') {
    html += `<span class="status-badge sb-warn">未启用</span>`;
  } else if (v.status === 'waiting') {
    html += `<span class="status-badge sb-warn" title="用量来自被动采集：收到首个 OAuth 订阅请求后显示">等待首个请求</span>`;
  }
  // 窗口按模型族分组（Antigravity 的 Gemini / Claude+GPT 各有双窗口），无分组的家并为一组不出标签。
  // 分组表用 Map：模型族名来自上游，取到 constructor 一类的值也不会命中继承成员
  const groups = [];
  const byGroup = new Map();
  (v.windows || []).forEach(function(w) {
    const g = String((w && w.group) || '');
    if (!byGroup.has(g)) { byGroup.set(g, []); groups.push(g); }
    byGroup.get(g).push(w);
  });
  return html + groups.map(function(g) {
    const cells = byGroup.get(g).sort(function(a, b) { return _windowRank(a) - _windowRank(b); }).map(renderSubWindow).join('');
    // 整组窗口都没有用量（used_percent 为空）时不出这个组，避免留下空订阅区
    return cells ? `<span class="qg">${g ? `<span class="zone-label">${escapeHtml(g)}</span>` : ''}${cells}</span>` : '';
  }).join('');
}

function updateSubscriptions(data) {
  _subData = (data && Array.isArray(data.vendors)) ? data : { vendors: [] };
  _renderStatusRows();
}

// 合并卡的渲染出口：订阅（/api/dashboard/subscriptions）× 路由 tier（/api/status）做 full outer join。
// 行序 = 路由优先级：带 tier 的行按 tiers 顺序排在 #vendor-list（可拖拽行须是连续前缀，拖拽按 DOM
// 顺序拼 PUT 载荷），无 tier 的订阅行排在其后的 #subscription-list。
function _renderStatusRows() {
  // 拖拽进行中 / 重排序 PUT 未决时跳过重渲染，避免抢占正在操作的 DOM
  if (_tierDrag.active || _tierDrag.inFlight) return;
  const tierList = document.getElementById('vendor-list');
  const subList = document.getElementById('subscription-list');
  if (!tierList || !subList) return;

  const tiers = (_tierStatus && _tierStatus.tiers) || [];
  const vendors = (_subData && _subData.vendors) || [];
  if (!tiers.length && !vendors.length) {
    tierList.innerHTML = '<div class="empty">无订阅数据</div>';
    subList.innerHTML = '';
    return;
  }

  const subByTier = {};
  const hasTier = {};
  tiers.forEach(function(tier) {
    hasTier[tier.name] = true;
  });
  vendors.forEach(function(v) {
    const tierName = v && SUB_META[v.key] && SUB_META[v.key].tier;
    if (tierName && !subByTier[tierName]) subByTier[tierName] = v;
  });

  const reorderable = tiers.length >= 2;
  // 重绘会替换节点：焦点若在某个手柄上（键盘调整优先级），重绘后回到同一行；
  // 放不下的行用户可能已把分区滚到右侧，各分区的横向滚动位置也要带过去
  const focusedVendor = _focusedTierHandle(tierList);
  const tierScroll = _zoneScroll(tierList);
  const subScroll = _zoneScroll(subList);
  const tierRows = tiers.map(function(tier, index) {
    const safeName = escapeHtml(tier.name || '');
    const meta = SUB_BY_TIER[tier.name];
    // Pointer Events 重排：仅需 data-vendor 定位；手柄是可聚焦按钮，键盘 ↑ / ↓ 与拖拽等价（无原生 draggable）
    const handle = reorderable ? `<button type="button" class="drag-handle" title="拖拽或按 ↑ / ↓ 调整优先级" aria-keyshortcuts="ArrowUp ArrowDown" aria-label="调整 ${escapeHtml(meta ? meta.label : tier.name || '')} 的优先级：第 ${index + 1} 位，共 ${tiers.length} 个">⠿</button>` : '';
    const routeHTML = _tierZoneHTML(tier);
    if (!meta) {
      // 无订阅对应的 tier 行：只有路由区（可用状态），不出订阅区
      const initial = escapeHtml(_tierInitial(tier.name));
      return `<div class="vendor-item" data-vendor="${safeName}">
        <div class="vendor-info">
          ${handle}
          <div class="vendor-avatar">${initial}</div>
          <span class="vendor-name">${safeName}</span>
        </div>
        <div class="vendor-badges tier-zone">
          ${routeHTML}
        </div>
      </div>`;
    }
    // 同一实体合并为一行：[标识] [订阅区] [路由区]；显示名取订阅名，订阅数据缺失时也不跳变
    const v = subByTier[tier.name];
    const subHTML = v ? _subZoneHTML(v) : '';
    return `<div class="vendor-item has-sub" data-vendor="${safeName}">
      <div class="vendor-info">
        ${handle}
        <div class="vendor-avatar">${meta.initial}</div>
        <span class="vendor-name">${escapeHtml(meta.label)}</span>
      </div>
      ${subHTML ? `<div class="vendor-badges sub-zone">${subHTML}</div>` : ''}
      <div class="vendor-badges tier-zone">${routeHTML}</div>
    </div>`;
  }).join('');
  tierList.innerHTML = (tiers.length ? '' : '<div class="empty"><div class="empty-icon">🔌</div>无供应商数据</div>') + tierRows;

  // 无 tier 的订阅行（如仅走 IDE gateway 透传的 Antigravity）：不可拖拽，没有路由区也就不画分隔线
  subList.innerHTML = vendors.filter(function(v) {
    return !(v && SUB_META[v.key] && hasTier[SUB_META[v.key].tier]);
  }).map(function(v) {
    const meta = SUB_META[v.key] || { label: v.key, initial: '?' };
    const subHTML = _subZoneHTML(v);
    return `<div class="vendor-item has-sub">
      <div class="vendor-info">
        <div class="vendor-avatar">${meta.initial}</div>
        <span class="vendor-name">${escapeHtml(meta.label)}</span>
      </div>
      ${subHTML ? `<div class="vendor-badges sub-zone">${subHTML}</div>` : ''}
      <span class="zone-note sub-only" title="该订阅对应的供应商未启用或未加入路由链（tiers）">未加入路由链</span>
    </div>`;
  }).join('') + (vendors.length ? '' : '<div class="zone-note list-note">无订阅数据</div>');

  _restoreZoneScroll(tierList, tierScroll);
  _restoreZoneScroll(subList, subScroll);
  if (focusedVendor) _focusTierHandle(tierList, focusedVendor);
}

// 分区横向滚动位置的键：行名 + 区（订阅区 / 路由区）。订阅行没有 data-vendor，统一取行名
function _zoneScrollKey(zone) {
  const item = zone.closest('.vendor-item');
  const name = item.dataset.vendor || item.querySelector('.vendor-name').textContent;
  return name + '|' + (zone.classList.contains('tier-zone') ? 't' : 's');
}
function _zoneScroll(listEl) {
  const left = new Map();
  Array.from(listEl.querySelectorAll('.vendor-badges')).forEach(function(zone) {
    if (zone.scrollLeft) left.set(_zoneScrollKey(zone), zone.scrollLeft);
  });
  return left;
}
function _restoreZoneScroll(listEl, left) {
  if (!left.size) return;
  Array.from(listEl.querySelectorAll('.vendor-badges')).forEach(function(zone) {
    const pos = left.get(_zoneScrollKey(zone));
    if (pos) zone.scrollLeft = pos;
  });
}

// 焦点所在的供应商行（仅当焦点在该行的手柄上）
function _focusedTierHandle(listEl) {
  const el = document.activeElement;
  const handle = el && el.closest ? el.closest('.drag-handle') : null;
  const item = handle ? handle.closest('.vendor-item') : null;
  return item ? item.dataset.vendor : null;
}

function _tierRowByName(listEl, name) {
  return Array.from(listEl.querySelectorAll('.vendor-item[data-vendor]'))
    .find(function(el) { return el.dataset.vendor === name; }) || null;
}

function _focusTierHandle(listEl, vendor) {
  const item = _tierRowByName(listEl, vendor);
  const handle = item ? item.querySelector('.drag-handle') : null;
  if (handle) handle.focus({ preventScroll: true });
}

// ── 订阅状态：拖拽调整优先级（Pointer Events 重排，运行时生效，不重置配额） ────
// 采用 Pointer Events 而非原生 HTML5 DnD：后者触屏不支持、跨浏览器易「拿不起来」，
// 是业界公认脆弱的重排序方案（故 SortableJS / dnd-kit 等均改用指针事件）。
const _tierDrag = {
  active: false,     // 已越过阈值、进入拖拽
  srcName: null,     // 被拖拽 vendor 名
  srcEl: null,       // 被拖拽行
  pointerId: null,   // 捕获的指针 ID
  startY: 0,         // pointerdown 起点 Y
  origOrder: null,   // 拖拽开始时的顺序快照（无变化则跳过 PUT）
  inFlight: false,   // PUT 未决
};
const _TIER_DRAG_THRESHOLD = 4; // px，越过才判定为拖拽（否则视作点击）

function _tierCurrentOrder(listEl) {
  return Array.from(listEl.querySelectorAll('.vendor-item'))
    .map(function(el) { return el.dataset.vendor; })
    .filter(Boolean);
}

// 依据指针 Y 与各兄弟行中点比较，即时(乐观)重排 DOM
function _tierDragMoveTo(listEl, clientY) {
  const dragged = _tierDrag.srcEl;
  if (!dragged) return;
  const siblings = Array.from(listEl.querySelectorAll('.vendor-item'))
    .filter(function(el) { return el !== dragged; });
  for (var i = 0; i < siblings.length; i++) {
    const rect = siblings[i].getBoundingClientRect();
    if (clientY < rect.top + rect.height / 2) {
      if (dragged !== siblings[i] && dragged.nextElementSibling !== siblings[i]) {
        listEl.insertBefore(dragged, siblings[i]);
      }
      return;
    }
  }
  if (listEl.lastElementChild !== dragged) listEl.appendChild(dragged); // 落到末尾
}

function _tierDragReset() {
  if (_tierDrag.srcEl) {
    if (_tierDrag.pointerId != null) {
      try { _tierDrag.srcEl.releasePointerCapture(_tierDrag.pointerId); } catch (_) {}
    }
    _tierDrag.srcEl.classList.remove('dragging');
  }
  _tierDrag.active = false;
  _tierDrag.srcName = null;
  _tierDrag.srcEl = null;
  _tierDrag.pointerId = null;
  _tierDrag.startY = 0;
  _tierDrag.origOrder = null;
}

function initTierDrag() {
  // 指针事件委托绑定在静态容器 #vendor-list 上一次；子节点重渲染后仍生效
  const list = document.getElementById('vendor-list');
  if (!list || list.dataset.dndBound === '1') return;
  list.dataset.dndBound = '1';

  list.addEventListener('pointerdown', function(e) {
    if (e.button != null && e.button !== 0) return;            // 仅主指针/左键
    const handle = e.target && e.target.closest && e.target.closest('.drag-handle');
    if (!handle) return;                                       // 仅从手柄发起，避免误触与文本选择
    const item = handle.closest('.vendor-item');
    if (!item || !item.dataset.vendor) return;
    if (_tierDrag.srcEl) return;                               // 已有指针在拖：忽略第二个指针（触屏多指）
    if (list.querySelectorAll('.vendor-item').length < 2) return;
    _tierDrag.srcEl = item;
    _tierDrag.srcName = item.dataset.vendor;
    _tierDrag.pointerId = e.pointerId;
    _tierDrag.startY = e.clientY;
    try { item.setPointerCapture(e.pointerId); } catch (_) {}  // 捕获后指针移出列表仍持续收到 pointermove
    e.preventDefault();                                        // 阻止文本选择
  });

  // 键盘调整优先级：手柄聚焦时 ↑ 上移、↓ 下移，与拖拽一行等价
  list.addEventListener('keydown', function(e) {
    if (e.key !== 'ArrowUp' && e.key !== 'ArrowDown') return;
    const handle = e.target && e.target.closest && e.target.closest('.drag-handle');
    if (!handle) return;
    e.preventDefault();                                        // 方向键默认滚动页面
    if (_tierDrag.inFlight || _tierDrag.srcEl) return;         // 上一次提交未决，或指针正按在手柄上
    _tierMove(handle.closest('.vendor-item').dataset.vendor, e.key === 'ArrowUp' ? -1 : 1);
  });

  list.addEventListener('pointermove', function(e) {
    if (!_tierDrag.srcEl || e.pointerId !== _tierDrag.pointerId) return;
    if (!_tierDrag.srcEl.isConnected) {
      // 按住未移动期间行被重绘（如上一次重排后的刷新）：原节点已脱离，按名字重新定位后再继续
      const item = _tierRowByName(list, _tierDrag.srcName);
      if (!item) { _tierDragReset(); return; }
      _tierDrag.srcEl = item;
    }
    if (!_tierDrag.active) {
      if (Math.abs(e.clientY - _tierDrag.startY) < _TIER_DRAG_THRESHOLD) return;
      _tierDrag.active = true;                                 // 越阈值 → 正式进入拖拽
      _tierDrag.origOrder = _tierCurrentOrder(list);
      _tierDrag.srcEl.classList.add('dragging');
    }
    e.preventDefault();
    _tierDragMoveTo(list, e.clientY);
    // 行被 DOM 重排后浏览器会释放指针捕获，每次重排后补捕获
    try { _tierDrag.srcEl.setPointerCapture(e.pointerId); } catch (_) {}
  });

  function _tierDragFinish(commit) {
    if (!_tierDrag.srcEl) return;
    const wasActive = _tierDrag.active;
    const order = _tierCurrentOrder(list);
    const orig = _tierDrag.origOrder;
    _tierDragReset();
    if (!wasActive) return;                                    // 仅点击手柄未拖动 → 空操作
    if (!commit) { _tierRevertToList(); return; }              // 取消 → 回滚服务端真实顺序
    // 顺序未变 → 跳过 PUT，补绘拖拽期间被守卫跳过的刷新
    if (orig && order.join(',') === orig.join(',')) { _renderStatusRows(); return; }
    persistTierOrder(order);                                   // 提交新顺序（PUT + 失败回滚）
  }

  // 收尾监听挂 document：指针在列表外松开时事件不经过 #vendor-list
  document.addEventListener('pointerup', function(e) {
    if (!_tierDrag.srcEl || e.pointerId !== _tierDrag.pointerId) return;
    _tierDragFinish(true);
  });
  document.addEventListener('pointercancel', function(e) {
    if (!_tierDrag.srcEl || e.pointerId !== _tierDrag.pointerId) return;
    _tierDragFinish(false);
  });
}

// 键盘移动：乐观重排并立即重绘（焦点回到同一行），再走与拖拽相同的 PUT 与失败回滚
function _tierMove(vendor, delta) {
  const tiers = ((_tierStatus && _tierStatus.tiers) || []).slice();
  const from = tiers.findIndex(function(tier) { return tier.name === vendor; });
  const to = from + delta;
  if (from < 0 || to < 0 || to >= tiers.length) return;      // 已在两端
  [tiers[from], tiers[to]] = [tiers[to], tiers[from]];
  _tierStatus.tiers = tiers;
  _renderStatusRows();
  persistTierOrder(tiers.map(function(tier) { return tier.name; }));
}

// 服务端已确认新顺序：先同步缓存，刷新落地前的重绘（刷新失败、或拖拽进行中被守卫跳过）才不会画回旧序
function _syncTierOrder(names) {
  const tiers = (_tierStatus && _tierStatus.tiers) || [];
  const byName = {};
  tiers.forEach(function(tier) { byName[tier.name] = tier; });
  const next = names.map(function(name) { return byName[name]; });
  if (next.every(Boolean)) _tierStatus.tiers = next;
}

// 行序代次：每次 PUT 成功加一。请求发出后才被服务端确认的重排，其状态快照落地时行序已过期
let _tierOrderGen = 0;
let _tierConfirmedOrder = null;

// 拉取路由状态。请求发出后若有新的重排被确认（连按方向键时两次 PUT 只隔几十毫秒），
// 返回的快照只采用状态字段，行序以已确认的顺序为准，避免迟到的旧快照把行序画回上一步
async function fetchTierStatus() {
  const gen = _tierOrderGen;
  const status = await fetchJSON('/api/status');
  const tiers = (status && status.tiers) || [];
  if (gen !== _tierOrderGen && _tierConfirmedOrder) {
    const byName = {};
    tiers.forEach(function(tier) { byName[tier.name] = tier; });
    const next = _tierConfirmedOrder.map(function(name) { return byName[name]; });
    if (next.length === tiers.length && next.every(Boolean)) status.tiers = next;
  }
  return status;
}

function persistTierOrder(names) {
  if (_tierDrag.inFlight) return;          // 防并发：上一次未决则忽略
  if (!Array.isArray(names) || names.length < 2) return;
  _tierDrag.inFlight = true;
  fetch('/api/tier-order', {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ vendors: names })
  }).then(function(res) {
    _tierDrag.inFlight = false;            // 先清守卫，允许随后的 refreshOverview 重渲染
    if (res.ok) {
      // 服务端已更新顺序；刷新以同步请求趋势图等的排序。拖拽进行中也照常刷新，
      // 状态行的重绘由 _renderStatusRows 的守卫延后到拖拽结束
      _tierConfirmedOrder = names;
      _tierOrderGen++;
      _syncTierOrder(names);
      refreshOverview().catch(function() {});
    } else {
      console.error('tier-order rejected:', res.status);
      _tierRevertToList();
    }
  }).catch(function(e) {
    _tierDrag.inFlight = false;
    console.error('tier-order failed:', e);
    _tierRevertToList();
  });
}

function _tierRevertToList() {
  // 回滚到服务端真实顺序（PUT 失败 → 服务端顺序未变）
  fetchTierStatus().then(function(status) {
    updateVendorStatus(status);
  }).catch(function() {});
}

// ── 订阅状态：一键复位（熔断 / 限流 / 配额守卫状态；不动额度用量与优先级）────
var _vendorResetInFlight = false;
function resetVendorStatus(btn) {
  if (_vendorResetInFlight) return;                  // 防并发，同 persistTierOrder 范式
  _vendorResetInFlight = true;
  var label = btn.textContent;
  btn.disabled = true;
  btn.textContent = '复位中…';
  // 无 body → 服务端跳过重排序，仅对全部 tier 复位弹性设施状态
  fetch('/api/reset', { method: 'POST' }).then(function(res) {
    if (!res.ok) throw new Error(res.status);
    btn.textContent = '✓ 已复位';
    // 复位已生效，状态刷新失败不应误报失败、诱导重复点击 → 单独吞掉
    return fetchTierStatus().then(function(status) {
      updateVendorStatus(status);                    // 定向重渲染，不等 10 分钟轮询
    }).catch(function() {});
  }).catch(function(e) {
    console.error('vendor reset failed:', e);
    btn.textContent = '✗ 失败';
  }).finally(function() {
    setTimeout(function() {
      btn.textContent = label;
      btn.disabled = false;
      _vendorResetInFlight = false;
    }, 1500);
  });
}

// ── Model Calling 实时状态：对齐成列的 meter list ──────────
// 行节点按 vendor/model 键复用，只改文字与条宽，不整体重建 DOM：轮询不闪，编辑并行度时也不打断输入
const _mcRows = new Map();   // key → 行节点引用
var _mcEditing = false;      // 正在编辑并行度

function _mcRowKey(vendor, model) { return vendor + '/' + model; }

function _mcEl(tag, cls, text) {
  const node = document.createElement(tag);
  if (cls) node.className = cls;
  if (text != null) node.textContent = text;
  return node;
}

function _mcBuildRow(m) {
  const row = _mcEl('div', 'mc-row');
  row.setAttribute('role', 'listitem');
  row.dataset.key = _mcRowKey(m.vendor, m.model);
  const vendor = _mcEl('span', 'mc-vendor', m.vendor);
  vendor.title = m.vendor;
  const model = _mcEl('span', 'mc-model-name', m.model);
  model.title = _mcRowKey(m.vendor, m.model);
  const meter = _mcEl('div', 'mc-meter');
  meter.setAttribute('aria-hidden', 'true');
  const fill = _mcEl('i', 'mc-fill');
  fill.style.width = '0%';
  meter.appendChild(fill);
  const meterNote = _mcEl('span', 'mc-meter-note');
  meterNote.appendChild(_mcEl('span', 'mc-note-wide', '仅监控 · 不限并行度'));
  meterNote.appendChild(_mcEl('span', 'mc-note-narrow', '仅监控'));
  // 上限是一个真按钮：键盘可达（Tab 聚焦、Enter / 空格触发），点击后原地变成数字输入
  const limit = _mcEl('button', 'mc-limit-editable');
  limit.type = 'button';
  limit.title = '点击修改并行度';
  limit.appendChild(_mcEl('span', 'mc-limit-val'));
  limit.insertAdjacentHTML('beforeend', '<svg class="mc-pencil" viewBox="0 0 16 16" width="12" height="12" aria-hidden="true" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><path d="M11.5 2.5a1.3 1.3 0 0 1 1.8 1.8l-7.2 7.2-2.5.7.7-2.5z"/></svg>');
  const use = _mcEl('span', 'mc-use');
  const inUse = _mcEl('b', 'mc-in', '0');
  const sep = _mcEl('span', 'mc-sep', '/');
  const unit = _mcEl('span', 'mc-use-unit', '进行中');
  use.append(inUse, sep, limit, unit);
  const queue = _mcEl('span', 'mc-queue');
  row.append(vendor, model, meter, meterNote, use, queue);
  return { row, vendor, model, meter, fill, meterNote, use, inUse, sep, limit, unit, queue, m: null };
}

function _mcUpdateRow(r, m) {
  r.m = m;   // 行缓存最近一次载荷：并行度改成功后用它就地重算，不等下一轮轮询
  const monitor = m.mode === 'monitor';
  r.row.classList.toggle('mc-monitor', monitor);
  r.vendor.textContent = m.vendor;
  r.model.textContent = m.model;
  r.model.title = _mcRowKey(m.vendor, m.model);
  if (monitor) {
    r.inUse.textContent = m.in_use;
    r.sep.style.display = 'none';
    r.limit.style.display = 'none';
    r.unit.style.display = '';
  } else {
    const limit = m.limit || 0;
    const pct = limit > 0 ? Math.round((m.in_use / limit) * 100) : 0;
    r.fill.style.width = Math.min(pct, 100) + '%';
    r.fill.className = 'mc-fill ' + (pct <= 50 ? 'mc-low' : (pct <= 80 ? 'mc-mid' : 'mc-high'));
    r.meter.title = '并行度 ' + m.in_use + ' / ' + limit;
    r.inUse.textContent = m.in_use;
    r.sep.style.display = '';
    r.limit.style.display = '';
    r.unit.style.display = 'none';
    r.limit.querySelector('.mc-limit-val').textContent = limit;
    r.limit.setAttribute('aria-label', m.vendor + '/' + m.model + ' 的并行度上限，当前 ' + limit + '，点击修改');
    r.limit.setAttribute('data-limit', limit);
    r.limit.setAttribute('data-tier', m.vendor);
    r.limit.setAttribute('data-model', m.model);
  }
  const peak = m.peak_pending_recent || 0;
  if (m.pending > 0) r.queue.replaceChildren(_mcEl('span', 'mc-badge mc-badge-pending', '排队 ' + m.pending));
  else if (peak > 0) r.queue.replaceChildren(_mcEl('span', 'mc-peak', '曾排队 ' + peak));
  else r.queue.replaceChildren();
}

function updateModelCalling(status) {
  const wrap = document.getElementById('model-calling-wrap');
  if (!wrap) return;
  // 正在编辑并行度时不碰 DOM：输入框、焦点与已输入的内容都原样保留，编辑结束后由下一轮轮询同步
  if (_mcEditing) return;
  const tiers = status.tiers || [];

  const models = [];
  for (const tier of tiers) {
    const conc = (tier.diagnostics || {}).concurrency;
    if (!conc) continue;
    for (const model of Object.keys(conc)) {
      const d = conc[model] || {};
      models.push({
        vendor: tier.name, model, mode: d.mode || 'limited', limit: d.limit,
        in_use: d.in_use || 0, available: d.available, pending: d.pending || 0,
        peak_pending_recent: d.peak_pending_recent || 0,
      });
    }
  }

  if (!models.length) {
    wrap.replaceChildren(_mcEl('div', 'mc-empty', '无活跃模型调用'));
    _mcRows.clear();
    return;
  }
  let list = wrap.querySelector('.mc-list');
  if (!list) {
    wrap.replaceChildren();
    list = _mcEl('div', 'mc-list');
    list.setAttribute('role', 'list');
    wrap.appendChild(list);
  }
  const seen = new Set();
  models.forEach(function (m, idx) {
    const key = _mcRowKey(m.vendor, m.model);
    seen.add(key);
    let r = _mcRows.get(key);
    if (!r || !r.row.parentNode) {
      r = _mcBuildRow(m);
      _mcRows.set(key, r);
    }
    if (list.children[idx] !== r.row) list.insertBefore(r.row, list.children[idx] || null);
    _mcUpdateRow(r, m);
  });
  for (const [key, r] of _mcRows) {
    if (!seen.has(key)) { r.row.remove(); _mcRows.delete(key); }
  }
  list.classList.toggle('mc-cols-2', models.length >= 3);
}

// Model Calling 独立短间隔轮询
var _mcTimer = null;
function startModelCallingPoll() {
  stopModelCallingPoll();
  function tick() {
    fetchJSON('/api/status').then(function(status) {
      updateModelCalling(status);
    }).catch(function() {});
  }
  tick();
  _mcTimer = setInterval(tick, 10000);
}
function stopModelCallingPoll() {
  if (_mcTimer) { clearInterval(_mcTimer); _mcTimer = null; }
}

// ── 并行度运行时编辑 ──────────────────────────────────────
// 输入只接受 1 到 20 的整数：7.5、1e2 这类输入按非法处理，不截断后提交
function limitValue(raw) {
  const text = String(raw == null ? '' : raw).trim();
  if (!/^\\d{1,2}$/.test(text)) return NaN;
  const value = Number(text);
  return value >= 1 && value <= 20 ? value : NaN;
}

// 捕获阶段接管上限按钮的点击。编辑期间 _mcEditing 为真，轮询不重绘；提交带在途守卫与 8 秒超时，
// 请求悬住时也能解除编辑态；结束编辑后焦点回到按钮；结果用静态色标并写进 role=status 节点供读屏播报
document.addEventListener('click', function (e) {
  const btn = e.target.closest('.mc-limit-editable');
  if (!btn || _mcEditing) return;
  e.preventDefault();
  startLimitEdit(btn);
}, true);

function _mcAnnounce(text) {
  let live = document.getElementById('mc-live');
  if (!live) {
    live = _mcEl('div', 'sr-only');
    live.id = 'mc-live';
    live.setAttribute('role', 'status');
    live.setAttribute('aria-live', 'polite');
    document.getElementById('model-calling-card').appendChild(live);
  }
  live.textContent = '';
  setTimeout(function () { live.textContent = text; }, 30);
}

function startLimitEdit(btn) {
  _mcEditing = true;
  const oldVal = btn.getAttribute('data-limit');
  const tier = btn.getAttribute('data-tier');
  const model = btn.getAttribute('data-model');
  const input = document.createElement('input');
  input.type = 'text';
  input.inputMode = 'numeric';
  input.className = 'mc-limit-input';
  input.setAttribute('aria-label', tier + '/' + model + ' 的并行度上限（1 到 20 的整数）');
  input.value = oldVal;
  btn.style.display = 'none';
  btn.parentNode.insertBefore(input, btn.nextSibling);
  input.focus();
  input.select();

  let done = false;
  let inFlight = false;   // PUT 未决期间不再接受第二次提交
  const restore = function () {
    if (done) return;
    const hadFocus = document.activeElement === input;
    done = true;            // 先落标记再移除输入框：移除引发的 blur 不会再提交
    _mcEditing = false;
    input.remove();
    btn.style.display = '';
    if (hadFocus) btn.focus();   // 回车 / Esc 结束后焦点回到上限按钮，键盘用户不用从头 Tab
  };
  let markTimer = null;
  const mark = function (cls) {
    btn.classList.remove('mc-limit-ok', 'mc-limit-err');
    clearTimeout(markTimer);
    btn.classList.add(cls);
    markTimer = setTimeout(function () { btn.classList.remove(cls); }, 1500);
  };
  const submit = function () {
    if (done || inFlight) return;
    const value = limitValue(input.value);
    if (isNaN(value)) { restore(); mark('mc-limit-err'); _mcAnnounce('并行度须是 1 到 20 的整数'); return; }
    if (String(value) === oldVal) { restore(); return; }
    inFlight = true;
    // 8 秒兜底：请求悬住（网络挂起、fetch 不响应 abort）时也解除编辑态，不让整张表停在「编辑中」
    const controller = typeof AbortController === 'function' ? new AbortController() : null;
    let timeoutId = null;
    const timeout = new Promise(function (_, reject) {
      timeoutId = setTimeout(function () { reject(new Error('timeout')); if (controller) controller.abort(); }, 8000);
    });
    const request = fetch('/api/concurrency', {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ tier: tier, model: model, limit: value }),
      signal: controller ? controller.signal : undefined,
    });
    Promise.race([request, timeout]).then(function (res) {
      if (!res.ok) throw new Error(String(res.status));
      btn.querySelector('.mc-limit-val').textContent = value;
      btn.setAttribute('data-limit', value);
      // 就地重算这一行（条形、可访问名称立刻自洽），再拉一次状态对齐服务端
      const row = _mcRows.get(_mcRowKey(tier, model));
      if (row && row.m && row.m.mode !== 'monitor') { row.m.limit = value; _mcUpdateRow(row, row.m); }
      mark('mc-limit-ok');
      _mcAnnounce(tier + '/' + model + ' 的并行度已更新为 ' + value);
      fetchJSON('/api/status').then(function (status) { updateModelCalling(status); }).catch(function () {});
    }).catch(function () {
      mark('mc-limit-err');
      _mcAnnounce(tier + '/' + model + ' 的并行度更新失败');
    }).finally(function () {
      clearTimeout(timeoutId);
      inFlight = false;
      restore();
    });
  };
  input.addEventListener('keydown', function (ev) {
    if (ev.key === 'Escape') { restore(); return; }
    if (ev.key !== 'Enter') return;
    ev.preventDefault();
    submit();
  });
  input.addEventListener('blur', function () {
    setTimeout(function () { if (!done) submit(); }, 50);
  });
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
  const ctx = chartContext('chart-vendor-dist');
  if (!labels.length) {
    chartEmpty(ctx);
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
  const ctx = chartContext('chart-token-timeline');
  if (!dates.length) {
    chartEmpty(ctx);
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
  const ctx = chartContext('chart-model-token-timeline');

  // 清理 HTML Legend 容器
  const legendEl = document.getElementById('model-token-legend');
  if (legendEl) legendEl.innerHTML = '';

  if (!dates.length || !keys.length) {
    if (legendEl) legendEl.style.display = 'none';
    chartEmpty(ctx);
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

// 区间标签：KPI 副行、趋势页签说明、分布图标题共用同一口径。自选区间且截止日是今天写「近 N 天」；
// 看历史窗口写起止日期（跨年写两位年份，完整日期在区间栏的日期输入框里），不能也叫「近 N 天」
function rangeLabel() {
  const custom = document.getElementById('range-custom');
  if (custom && custom.classList.contains('visible')) {
    const start = document.getElementById('range-start');
    const end = document.getElementById('range-end');
    const today = new Date().toISOString().slice(0, 10);
    if (start && start.value && end && end.value && end.value !== today) {
      const sameYear = start.value.slice(0, 4) === end.value.slice(0, 4);
      return start.value.slice(sameYear ? 5 : 2) + ' ~ ' + end.value.slice(sameYear ? 5 : 2);
    }
  }
  return '近 ' + currentDays + ' 天';
}

function setTimeRange(days, btn) {
  currentDays = days;
  // 仅限时间区间栏：Config 页的 section pills 复用 .range-btn
  document.querySelectorAll('.time-range-bar .range-btn').forEach(b => b.classList.remove('active'));
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
  if (list.length > max) {
    var fullList = list.map(function(c) { return c.trim(); }).join(', ');
    html += '<span class="session-tag" title="' + escapeHtml(fullList) + '">+' + (list.length - max) + '</span>';
  }
  return html;
}
function categoryLabel(t) { return t === 'cc' ? 'Claude Code' : (t === 'api' ? 'API' : (t === 'antigravity' ? 'Antigravity' : t)); }
function formatCategories(cats) {
  if (!cats) return '–';
  return cats.split(',').map(function(c) {
    return '<span class="session-tag">' + escapeHtml(categoryLabel(c.trim())) + '</span>';
  }).join('');
}
function formatVendorTags(vendors) {
  if (!vendors) return '–';
  var list = vendors.split(',');
  var max = 4;
  var html = list.slice(0, max).map(function(v) {
    var vt = v.trim();
    var name = vendorShortName(vt);
    var fullLabel = formatVendorLabel(vt);
    var cls = isApiVendor(vt) ? 'session-tag' : 'session-tag session-tag-cc';
    return '<span class="' + cls + '" title="' + escapeHtml(fullLabel) + '">' + escapeHtml(name) + '</span>';
  }).join('');
  if (list.length > max) {
    var fullList = list.map(function(v) { return formatVendorLabel(v.trim()); }).join(', ');
    html += '<span class="session-tag" title="' + escapeHtml(fullList) + '">+' + (list.length - max) + '</span>';
  }
  return html;
}
// ── Sessions Pagination State ──
var allSessions = [];
var sessionPage = 0;
var sessionPageSize = 30;
var sessionBindMap = {};
var sessionBindableModels = [];

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
    var statusData = results[2].status === 'fulfilled' ? results[2].value : {tiers: [], bindable_models: []};
    allSessions = data.sessions || [];
    sessionBindMap = {};
    (bindData.bindings || []).forEach(function(b) { sessionBindMap[b.session_key] = b; });
    sessionBindableModels = statusData.bindable_models || [];
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
    tbody.innerHTML = '<tr><td colspan="11" class="empty"><div class="empty-icon">📭</div>No session data</td></tr>';
  } else {
    tbody.innerHTML = page.map(function(s) {
      var parsed = parseSessionKey(s.session_key);
      var binding = sessionBindMap[s.session_key];
      var selectHtml = buildModelSelect(s.session_key, binding, sessionBindableModels);
      var modelsFull = (s.models || '').split(',').map(function(c){return c.trim();});
      var vendorsFull = (s.vendors || '').split(',').map(function(v){return formatVendorLabel(v.trim());});
      var sr = s.success_rate != null ? Math.round(s.success_rate) : null;
      var sessionTitle = s.title || '';
      var clientsFull = (s.client_categories || '').split(',').filter(Boolean).map(function(c){return categoryLabel(c.trim());});
      return '<tr data-row onclick="toggleRow(this)">' +
        '<td class="session-key" onclick="event.stopPropagation()">' +
          '<div class="session-id" data-key="' + escapeHtml(s.session_key) + '" title="' + escapeHtml(s.session_key) + '">' +
            '<span class="session-id-text">' + escapeHtml(parsed.session_id || s.session_key) + '</span>' +
            '<button class="copy-btn" onclick="copyFromParent(this)" title="Copy Session ID">⧉</button>' +
          '</div>' +
          '<div class="session-meta" title="device: ' + escapeHtml(parsed.device_id) + ' | account: ' + escapeHtml(parsed.account_uuid) + '">' +
            'dev:' + escapeHtml(shortId(parsed.device_id, 8)) + ' · acct:' + escapeHtml(shortId(parsed.account_uuid, 8)) +
          '</div>' +
        '</td>' +
        '<td class="session-title" title="' + escapeHtml(sessionTitle) + '">' + (sessionTitle ? escapeHtml(sessionTitle) : '–') + '</td>' +
        '<td>' + relativeTime(s.last_active_ts) + '</td>' +
        '<td style="font-family:JetBrains Mono,monospace">' + fmtNum(s.total_requests) + '</td>' +
        '<td style="font-family:JetBrains Mono,monospace">' + fmtTokens(s.total_tokens) + '</td>' +
        '<td title="' + escapeHtml(modelsFull.join(', ')) + '">' + formatSessionTags(s.models, 3) + '</td>' +
        '<td title="' + escapeHtml(vendorsFull.join(', ')) + '">' + formatVendorTags(s.vendors) + '</td>' +
        '<td style="font-family:JetBrains Mono,monospace">' + fmtDuration(s.avg_duration_ms) + '</td>' +
        '<td class="cell-success">' + successBarHtml(s.success_rate) + '</td>' +
        '<td class="cell-bind" onclick="event.stopPropagation()">' + selectHtml + '</td>' +
        '<td title="' + escapeHtml(clientsFull.join(', ')) + '">' + formatCategories(s.client_categories) + '</td>' +
        '</tr>' +
        '<tr class="row-detail"><td colspan="11"><div class="detail-card">' +
          '<div class="detail-title-row">' +
            '<div class="detail-item"><div class="detail-label">Title</div><div class="detail-value">' + (sessionTitle ? escapeHtml(sessionTitle) : '–') + '</div></div>' +
          '</div>' +
          '<div class="detail-identity-row">' +
            '<div class="detail-item"><div class="detail-label">Session ID</div><div class="detail-value" title="' + escapeHtml(s.session_key) + '">' + escapeHtml(parsed.session_id || s.session_key) + '</div></div>' +
            '<div class="detail-item"><div class="detail-label">Device</div><div class="detail-value" title="' + escapeHtml(parsed.device_id || '') + '">' + (parsed.device_id ? escapeHtml(parsed.device_id) : '–') + '</div></div>' +
            '<div class="detail-item"><div class="detail-label">Account</div><div class="detail-value" title="' + escapeHtml(parsed.account_uuid || '') + '">' + (parsed.account_uuid ? escapeHtml(parsed.account_uuid) : '–') + '</div></div>' +
          '</div>' +
          '<div class="detail-metrics-grid">' +
            '<div class="detail-item"><div class="detail-label">Last Active</div><div class="detail-value">' + relativeTime(s.last_active_ts) + '</div></div>' +
            '<div class="detail-item"><div class="detail-label">Requests</div><div class="detail-value">' + fmtNum(s.total_requests) + '</div></div>' +
            '<div class="detail-item"><div class="detail-label">Tokens</div><div class="detail-value">' + fmtTokens(s.total_tokens) + '</div></div>' +
            '<div class="detail-item"><div class="detail-label">Models</div><div class="detail-value">' + (modelsFull.length ? modelsFull.map(function(m){return '<span class="session-tag">' + escapeHtml(m) + '</span>';}).join(' ') : '–') + '</div></div>' +
            '<div class="detail-item"><div class="detail-label">Vendors</div><div class="detail-value">' + (vendorsFull.length ? vendorsFull.map(function(v){return '<span class="session-tag">' + escapeHtml(v) + '</span>';}).join(' ') : '–') + '</div></div>' +
            '<div class="detail-item"><div class="detail-label">Avg Latency</div><div class="detail-value">' + fmtDuration(s.avg_duration_ms) + '</div></div>' +
            '<div class="detail-item" style="grid-column:span 2"><div class="detail-inline-pair">' +
              '<div><div class="detail-label">Success Rate</div><div class="detail-value">' + (sr != null ? sr + '%' : '–') + '</div></div>' +
              '<div><div class="detail-label">Client</div><div class="detail-value">' + escapeHtml(s.client_categories || '–') + '</div></div>' +
            '</div></div>' +
          '</div>' +
        '</div></td></tr>';
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

function buildModelSelect(sessionKey, binding, bindableModels) {
  var boundModel = binding && binding.model ? binding.model : '';
  // vendor-only 绑定（CLI --vendor / API vendors-only 建立）在 Model 列无模型可显，
  // 通过 Default 选项标注实际钉住的 vendor，避免与未绑定态不可区分
  var vendorOnly = binding && !binding.model && binding.vendors && binding.vendors.length
    ? binding.vendors.join(', ') : '';
  var defaultLabel = vendorOnly ? 'Default (vendor: ' + vendorOnly + ')' : 'Default';
  var html = '<select class="bind-select" data-session-key="' + escapeHtml(sessionKey) + '" title="Session-level model override — applies from the next request; in-flight requests keep the old model">';
  html += '<option value=""' + (!boundModel ? ' selected' : '') + '>' + escapeHtml(defaultLabel) + '</option>';
  // 按 vendor 分组渲染（内置目录条目带 vendor 字段；custom/无 vendor 条目落 Custom 组）
  var groups = {};
  var groupOrder = [];
  bindableModels.forEach(function(m) {
    var g = m.vendor || 'custom';
    if (!groups[g]) { groups[g] = []; groupOrder.push(g); }
    groups[g].push(m);
  });
  groupOrder.forEach(function(g) {
    if (groupOrder.length > 1) {
      html += '<optgroup label="' + escapeHtml(g.charAt(0).toUpperCase() + g.slice(1)) + '">';
    }
    groups[g].forEach(function(m) {
      var v = m.model || '';
      html += '<option value="' + escapeHtml(v) + '"' + (v === boundModel ? ' selected' : '') + '>' + escapeHtml(m.label || v) + '</option>';
    });
    if (groupOrder.length > 1) {
      html += '</optgroup>';
    }
  });
  if (boundModel && !bindableModels.some(function(m) { return m.model === boundModel; })) {
    html += '<option value="' + escapeHtml(boundModel) + '" selected>' + escapeHtml(boundModel) + ' (off-list)</option>';
  }
  html += '</select>';
  return html;
}

async function handleBindChange(sel) {
  var sessionKey = sel.getAttribute('data-session-key');
  var model = sel.value;
  var previousValue = sel.getAttribute('data-previous') || '';
  try {
    var resp;
    if (model) {
      resp = await fetch('/api/session-vendor', {
        method: 'PUT',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({session_key: sessionKey, model: model}),
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

async function refreshOverview() {
  const days = currentDays > 0 ? currentDays : 7;
  const [summary, timeline, status, subs] = await Promise.all([
    fetchJSON('/api/dashboard/summary?days=' + days),
    fetchJSON('/api/dashboard/timeline?days=' + days),
    fetchTierStatus(),
    // 订阅状态面板失败不拖垮整体刷新（服务端已隔离单家错误，这里兜底网络层）
    fetchJSON('/api/dashboard/subscriptions').catch(function () { return { vendors: [] }; }),
  ]);

  if (summary.version) {
    document.getElementById('version-badge').textContent = 'v' + summary.version;
  }

  updateKPI(summary);
  // 行数据与随后的订阅数据到齐后只重绘一次：连续替换行节点会让键盘焦点环闪烁
  updateVendorStatus(status, true);
  updateModelCalling(status);
  updateSubscriptions(subs);
  updateChartTitles(days);

  const rows = timeline.rows || [];
  const tierOrder = (status.tiers || []).map(t => t.name);
  if (!window.Chart) return;  // Chart.js 未加载：跳过图表，其余面板照常
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
        if (tab === 'config') {
          if (window.ConfigTab) await window.ConfigTab.refresh(false);
        } else if (tab === 'sessions') {
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
    url.searchParams.set('tab', name);
    // section 是 Config 页内部定位参数，离开该页时清除，避免残留进其它页签的 URL
    if (name !== 'config') url.searchParams.delete('section');
    if (url.href === window.location.href) return;
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
  if (TABS.indexOf(name) < 0) name = 'overview';
  if (name === currentTab) {
    syncTabUrl(name);
    return;
  }
  currentTab = name;
  applyTabState(name);
  syncTabUrl(name);
  // Model Calling 轮询随页签切换启停
  if (name === 'overview') { startModelCallingPoll(); } else { stopModelCallingPoll(); }
  // Config 页只在首次进入时加载；之后仅手动 ⟳ 或保存后重载，避免覆盖编辑中的表单
  if (name === 'config' && tabLoaded.config) return;
  refresh();
}

// ── 初始化 ────────────────────────────────────────────────
(function bootstrap() {
  let initial = 'overview';
  try {
    const t = new URL(window.location.href).searchParams.get('tab');
    if (TABS.indexOf(t) >= 0) initial = t;
  } catch (e) { /* no-op */ }
  currentTab = initial;
  applyTabState(initial);
  syncTabUrl(initial);
  // Load version immediately regardless of active tab
  fetchJSON('/api/dashboard/summary?days=7').then(function(s) {
    if (s && s.version) document.getElementById('version-badge').textContent = 'v' + s.version;
  }).catch(function(){});
  initTierDrag();                // 绑定 tier 行拖拽（事件委托，幂等）
  refresh();                     // 仅加载初始页签的数据
  setInterval(function () { if (currentTab !== 'config') refresh(); }, 600000);  // 每 10 分钟刷新当前页签（Config 除外）
  if (initial === 'overview') startModelCallingPoll();
})();
</script>
</body>
</html>
"""
for _key, _value in (
    ("{{PROMPT}}", _PROMPT_PATHS),
    ("{{LOGO_DEFS}}", _LOGO_DEFS),
    ("{{CHART_JS_URL}}", _CHART_JS_URL),
    ("{{CHART_JS_SRI}}", _CHART_JS_SRI),
    ("{{CONFIG_CSS}}", _asset_url("config/config.css")),
    ("{{CONFIG_JS}}", _asset_url("config/config.js")),
    ("{{CONFIG_SCHEMA_DEFAULTS}}", _schema_defaults_json()),
):
    _DASHBOARD_HTML = _DASHBOARD_HTML.replace(_key, _value)


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
    """注册 Dashboard 相关路由与 ``/static`` 静态资源（重复调用幂等）."""
    from .. import __version__

    if any(getattr(route, "path", None) == "/static" for route in app.routes):
        return
    app.mount("/static", _StaticAssets(directory=_STATIC_DIR), name="static")

    @app.get("/favicon.ico", include_in_schema=False)
    async def favicon() -> Response:
        """返回内嵌 favicon."""
        return Response(content=_FAVICON_ICO, media_type="image/x-icon")

    @app.get("/favicon.svg", include_in_schema=False)
    async def favicon_svg() -> Response:
        """返回 SVG favicon（现代浏览器优先）."""
        return Response(content=_FAVICON_SVG, media_type="image/svg+xml")

    @app.get("/dashboard", response_class=HTMLResponse, include_in_schema=False)
    async def dashboard() -> HTMLResponse:
        """返回 Dashboard HTML 页面.

        内联 JS/CSS 随版本变化，禁用缓存以免浏览器留存旧内联脚本掩盖前端修复。
        """
        return HTMLResponse(
            content=_DASHBOARD_HTML,
            headers={
                "Cache-Control": "no-cache",
                "Content-Security-Policy": _DASHBOARD_CSP,
            },
        )

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

    @app.get("/api/dashboard/subscriptions")
    async def dashboard_subscriptions(request: Request) -> Response:
        """返回三家订阅（Claude/GLM/Antigravity）档位与用量快照.

        主动查询带 TTL 缓存且单家失败隔离；服务未装配（异常配置）时
        返回空列表（200），保证 Overview 整体刷新不被该面板拖垮。
        """
        service = getattr(request.app.state, "subscription_status", None)
        if service is None:
            return Response(
                content=b'{"vendors":[]}',
                status_code=200,
                media_type="application/json",
            )
        try:
            result = await service.get_status()
        except Exception as exc:
            # I2 泄露纪律（对齐 executor._log_unexpected canon）：类型 + 堆栈帧，不落 str(exc)
            logger.error(
                "dashboard_subscriptions unexpected error: %s\n%s",
                type(exc).__name__,
                "".join(traceback.format_tb(exc.__traceback__)),
            )
            return Response(
                content=b'{"vendors":[]}',
                status_code=200,
                media_type="application/json",
            )
        return Response(
            content=json.dumps(result, ensure_ascii=False).encode(),
            status_code=200,
            media_type="application/json",
        )
