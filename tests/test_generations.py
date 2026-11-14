"""GenerationManager 核心机制：切换、排空、in-flight 计数、degraded app、关闭."""

from __future__ import annotations

import asyncio
import json
import threading
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field

import httpx
import pytest
from fastapi import FastAPI
from starlette.responses import StreamingResponse

from coding.proxy.config.home import config_db_path
from coding.proxy.config.importer import BootResult, ensure_store_ready
from coding.proxy.config.resolver import MODE_STORE, ResolvedConfig
from coding.proxy.config.schema import ProxyConfig
from coding.proxy.config.store import ConfigStore
from coding.proxy.server import generations
from coding.proxy.server.generations import (
    DEGRADED_ERROR,
    GenerationActivationError,
    GenerationManager,
    PreparedGeneration,
    create_degraded_app,
    get_generation_manager,
)

BASE = "http://127.0.0.1:3392"
PLANTED = "sk-PLANTED-lifespan-secret-0123456789"


# ── 测试用轻量 generation app ────────────────────────────────────


@dataclass
class Spec:
    """一次 build 的行为定义."""

    label: str
    fail_start: bool = False
    hang_start: asyncio.Event | None = None  # 进入 lifespan 时置位，等待 resume
    resume: asyncio.Event = field(default_factory=asyncio.Event)
    gate: asyncio.Event = field(default_factory=asyncio.Event)  # /stream 门控
    events: list[str] = field(default_factory=list)
    tasks: dict[str, asyncio.Task] = field(default_factory=dict)


def make_app(spec: Spec) -> FastAPI:
    """轻量 generation app：/who 标识所属 generation，/stream 可被门控."""

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        if spec.fail_start:
            raise RuntimeError(PLANTED)
        spec.events.append(f"enter:{spec.label}")
        spec.tasks[spec.label] = asyncio.current_task()
        if spec.hang_start is not None:
            spec.hang_start.set()
            await spec.resume.wait()
        yield
        spec.events.append(f"exit:{spec.label}")

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)

    @app.get("/who")
    async def who() -> dict:
        manager = get_generation_manager(app)
        return {"gen": spec.label, "in_flight": manager.in_flight() if manager else -1}

    def sse(gen: AsyncIterator[bytes]) -> StreamingResponse:
        return StreamingResponse(gen, media_type="text/event-stream")

    @app.post("/stream")
    async def stream() -> StreamingResponse:
        async def gen() -> AsyncIterator[bytes]:
            yield f"start:{spec.label}\n".encode()
            await spec.gate.wait()
            yield f"end:{spec.label}\n".encode()

        return sse(gen())

    @app.post("/stream-stuck")
    async def stream_stuck() -> StreamingResponse:
        async def gen() -> AsyncIterator[bytes]:
            yield b"one\n"
            await asyncio.Event().wait()  # 永不结束

        return sse(gen())

    return app


def config() -> ProxyConfig:
    """最小有效配置（fake build 只借用其类型）."""
    return ProxyConfig(vendors=[{"vendor": "anthropic"}])


def boot_result() -> BootResult:
    return BootResult(
        resolved=ResolvedConfig(config=config(), effective_raw={}, mode=MODE_STORE)
    )


class Harness:
    """按次序返回 make_app(spec) 的 build 序列."""

    def __init__(self, specs: list[Spec], **kw) -> None:
        self.specs = specs
        self.calls = 0
        resolved = ResolvedConfig(config=config(), effective_raw={}, mode=MODE_STORE)
        self.manager = GenerationManager(
            boot=BootResult(resolved=resolved), build=self._build, **kw
        )

    def _build(self, cfg) -> FastAPI:
        spec = self.specs[min(self.calls, len(self.specs) - 1)]
        self.calls += 1
        return make_app(spec)

    def spec(self, label: str) -> Spec:
        return next(s for s in self.specs if s.label == label)

    async def wait_event(self, label: str, event: str, timeout: float = 10) -> None:
        want = f"{event}:{label}"
        async with asyncio.timeout(timeout):
            while want not in self.spec(label).events:
                await asyncio.sleep(0.01)


