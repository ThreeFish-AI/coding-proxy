"""Dashboard Overview 图表空态：canvas 恒留在 DOM 中，无数据与有数据之间反复刷新不抛错（静态检查 + Node vm 行为）."""

from __future__ import annotations

import textwrap

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from coding.proxy.server.dashboard import register_dashboard_routes
from tests.test_dashboard_subscription_card import (
    _NODE,
    _extract_function,
    _inline_script,
    _run_node,
)

_CHARTS = (
    ("buildTimeline", "chart-timeline"),
    ("buildVendorDist", "chart-vendor-dist"),
    ("buildTokenTimeline", "chart-token-timeline"),
    ("buildModelTokenTimeline", "chart-model-token-timeline"),
)


@pytest.fixture
def client() -> TestClient:
    app = FastAPI()
    register_dashboard_routes(app)
    return TestClient(app, base_url="http://127.0.0.1:3392")


# 构建函数内的这些写法会把 canvas 移出 DOM，或绕开成对的空态入口
_CANVAS_REMOVAL_PATTERNS = (
    ".parentElement.innerHTML",
    ".parentNode.innerHTML",
    ".replaceChildren(",
    ".replaceWith(",
    "canvas.remove(",
)


def test_chart_builders_keep_canvas_in_dom(client: TestClient):
    """空态不得移除 canvas.

    旧实现用空态提示覆盖 canvas 父节点的内容（innerHTML），canvas 随之被移除，下一次刷新取不到元素抛
    TypeError，页面显示「刷新失败」。
    """
    script = _inline_script(client.get("/dashboard").text)
    for builder, canvas_id in _CHARTS:
        source = _extract_function(script, f"function {builder}(")
        assert f"chartContext('{canvas_id}')" in source
        assert "chartEmpty(ctx);" in source
        assert ".getContext('2d')" not in source
        for pattern in _CANVAS_REMOVAL_PATTERNS:
            assert pattern not in source, (builder, pattern)


def test_trend_charts_are_stacked_areas_with_empty_states(client: TestClient):
    """三张趋势图共用一个页签图位：折线填到上一条堆叠线，空数据时显示「暂无数据」而不是空坐标轴."""
    script = _inline_script(client.get("/dashboard").text)
    for builder in ("buildTimeline", "buildTokenTimeline", "buildModelTokenTimeline"):
        source = _extract_function(script, f"function {builder}(")
        assert "fill: i === 0 ? 'start' : '-1'," in source, builder
        assert "chartEmpty(ctx);" in source, builder
    # 空态提示只有文字：不依赖 emoji 图标（各平台渲染不一）
    assert "note.textContent = '暂无数据';" in _extract_function(
        script, "function chartEmpty("
    )
    # 销毁后随即清空实例引用：空态分支返回后，不能再对已销毁的实例调用 update
    for builder, instance in (
        ("buildTimeline", "chartTimeline"),
        ("buildTokenTimeline", "chartTokenTimeline"),
        ("buildModelTokenTimeline", "chartModelTokenTimeline"),
    ):
        source = _extract_function(script, f"function {builder}(")
        assert (
            f"if ({instance}) {{ {instance}.destroy(); {instance} = null; }}" in source
        ), builder


def test_chart_empty_state_class_pairs_with_selector(client: TestClient):
    """chartEmpty 打的类名必须能被 chartContext 的选择器命中，否则空态提示清不掉、逐轮残留."""
    script = _inline_script(client.get("/dashboard").text)
    assert "note.className = 'empty';" in _extract_function(
        script, "function chartEmpty("
    )
    assert "querySelector('.empty')" in _extract_function(
        script, "function chartContext("
    )


_CHART_HARNESS = textwrap.dedent(
    r"""
    // 最小假 DOM：canvas 恒在 wrap 内；空态提示是 canvas 的兄弟节点
    const makeCanvas = (id) => {
      const canvas = {
        id: id, style: { display: 'block' }, getContextCalls: 0,
        getContext() { canvas.getContextCalls += 1; return { canvas: canvas }; },
      };
      const notes = () => canvas.parentElement.children.filter(c => c !== canvas);
      canvas.parentElement = {
        children: [canvas], notes,
        // 按 class 匹配空态提示（与真实 querySelector 一致），不按位置取
        querySelector(sel) {
          return sel === '.empty'
            ? (notes().find(n => String(n.className).split(/\s+/).includes('empty')) || null)
            : null;
        },
        appendChild(node) { node.parentElement = canvas.parentElement; canvas.parentElement.children.push(node); },
      };
      return canvas;
    };
    const canvases = {};
    const document = {
      getElementById(id) { return canvases[id] || null; },
      createElement() {
        return {
          className: '', textContent: '',
          remove() { const p = this.parentElement; p.children.splice(p.children.indexOf(this), 1); },
        };
      },
    };
    for (const id of ['chart-timeline', 'chart-vendor-dist', 'chart-token-timeline', 'chart-model-token-timeline']) canvases[id] = makeCanvas(id);

    for (const id of Object.keys(canvases)) {
      const canvas = canvases[id];
      let ctx = chartContext(id);                              // 首次刷新：有数据
      assert.strictEqual(ctx.canvas, canvas);
      assert.strictEqual(canvas.style.display, '');
      chartEmpty(ctx);                                         // 数据清空：显示空态
      assert.strictEqual(canvas.style.display, 'none');
      assert.strictEqual(canvas.parentElement.notes().length, 1);
      assert.ok(canvas.parentElement.notes()[0].textContent.includes('暂无数据'));
      ctx = chartContext(id);                                  // 下一次刷新：空态清除、canvas 复原（旧实现在此抛 TypeError）
      assert.strictEqual(ctx.canvas, canvas);
      assert.strictEqual(canvas.style.display, '');
      assert.strictEqual(canvas.parentElement.notes().length, 0);
      chartEmpty(ctx);
      ctx = chartContext(id);
      assert.strictEqual(canvas.parentElement.notes().length, 0);
      assert.strictEqual(canvas.getContextCalls, 3);
    }
    console.log('chart empty-state harness OK');
    """
)


@pytest.mark.skipif(_NODE is None, reason="node 不可用")
def test_chart_empty_state_is_reversible(client: TestClient, tmp_path):
    """chartContext / chartEmpty 在最小假 DOM 上执行：空态可反复进出，canvas 始终可取到上下文."""
    script = _inline_script(client.get("/dashboard").text)
    _run_node(
        tmp_path,
        "chart_harness.js",
        "const assert = require('assert');\n"
        + _extract_function(script, "function chartContext(")
        + "\n"
        + _extract_function(script, "function chartEmpty(")
        + "\n"
        + _CHART_HARNESS,
        "chart empty-state harness OK",
    )
