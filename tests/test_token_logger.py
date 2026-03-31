"""TokenLogger 查询扩展单元测试."""

import pytest
import pytest_asyncio

from coding.proxy.logging.db import TokenLogger


@pytest_asyncio.fixture
async def logger(tmp_path):
    tl = TokenLogger(tmp_path / "test.db")
    await tl.init()
    yield tl
    await tl.close()


@pytest.mark.asyncio
async def test_query_window_total_empty(logger):
    total = await logger.query_window_total(5.0)
    assert total == 0


@pytest.mark.asyncio
async def test_query_window_total_sums_correctly(logger):
    await logger.log(
        backend="anthropic", model_requested="claude-sonnet-4",
        model_served="claude-sonnet-4",
        input_tokens=100, output_tokens=50,
        success=True,
    )
    await logger.log(
        backend="anthropic", model_requested="claude-sonnet-4",
        model_served="claude-sonnet-4",
        input_tokens=200, output_tokens=80,
        success=True,
    )
    total = await logger.query_window_total(5.0)
    assert total == 430  # (100+50) + (200+80)


@pytest.mark.asyncio
async def test_query_window_total_filters_backend(logger):
    await logger.log(
        backend="anthropic", model_requested="claude-sonnet-4",
        model_served="claude-sonnet-4",
        input_tokens=100, output_tokens=50,
        success=True,
    )
    await logger.log(
        backend="zhipu", model_requested="claude-sonnet-4",
        model_served="glm-5.1",
        input_tokens=200, output_tokens=80,
        success=True,
    )
    total = await logger.query_window_total(5.0, backend="anthropic")
    assert total == 150  # 仅 anthropic


@pytest.mark.asyncio
async def test_query_window_total_excludes_failures(logger):
    await logger.log(
        backend="anthropic", model_requested="claude-sonnet-4",
        model_served="claude-sonnet-4",
        input_tokens=100, output_tokens=50,
        success=True,
    )
    await logger.log(
        backend="anthropic", model_requested="claude-sonnet-4",
        model_served="claude-sonnet-4",
        input_tokens=500, output_tokens=0,
        success=False,
    )
    total = await logger.query_window_total(5.0)
    assert total == 150  # 失败请求不计入


@pytest.mark.asyncio
async def test_query_daily_groups_by_model(logger):
    """query_daily 应按 model_requested 和 model_served 分组."""
    await logger.log(
        backend="anthropic", model_requested="claude-sonnet-4",
        model_served="claude-sonnet-4",
        input_tokens=100, output_tokens=50,
    )
    await logger.log(
        backend="anthropic", model_requested="claude-opus-4",
        model_served="claude-opus-4",
        input_tokens=200, output_tokens=80,
    )
    await logger.log(
        backend="anthropic", model_requested="claude-sonnet-4",
        model_served="claude-sonnet-4",
        input_tokens=150, output_tokens=60,
    )
    rows = await logger.query_daily(days=7)
    assert len(rows) == 2
    models = {r["model_requested"] for r in rows}
    assert models == {"claude-sonnet-4", "claude-opus-4"}
    # 验证 sonnet 聚合正确
    sonnet = next(r for r in rows if r["model_requested"] == "claude-sonnet-4")
    assert sonnet["total_requests"] == 2
    assert sonnet["total_input"] == 250
    assert sonnet["total_output"] == 110


@pytest.mark.asyncio
async def test_query_daily_model_filter(logger):
    """query_daily 的 model 参数应正确过滤."""
    await logger.log(
        backend="anthropic", model_requested="claude-sonnet-4",
        model_served="claude-sonnet-4",
        input_tokens=100, output_tokens=50,
    )
    await logger.log(
        backend="anthropic", model_requested="claude-opus-4",
        model_served="claude-opus-4",
        input_tokens=200, output_tokens=80,
    )
    rows = await logger.query_daily(days=7, model="claude-opus-4")
    assert len(rows) == 1
    assert rows[0]["model_requested"] == "claude-opus-4"
    assert rows[0]["total_requests"] == 1


@pytest.mark.asyncio
async def test_query_daily_shows_model_mapping(logger):
    """故障转移场景：model_requested 与 model_served 不同时应分别展示."""
    await logger.log(
        backend="zhipu", model_requested="claude-sonnet-4",
        model_served="glm-5.1",
        input_tokens=300, output_tokens=100, failover=True,
    )
    await logger.log(
        backend="anthropic", model_requested="claude-sonnet-4",
        model_served="claude-sonnet-4",
        input_tokens=100, output_tokens=50,
    )
    rows = await logger.query_daily(days=7)
    assert len(rows) == 2
    zhipu_row = next(r for r in rows if r["backend"] == "zhipu")
    assert zhipu_row["model_requested"] == "claude-sonnet-4"
    assert zhipu_row["model_served"] == "glm-5.1"
    assert zhipu_row["total_failovers"] == 1