def one(label: str, **kw) -> Harness:
    return Harness([Spec(label=label, **kw)])


def two(label_a: str, label_b: str, **kw) -> Harness:
    return Harness([Spec(label=label_a), Spec(label=label_b)], **kw)


@asynccontextmanager
async def running_lifespan(app):
    """以 ASGI lifespan 协议驱动（同 uvicorn）."""
    inbox: asyncio.Queue = asyncio.Queue()
    outbox: asyncio.Queue = asyncio.Queue()
    task = asyncio.create_task(app({"type": "lifespan"}, inbox.get, outbox.put))
    try:
        await inbox.put({"type": "lifespan.startup"})
        msg = await asyncio.wait_for(outbox.get(), 10)
        assert msg["type"] == "lifespan.startup.complete", msg
        yield
    finally:
        await inbox.put({"type": "lifespan.shutdown"})
        msg = await asyncio.wait_for(outbox.get(), 10)
        assert msg["type"] == "lifespan.shutdown.complete", msg
        await task


def client(app) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url=BASE, timeout=30
    )


@dataclass
class StreamCall:
    task: asyncio.Task
    chunks: list[bytes]
    disconnect: Callable[[], None]


async def stream_call(manager: GenerationManager, path: str) -> StreamCall:
    """裸 ASGI 流式请求：首个 chunk 到达后返回，可模拟客户端断开."""
    inbox: asyncio.Queue = asyncio.Queue()
    body = json.dumps({"stream": True}).encode()
    await inbox.put({"type": "http.request", "body": body, "more_body": False})
    disconnect = asyncio.Event()
    chunks: list[bytes] = []
    first_chunk = asyncio.Event()

    async def receive():
        get = asyncio.create_task(inbox.get())
        done = asyncio.wait({get}, timeout=0.005)
        ready = await done
        if get in ready:
            return get.result()
        get.cancel()
        if disconnect.is_set():
            return {"type": "http.disconnect"}
        return await inbox.get()

    async def send(message):
        if message["type"] == "http.response.body" and message.get("body"):
            chunks.append(message["body"])
            first_chunk.set()

    scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "root_path": "",
        "headers": [
            (b"host", b"127.0.0.1:3392"),
            (b"content-type", b"application/json"),
        ],
        "client": ("127.0.0.1", 50000),
        "server": ("127.0.0.1", 3392),
    }
    task = asyncio.create_task(manager(scope, receive, send))
    await asyncio.wait_for(first_chunk.wait(), 10)
    return StreamCall(task=task, chunks=chunks, disconnect=disconnect.set)


# ── 基础行为 ─────────────────────────────────────────────────────


async def test_startup_and_shutdown_lifespan_in_dedicated_task():
    h = two("a", "b")
    async with running_lifespan(h.manager):
        async with client(h.manager) as c:
            assert (await c.get("/who")).json()["gen"] == "a"
    assert h.spec("a").events == ["enter:a", "exit:a"]
    task = h.spec("a").tasks["a"]
    assert task is not asyncio.current_task()
    assert task.get_name().startswith("coding-proxy-generation-")


async def test_request_before_startup_is_503():
    h = one("a")
    async with client(h.manager) as c:
        assert (await c.get("/who")).status_code == 503


async def test_build_is_sync_and_thread_safe():
    h = two("a", "b")
    barrier = threading.Barrier(2, timeout=5)
    inner = h.manager._build

    def build(cfg):
        barrier.wait()  # 两个 build 必须同时在各自 worker 线程中执行，否则超时
        return inner(cfg)

    h.manager._build = build
    first, second = await asyncio.gather(
        asyncio.to_thread(h.manager.build, config()),
        asyncio.to_thread(h.manager.build, config()),
    )
    assert isinstance(first, PreparedGeneration)
    assert second.app is not first.app
    assert get_generation_manager(first.app) is get_generation_manager(second.app)
    await h.manager.activate(second)  # 未进入 lifespan 的 prepared 可安全丢弃


