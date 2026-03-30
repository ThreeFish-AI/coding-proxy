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