async def test_activate_rejects_reuse_and_after_shutdown():
    h = one("a")
    prepared = h.manager.build(config())
    await h.manager.activate(prepared)
    with pytest.raises(GenerationActivationError):
        await h.manager.activate(prepared)
    await h.manager._shutdown()
    with pytest.raises(GenerationActivationError):
        await h.manager.activate(h.manager.build(config()))


# ── 流式请求跨切换 ────────────────────────────────────────────────


async def test_stream_finishes_on_old_generation_new_requests_hit_new():
    h = two("a", "b")
    async with running_lifespan(h.manager):
        call = await stream_call(h.manager, "/stream")
        assert h.manager.in_flight() == 1

        await h.manager.activate(h.manager.build(config()))
        assert h.manager.in_flight() == 0  # 新 generation 无进行中请求
        assert "exit:a" not in h.spec("a").events  # 旧流未结束，不关闭

        async with client(h.manager) as c:
            assert (await c.get("/who")).json()["gen"] == "b"

        h.spec("a").gate.set()
        await asyncio.wait_for(call.task, 10)
        assert b"end:a" in b"".join(call.chunks)  # 旧流在旧 generation 上完成
        await h.wait_event("a", "exit")  # 排空后旧 generation 退出 lifespan
        assert h.spec("a").events == ["enter:a", "exit:a"]


async def test_drain_timeout_forces_old_generation_closed(caplog):
    h = two("a", "b", drain_timeout=0.05)
    async with running_lifespan(h.manager):
        call = await stream_call(h.manager, "/stream")
        await h.manager.activate(h.manager.build(config()))
        with caplog.at_level("WARNING"):
            await h.wait_event("a", "exit")
        assert "排空超时" in caplog.text  # 请求仍在进行也强制关闭
        call.task.cancel()


async def test_in_flight_returns_to_zero_on_client_disconnect():
    h = one("a")
    async with running_lifespan(h.manager):
        call = await stream_call(h.manager, "/stream-stuck")
        assert h.manager.in_flight() == 1
        call.disconnect()
        await asyncio.wait_for(call.task, 10)  # 断开后 ASGI 调用返回
        assert h.manager.in_flight() == 0


async def test_single_flight_serializes_activations():
    hang = asyncio.Event()
    h = Harness(
        [Spec(label="init"), Spec(label="hang", hang_start=hang), Spec(label="b")]
    )
    async with running_lifespan(h.manager):
        first = asyncio.create_task(h.manager.activate(h.manager.build(config())))
        await asyncio.wait_for(hang.wait(), 10)  # 第一个 lifespan 启动挂起
        second = asyncio.create_task(h.manager.activate(h.manager.build(config())))
        await asyncio.wait({second}, timeout=0.1)  # 单飞：第二个等待第一个
        assert not second.done()
        h.spec("hang").resume.set()
        results = await asyncio.gather(first, second, return_exceptions=True)
        assert not any(isinstance(r, BaseException) for r in results)


# ── 启用失败 ─────────────────────────────────────────────────────


async def test_activation_failure_keeps_old_generation():
    h = one("a")
    h.specs.append(Spec(label="bad", fail_start=True))
    async with running_lifespan(h.manager):
        with pytest.raises(GenerationActivationError) as exc:
            await h.manager.activate(h.manager.build(config()))
        assert PLANTED not in str(exc.value)
        async with client(h.manager) as c:
            assert (await c.get("/who")).json()["gen"] == "a"
        assert h.spec("a").events == ["enter:a"]


async def test_cancelled_start_stops_new_generation():
    h = one("a")
    hang = asyncio.Event()
    h.specs.append(Spec(label="hang", hang_start=hang))
    async with running_lifespan(h.manager):
        activate = asyncio.create_task(h.manager.activate(h.manager.build(config())))
        await asyncio.wait_for(hang.wait(), 10)
        activate.cancel()
        with pytest.raises(asyncio.CancelledError):
            await activate
        h.spec("hang").resume.set()
        await h.wait_event("hang", "exit")  # 启动完成后立即退出，不遗留
        async with client(h.manager) as c:
            assert (await c.get("/who")).json()["gen"] == "a"


@pytest.mark.parametrize("step", ["_migrate_bindings", "_runtime_resets"])
async def test_failure_after_start_stops_new_generation(monkeypatch, step):
    """lifespan 已进入、指针尚未切换时失败：新 generation 必须退出 lifespan，不遗留."""

    def boom(*args, **kwargs):
        raise RuntimeError(PLANTED)

    h = two("a", "b")
    async with running_lifespan(h.manager):
        monkeypatch.setattr(generations, step, boom)
        with pytest.raises(GenerationActivationError) as exc:
            await h.manager.activate(h.manager.build(config()))
        assert PLANTED not in str(exc.value)
        assert h.spec("b").events == ["enter:b", "exit:b"]
        async with client(h.manager) as c:
            assert (await c.get("/who")).json()["gen"] == "a"
    leaked = [
        t.get_name()
        for t in asyncio.all_tasks()
        if t.get_name().startswith("coding-proxy-generation-") and not t.done()
    ]
    assert leaked == []


# ── running_revisions ───────────────────────────────────────────


async def test_activate_records_revisions_of_the_built_config():
    h = two("a", "b")
    async with running_lifespan(h.manager):
        await h.manager.activate(h.manager.build(config()), revisions={"tiers": 7})
        assert h.manager.running_revisions == {"tiers": 7}


async def test_fallback_revisions_are_read_before_lifespan_start():
    """未传 revisions：快照先于 lifespan 启动读取，启动期间其它写入方的提交不计为已运行."""
    ensure_store_ready()
    before = dict(ConfigStore(config_db_path()).snapshot().revisions)
    hang = asyncio.Event()
    h = Harness([Spec(label="a"), Spec(label="hang", hang_start=hang)])
    async with running_lifespan(h.manager):
        activate = asyncio.create_task(h.manager.activate(h.manager.build(config())))
        await asyncio.wait_for(hang.wait(), 10)
        with ConfigStore(config_db_path()).transaction() as txn:  # 模拟 CLI 写入
            txn.put_section("tiers", ["anthropic"])
        h.spec("hang").resume.set()
        await activate
        assert h.manager.running_revisions == before
        assert ConfigStore(config_db_path()).snapshot().revision("tiers") == (
            before.get("tiers", 0) + 1
        )


# ── bootstrap 字段钉住 ───────────────────────────────────────────


def _with_db(path: str) -> ProxyConfig:
    return config().model_copy(
        update={"database": config().database.model_copy(update={"path": path})}
    )


async def test_bootstrap_pinned_to_first_healthy_generation_after_degraded_boot(
    tmp_path,
):
    h = one("a")
    h.manager.boot.resolved.config = None  # degraded 启动
    first_db, cli_db = str(tmp_path / "first.db"), str(tmp_path / "cli.db")
    async with running_lifespan(h.manager):
        # degraded 时无运行中的 database：取存储值
        recovered = h.manager.build(_with_db(first_db))
        assert recovered.config.database.path == first_db
        await h.manager.activate(recovered)
        # 之后 CLI 改 database（需重启生效）：Dashboard 保存不得在线换库
        again = h.manager.build(_with_db(cli_db))
        assert again.config.database.path == first_db


async def test_bootstrap_not_pinned_by_failed_activation(tmp_path):
    h = one("a")
    h.manager.boot.resolved.config = None
    h.specs.append(Spec(label="bad", fail_start=True))
    bad_db, good_db = str(tmp_path / "bad.db"), str(tmp_path / "good.db")
    async with running_lifespan(h.manager):
        h.calls = 1  # 下一次 build 返回 fail_start 的 generation
        with pytest.raises(GenerationActivationError):
            await h.manager.activate(h.manager.build(_with_db(bad_db)))
        assert h.manager.build(_with_db(good_db)).config.database.path == good_db


# ── 关闭 ─────────────────────────────────────────────────────────


async def test_shutdown_stops_current_and_draining_generations():
    h = two("a", "b")
    async with running_lifespan(h.manager):
        call = await stream_call(h.manager, "/stream")
        await h.manager.activate(h.manager.build(config()))
        assert "exit:a" not in h.spec("a").events
    # running_lifespan 退出即 lifespan.shutdown：两个 generation 都已关闭
    assert "exit:a" in h.spec("a").events
    assert "exit:b" in h.spec("b").events
    call.task.cancel()


# ── degraded app ─────────────────────────────────────────────────


async def test_degraded_app_returns_503_and_serves_admin_surface():
    boot = boot_result()
    boot.resolved.errors = [{"loc": ["vendors", 0], "msg": "bad"}]
    app = create_degraded_app(boot)
    async with running_lifespan(app):
        async with client(app) as c:
            resp = await c.post("/v1/messages", json={"stream": False})
            assert resp.status_code == 503
            assert resp.json() == DEGRADED_ERROR
            assert resp.headers["content-security-policy"].startswith("sandbox")
            assert (
                await c.post("/api/openai/v1/chat/completions", json={})
            ).status_code == 503
            assert (await c.get("/api/status")).json()["tiers"] == []
            assert (await c.get("/health")).status_code == 200
            assert (await c.get("/")).status_code == 307


async def test_boot_without_config_starts_degraded():
    h = one("a")
    h.manager.boot.resolved.config = None  # 配置为 None：build 不会被调用
    async with running_lifespan(h.manager):
        async with client(h.manager) as c:
            resp = await c.post("/v1/messages", json={})
            assert resp.status_code == 503
            assert resp.json() == DEGRADED_ERROR


async def test_boot_build_failure_falls_back_to_degraded(caplog):
    def boom(cfg):
        raise ValueError("至少需要一个供应商层级")

    h = one("a")
    h.manager._build = boom
    with caplog.at_level("ERROR"):
        async with running_lifespan(h.manager):
            async with client(h.manager) as c:
                assert (await c.post("/v1/messages", json={})).status_code == 503
                assert (
                    await c.get("/api/config")
                ).status_code == 401  # admin surface 已装
    assert "运行时构建失败" in caplog.text
    assert h.manager.boot.resolved.errors[-1]["msg"].endswith("至少需要一个供应商层级")


async def test_boot_lifespan_failure_falls_back_to_degraded(caplog):
    """初始 generation lifespan 启动失败（如 database.path 不可写）：degraded 启动而非退出."""
    h = one("a", fail_start=True)
    with caplog.at_level("DEBUG"):
        async with running_lifespan(h.manager):
            async with client(h.manager) as c:
                resp = await c.post("/v1/messages", json={})
                assert resp.status_code == 503
                assert resp.json() == DEGRADED_ERROR
                assert (await c.get("/dashboard")).status_code == 200
    msg = h.manager.boot.resolved.errors[-1]["msg"]
    assert msg.startswith("运行时启动失败: RuntimeError")
    assert "coding-proxy config set" in msg
    assert "以 degraded 模式启动" in caplog.text
    assert PLANTED not in msg and PLANTED not in caplog.text


async def test_startup_failed_message_has_no_exception_text(caplog):
    def broken_degraded(boot):
        raise RuntimeError(PLANTED)

    h = one("a")
    h.manager.boot.resolved.config = None
    h.manager._degraded_factory = broken_degraded
    inbox: asyncio.Queue = asyncio.Queue()
    outbox: asyncio.Queue = asyncio.Queue()
    with caplog.at_level("DEBUG"):
        task = asyncio.create_task(
            h.manager({"type": "lifespan"}, inbox.get, outbox.put)
        )
        await inbox.put({"type": "lifespan.startup"})
        msg = await asyncio.wait_for(outbox.get(), 10)
        await asyncio.wait_for(task, 10)
    assert msg == {
        "type": "lifespan.startup.failed",
        "message": "RuntimeError during startup",
    }
    assert PLANTED not in caplog.text
