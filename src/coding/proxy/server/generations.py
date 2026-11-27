"""进程内 generation swap：uvicorn 运行稳定的 ASGI dispatcher，listener 全程不关.

- generation = ``create_app(cfg)`` 构建的 FastAPI app；其 lifespan 在专属 task 中进入并退出
  （enter → 等待 stop → exit），满足 anyio cancel scope / contextvars 的同 task 约束；
- ``http`` / ``websocket`` 转发至当前 generation，按 generation 计 in-flight：读取指针与计数
  之间无 await，计数覆盖整个 ASGI 调用（流式响应直至响应体发完或客户端断开）；
- ``activate``：单飞；新 generation lifespan 启动成功后迁移运行时 session 绑定并原子切换
  指针（两者之间无 await），旧 generation 后台排空（上限 ``drain_timeout``）后退出 lifespan
  （关闭 vendor client 与 DB 连接）；启动后任一步失败都会先停掉新 generation，旧 generation
  继续服务；
- 切换时旧 generation 中仍在进行的 OAuth 重认证登录交接给新 generation：完成后的凭证
  同步进新 generation，登录期间新 generation 报 pending（不重复触发浏览器登录）；
- 存储的配置无效（或启动期构建 / lifespan 启动失败）时以 degraded app 提供 Dashboard 与
  管理 API，``/v1/*`` 与原生透传返回 503，在 Config 页保存有效配置后切换为正常 generation。
"""

from __future__ import annotations

import asyncio
import itertools
import logging
import os
import traceback
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ValidationError
from starlette.types import Receive, Scope, Send

from .. import __version__
from ..auth.runtime import ReauthState, RuntimeReauthCoordinator
from ..config.home import config_db_path
from ..config.importer import BootResult
from ..config.normalize import expand_tmpl
from ..config.schema import ProxyConfig
from ..config.server import LoggingConfig, ServerConfig
from ..config.store import ConfigStore, ConfigStoreError
from ..routing.circuit_breaker import CircuitState
from .app import create_app
from .config_routes import in_flight, install_admin_surface
from .dashboard import register_dashboard_routes
from .routes import register_health_routes
from .security import NATIVE_PASSTHROUGH_PREFIXES, install_security

logger = logging.getLogger(__name__)

DEGRADED_ERROR: dict[str, Any] = {
    "error": {
        "type": "config_invalid",
        "message": "存储的配置无效，请在 Dashboard → Config 修复",
    }
}
_NOT_READY: dict[str, Any] = {
    "error": {"type": "api_error", "message": "coding-proxy 尚未就绪或正在关闭"}
}
_ALL_METHODS = ["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"]
_STARTUP_HINT = (
    "；请检查 database / auth 中的路径是否可写，用 `coding-proxy config set` 修正后重启"
)


class GenerationActivationError(RuntimeError):
    """新 generation 启用失败，旧 generation 继续服务；消息不含配置值，可直接返回客户端."""


@dataclass(eq=False)
class PreparedGeneration:
    """已构建、尚未进入 lifespan 的 generation（尚未打开 DB / 上游连接）."""

    app: FastAPI
    config: ProxyConfig | None = field(default=None, repr=False)
    activated: bool = field(default=False, repr=False)


class _Generation:
    """已启用的 generation：app + 专属 lifespan task + in-flight 计数."""

    def __init__(self, app: FastAPI, seq: int) -> None:
        self.app = app
        self.seq = seq
        self.in_flight = 0
        self.state: dict[str, Any] = {}
        self.order, self.limits = _runtime_baseline(app)
        self._drained = asyncio.Event()
        self._drained.set()
        self._stop = asyncio.Event()
        self._task: asyncio.Task[None] | None = None

    def enter(self) -> None:
        self.in_flight += 1
        self._drained.clear()

    def leave(self) -> None:
        self.in_flight -= 1
        if self.in_flight <= 0:
            self._drained.set()

    def release(self) -> None:
        """服务关闭：不再等待排空."""
        self._drained.set()

    async def wait_drained(self) -> None:
        await self._drained.wait()

    async def start(self) -> None:
        """在专属 task 中进入 lifespan，启动完成后返回；启动失败原样抛出."""
        started: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        self._task = asyncio.create_task(
            self._run(started), name=f"coding-proxy-generation-{self.seq}"
        )
        try:
            await asyncio.shield(started)
        except asyncio.CancelledError:
            # 调用方被取消：启动完成后立即退出 lifespan，不遗留半启用的 generation
            self._stop.set()
            started.add_done_callback(_consume)
            raise

    async def _run(self, started: asyncio.Future[None]) -> None:
        try:
            async with self.app.router.lifespan_context(self.app) as state:
                if state:
                    self.state.update(state)
                if not started.done():
                    started.set_result(None)
                await self._stop.wait()
        except asyncio.CancelledError:
            if not started.done():
                started.cancel()
            raise
        except Exception as exc:
            if not started.done():
                await _close_partial(self.app)
                started.set_exception(exc)
            else:
                logger.error(
                    "generation #%d lifespan 退出异常: %s",
                    self.seq,
                    type(exc).__name__,
                    exc_info=exc,
                )

    async def stop(self) -> None:
        """退出 lifespan（关闭 vendor client / DB）；调用方被取消不打断退出过程."""
        self._stop.set()
        if self._task is not None:
            await asyncio.shield(self._task)


class GenerationManager:
    """uvicorn 的 ASGI 入口：持有当前 generation，负责 lifespan、请求转发与热切换."""

    def __init__(
        self,
        *,
        boot: BootResult,
        build: Callable[[ProxyConfig], FastAPI] = create_app,
        degraded_factory: Callable[[BootResult], FastAPI] | None = None,
        drain_timeout: float = 600.0,
    ) -> None:
        self.boot = boot
        self.running_revisions: dict[str, int] = {}
        self._build = build
        self._degraded_factory = degraded_factory or create_degraded_app
        self._drain_timeout = drain_timeout
        # bootstrap 配置以启动时为准（-p/-h 直接修改 self.server），经 CLI 修改需重启生效
        self.server, self.logging = bootstrap_settings(boot)
        self._seq = itertools.count(1)
        self._lock = asyncio.Lock()
        self._current: _Generation | None = None
        # 首个成功启用的正常 generation 的配置：database / auth bootstrap 字段以其为准
        self._bootstrap_cfg: ProxyConfig | None = None
        self._retiring: dict[_Generation, asyncio.Task[None]] = {}
        # provider → 等待旧 generation 中进行中登录结束的 task（见 _relay_reauth）
        self._reauth_relays: dict[str, asyncio.Task[None]] = {}
        self._closing = False

    # ── 查询 ────────────────────────────────────────────────────

    @property
    def current_app(self) -> FastAPI | None:
        """当前对外服务的 generation app."""
        return self._current.app if self._current is not None else None

    def in_flight(self) -> int:
        """当前 generation 进行中的请求数."""
        return self._current.in_flight if self._current is not None else 0

    # ── 构建与启用 ──────────────────────────────────────────────

    def build(self, cfg: ProxyConfig) -> PreparedGeneration:
        """构建新 generation（同步、可在 worker 线程调用；不进入 lifespan）."""
        cfg = self._pin_bootstrap(cfg)
        return self._prepare(self._build(cfg), cfg)

    def _pin_bootstrap(self, cfg: ProxyConfig) -> ProxyConfig:
        """server / logging 沿用启动值；database 与 auth 中的 bootstrap 字段沿用首个成功
        启用的正常 generation（auth 中仅 google_client_secret 可在线修改）。尚无正常
        generation 运行过（degraded）时取存储中的值."""
        update: dict[str, Any] = {"server": self.server, "logging": self.logging}
        running = self._bootstrap_cfg
        if running is not None:
            update["database"] = running.database
            update["auth"] = cfg.auth.model_copy(
                update={
                    "token_store_path": running.auth.token_store_path,
                    "google_client_id": running.auth.google_client_id,
                }
            )
        return cfg.model_copy(update=update)

    def _prepare(
        self, app: FastAPI, cfg: ProxyConfig | None = None
    ) -> PreparedGeneration:
        app.state.generation_manager = self
        app.state.boot = self.boot
        return PreparedGeneration(app=app, config=cfg)

    async def activate(
        self,
        prepared: PreparedGeneration,
        *,
        revisions: Mapping[str, int] | None = None,
    ) -> dict[str, list[str]]:
        """启用已构建的 generation 并切换，返回 ``{"reset_runtime": [...]}``.

        ``revisions``：构建该 generation 所依据的存储 revision（即持久化该配置的那次
        提交的 ``ChangeResult.revisions``），成功后记为 ``running_revisions``；缺省时取
        启用开始时的存储快照（提交之后其它写入方的改动会被误记为已运行）。
        失败时旧 generation 保持服务，抛 :class:`GenerationActivationError`。
        """
        async with self._lock:
            if self._closing:
                raise GenerationActivationError(
                    "服务正在关闭，新配置将在下次启动时生效"
                )
            if prepared.activated:
                raise GenerationActivationError("该 generation 已启用过")
            try:
                return await self._swap_in(
                    prepared, dict(revisions) if revisions is not None else None
                )
            except Exception as exc:
                logger.warning(
                    "新 generation 启用失败，保持当前 generation: %s",
                    type(exc).__name__,
                )
                raise GenerationActivationError(
                    f"新配置已保存但启用失败（{type(exc).__name__}），"
                    "当前仍以原配置运行，重启后生效"
                ) from None

    async def _swap_in(
        self, prepared: PreparedGeneration, revisions: dict[str, int] | None = None
    ) -> dict[str, list[str]]:
        prepared.activated = True
        if revisions is None:  # 先于 lifespan 启动读取，缩小与其它写入方的竞态窗口
            revisions = await asyncio.to_thread(_store_revisions)
        gen = _Generation(prepared.app, next(self._seq))
        await gen.start()
        # 进入 lifespan 后到切换指针前任一步失败：停掉新 generation，不遗留。
        # 迁移运行时状态与切换指针之间无 await：旧 generation 上的状态不会丢失
        try:
            old = self._current
            # 已知局限：切换后瞬间在旧 generation 上完成的请求新写入的绑定会丢失
            # （窗口为毫秒级；绑定也可经 Dashboard/CLI 重新设置）
            dropped, model_dropped, vendor_dropped = (
                _migrate_bindings(_bindings(old.app), gen.app) if old else (0, 0, 0)
            )
            resets = _runtime_resets(old, gen, dropped, model_dropped, vendor_dropped)
            relayed = self._relay_reauth(old.app, gen.app) if old else []
        except BaseException:
            await gen.stop()
            raise
        self._current = gen
        self.running_revisions = revisions
        if prepared.config is not None and self._bootstrap_cfg is None:
            self._bootstrap_cfg = prepared.config
        if relayed:
            logger.info("进行中的重认证已交接至新 generation: %s", ", ".join(relayed))
        if old is not None:
            self._retire(old)
            logger.info(
                "已切换至 generation #%d（旧 generation #%d 进行中请求 %d 个）",
                gen.seq,
                old.seq,
                old.in_flight,
            )
        return {"reset_runtime": resets}

    def _relay_reauth(self, old: FastAPI, new: FastAPI) -> list[str]:
        """旧 generation 的 OAuth 重认证交接给新 generation，返回交接中的 provider.

        登录 task 属于旧 generation 的 coordinator，in-flight 计数覆盖不到：之后在旧
        coordinator 上完成的登录，凭证同步写入当前 generation；切换时仍在进行的登录，
        新 generation 报 pending（不重复触发浏览器登录），结束后同步最终状态。
        """
        origin = _coordinator(old)
        if origin is not None:
            for provider in origin._providers:
                _forward_tokens(origin, provider, self._current_coordinator)
                if (
                    origin._locks[provider].locked()  # 登录进行中（task 持锁直至结束）
                    and provider not in self._reauth_relays
                ):
                    self._reauth_relays[provider] = asyncio.create_task(
                        self._finish_relay(origin, provider),
                        name=f"coding-proxy-reauth-relay-{provider}",
                    )
        target = _coordinator(new)
        if target is None:
            return []
        relayed = [p for p in self._reauth_relays if p in target._states]
        for provider in relayed:
            target._states[provider] = ReauthState.PENDING
        return relayed

    def _current_coordinator(self) -> RuntimeReauthCoordinator | None:
        return _coordinator(self._current.app) if self._current is not None else None

    async def _finish_relay(
        self, origin: RuntimeReauthCoordinator, provider: str
    ) -> None:
        """旧 coordinator 上的登录结束后，把最终状态同步给当前 generation."""
        try:
            async with origin._locks[provider]:
                pass
        finally:
            self._reauth_relays.pop(provider, None)
        target = self._current_coordinator()
        if target is None or target is origin or provider not in target._states:
            return
        target._states[provider] = origin._states[provider]
        for attr in ("_last_error", "_last_completed"):
            src, dst = getattr(origin, attr), getattr(target, attr)
            if provider in src:
                dst[provider] = src[provider]
            else:
                dst.pop(provider, None)

    def _retire(self, gen: _Generation) -> None:
        task = asyncio.create_task(
            self._drain_and_stop(gen), name=f"coding-proxy-drain-{gen.seq}"
        )
        self._retiring[gen] = task
        task.add_done_callback(lambda _t, g=gen: self._retiring.pop(g, None))

    async def _drain_and_stop(self, gen: _Generation) -> None:
        try:
            await asyncio.wait_for(gen.wait_drained(), self._drain_timeout)
        except TimeoutError:
            logger.warning(
                "generation #%d 排空超时（%.0fs），%d 个请求仍在进行，强制关闭",
                gen.seq,
                self._drain_timeout,
                gen.in_flight,
            )
        await gen.stop()
        logger.info("generation #%d 已关闭", gen.seq)

    # ── ASGI ────────────────────────────────────────────────────

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "lifespan":
            await self._lifespan(receive, send)
            return
        gen = self._current
        if gen is None:
            await _not_ready(scope, receive, send)
            return
        gen.enter()  # 与读取指针之间无 await：请求归属在切换前后确定
        try:
            if gen.state:
                scope = {**scope, "state": {**scope.get("state", {}), **gen.state}}
            await gen.app(scope, receive, send)
        finally:
            gen.leave()

    async def _lifespan(self, receive: Receive, send: Send) -> None:
        await receive()  # lifespan.startup
        try:
            await self._startup()
        except Exception as exc:
            logger.error("启动失败: %s（%s）", type(exc).__name__, _where(exc))
            await send(
                {
                    "type": "lifespan.startup.failed",
                    "message": f"{type(exc).__name__} during startup",
                }
            )
            return
        await send({"type": "lifespan.startup.complete"})
        await receive()  # lifespan.shutdown
        try:
            await self._shutdown()
        except Exception as exc:
            await send(
                {
                    "type": "lifespan.shutdown.failed",
                    "message": f"{type(exc).__name__} during shutdown",
                }
            )
            return
        await send({"type": "lifespan.shutdown.complete"})

    async def _startup(self) -> None:
        """启用初始 generation；配置无效、构建或 lifespan 启动失败时启用 degraded app."""
        revisions = dict(self.boot.resolved.revisions)
        prepared: PreparedGeneration | None = None
        cfg = self.boot.resolved.config
        if cfg is not None:
            try:
                prepared = self.build(cfg)
            except Exception as exc:
                self._boot_failed(f"运行时构建失败: {_reason(exc)}")
        async with self._lock:
            if prepared is not None:
                try:
                    await self._swap_in(prepared, revisions)
                    return
                except Exception as exc:
                    # lifespan 异常消息可能夹带配置值（如路径），只记类型名与出错位置
                    self._boot_failed(
                        f"运行时启动失败: {type(exc).__name__}{_STARTUP_HINT}",
                        where=_where(exc),
                    )
            degraded = self._prepare(self._degraded_factory(self.boot))
            await self._swap_in(degraded, revisions)

    def _boot_failed(self, msg: str, *, where: str = "") -> None:
        """记录启动期失败并写入 boot 错误（Config 页展示），随后以 degraded 启动."""
        logger.error("%s；以 degraded 模式启动%s", msg, f"（{where}）" if where else "")
        self.boot.resolved.errors.append({"loc": [], "msg": msg})

    async def _shutdown(self) -> None:
        self._closing = True
        async with self._lock:  # 等待进行中的 activate 结束
            current, self._current = self._current, None
            for gen in self._retiring:
                gen.release()
            relays = list(self._reauth_relays.values())
            for relay in relays:
                relay.cancel()
            await asyncio.gather(*relays, return_exceptions=True)
            pending: list[Any] = list(self._retiring.values())
            if current is not None:
                pending.append(current.stop())
            for result in await asyncio.gather(*pending, return_exceptions=True):
                if isinstance(result, BaseException):
                    logger.error("generation 关闭异常: %s", type(result).__name__)


# ── degraded app ────────────────────────────────────────────────


def create_degraded_app(boot: BootResult) -> FastAPI:
    """配置无效时的 admin-only app：Dashboard + 管理 API，代理入口一律 503."""
    server, _ = bootstrap_settings(boot)
    app = FastAPI(
        title="coding-proxy",
        version=__version__,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    install_security(app, allowed_hosts=server.allowed_hosts)
    app.state.boot = boot

    async def unavailable(request: Request) -> JSONResponse:
        return JSONResponse(DEGRADED_ERROR, status_code=503)

    for prefix in ("/v1", *NATIVE_PASSTHROUGH_PREFIXES):
        for path in (prefix, prefix + "/{path:path}"):
            app.add_route(
                path, unavailable, methods=_ALL_METHODS, include_in_schema=False
            )

    @app.get("/api/status", include_in_schema=False)
    async def status(request: Request) -> dict:
        return {"tiers": [], "in_flight": in_flight(request.app)}

    install_admin_surface(app)
    register_health_routes(app)
    register_dashboard_routes(app)
    return app


def get_generation_manager(app: Any) -> GenerationManager | None:
    """读取 ``app.state.generation_manager``（未接入时为 None）."""
    return getattr(getattr(app, "state", None), "generation_manager", None)


def bootstrap_settings(boot: BootResult) -> tuple[ServerConfig, LoggingConfig]:
    """启动所需的 server / logging 配置；配置无效时逐 section 校验，无效 section 用默认值.

    配置有效时返回 ``boot.resolved.config`` 中的对象本身。
    """
    cfg = boot.resolved.config
    if cfg is not None:
        return cfg.server, cfg.logging
    return _salvage(boot, "server", ServerConfig), _salvage(
        boot, "logging", LoggingConfig
    )


# ── 内部工具 ────────────────────────────────────────────────────


def _salvage[M: BaseModel](boot: BootResult, section: str, model: type[M]) -> M:
    raw = boot.resolved.effective_raw.get(section)
    try:
        data = expand_tmpl(raw, os.environ) if isinstance(raw, dict) else {}
        return model.model_validate(data)
    except ValidationError:
        logger.warning("%s 配置无效，启动时使用默认值", section)
        return model()


def _store_revisions() -> dict[str, int]:
    try:
        return dict(ConfigStore(config_db_path()).snapshot().revisions)
    except ConfigStoreError as exc:
        logger.warning("读取 config.db revision 失败: %s", type(exc).__name__)
        return {}


def _reason(exc: BaseException) -> str:
    """构建失败摘要：仅 ``ValueError`` 本体附带消息（构建代码自身的校验文案，不含配置值；
    ValidationError 等子类可能夹带输入值，只给类型名）."""
    if type(exc) is ValueError and str(exc):
        return str(exc)
    return type(exc).__name__


def _where(exc: BaseException) -> str:
    """异常最内层帧的位置（``文件:行 in 函数``，不含异常消息）."""
    frames = traceback.extract_tb(exc.__traceback__)
    if not frames:
        return type(exc).__name__
    last = frames[-1]
    return f"{os.path.basename(last.filename)}:{last.lineno} in {last.name}"


async def _close_partial(app: FastAPI) -> None:
    """lifespan 启动失败时尽力关闭已打开的资源（create_app 的 lifespan 无 try/finally）."""
    for name, method in (
        ("router", "close"),
        ("native_handler", "aclose"),
        ("compat_session_store", "close"),
        ("token_logger", "close"),
    ):
        close = getattr(getattr(app.state, name, None), method, None)
        if close is None:
            continue
        try:
            await close()
        except Exception as exc:
            logger.debug("清理 %s 失败: %s", name, type(exc).__name__)


def _consume(future: asyncio.Future[Any]) -> None:
    if not future.cancelled():
        future.exception()


async def _not_ready(scope: Scope, receive: Receive, send: Send) -> None:
    if scope["type"] == "http":
        await JSONResponse(_NOT_READY, status_code=503)(scope, receive, send)
    elif scope["type"] == "websocket":
        await send({"type": "websocket.close", "code": 1013})


def _router(app: FastAPI) -> Any | None:
    return getattr(app.state, "router", None)


def _coordinator(app: FastAPI) -> RuntimeReauthCoordinator | None:
    coordinator = getattr(app.state, "reauth_coordinator", None)
    return coordinator if isinstance(coordinator, RuntimeReauthCoordinator) else None


def _forward_tokens(
    origin: RuntimeReauthCoordinator,
    provider: str,
    current: Callable[[], RuntimeReauthCoordinator | None],
) -> None:
    """退役 coordinator 之后完成的登录：凭证同时写入当前 generation 的 token manager.

    coordinator 在登录完成后才查找回调，替换对进行中的登录同样生效。
    """
    own = origin._token_updaters.get(provider)
    if getattr(own, "forwards_to_current", False):
        return

    def update(token: str) -> None:
        if own is not None:
            own(token)
        target = current()
        if target is None or target is origin:
            return
        # tokens.json 已由 origin 写入：重新加载，避免当前 generation 之后的写入回退凭证
        target._token_store.load()
        if (forward := target._token_updaters.get(provider)) is not None:
            forward(token)

    update.forwards_to_current = True  # type: ignore[attr-defined]
    origin._token_updaters[provider] = update


def _policy_resolver(app: FastAPI) -> Any | None:
    executor = getattr(_router(app), "_executor", None)
    return getattr(executor, "_policy_resolver", None)


def _concurrency_limits(router: Any) -> dict[str, dict[str, int]]:
    limits: dict[str, dict[str, int]] = {}
    for tier in router.tiers:
        controller = getattr(tier.vendor, "_concurrency_controller", None)
        config = getattr(controller, "_config", None)
        if config is not None:
            limits[tier.name] = dict(config.models)
    return limits


def _runtime_baseline(app: FastAPI) -> tuple[list[str], dict[str, dict[str, int]]]:
    """启用时的链路顺序与并发上限（用于识别之后的运行时调整）."""
    router = _router(app)
    if router is None:
        return [], {}
    return router.get_vendor_names(), _concurrency_limits(router)


def _bindings(app: FastAPI) -> dict[str, tuple[tuple[str, ...], str | None]]:
    """运行时绑定快照：session → (vendor 元组, 模型覆盖或 None)."""
    resolver = _policy_resolver(app)
    if resolver is None:
        return {}
    return {
        str(b["session_key"]): (tuple(b["vendors"]), b.get("model"))
        for b in resolver.list_runtime_bindings()
    }


def _available(vendors: tuple[str, ...] | None, app: FastAPI) -> tuple[str, ...] | None:
    """绑定迁移到 ``app`` 后的 vendor 列表（不在其链路中的 vendor 去掉；全部不在时为 None）."""
    router = _router(app)
    if vendors is None or router is None:
        return None
    names = set(router.get_vendor_names())
    return tuple(v for v in vendors if v in names) or None


def _migrate_bindings(
    bindings: dict[str, tuple[tuple[str, ...], str | None]], app: FastAPI
) -> tuple[int, int, int]:
    """把运行时 session 绑定写入 ``app``，返回（整条丢弃数, 模型侧降级数, vendor 侧降级数）.

    vendor 只保留新链路中仍存在的，模型只保留新 ``bindable_models`` 清单内的；
    单侧失效时降级保留另一侧并分别计数（双侧失效整条丢弃）。
    """
    resolver = _policy_resolver(app)
    router = _router(app)
    if resolver is None:
        return (
            len(bindings),
            sum(1 for _, (_, m) in bindings.items() if m),
            sum(1 for _, (v, m) in bindings.items() if v and not m),
        )
    catalog = {
        str(entry.get("model"))
        for entry in (router.get_known_models() if router else [])
    }
    dropped = 0
    model_dropped = 0
    vendor_dropped = 0
    for key, (vendors, model) in bindings.items():
        kept = _available(vendors, app)
        model_kept = model if model in catalog else None
        if kept or model_kept:
            # 降级计数只对存活的绑定（双侧失效的绑定整体计入 dropped，
            # 否则「绑定保留」类提示对已移除的绑定为假）
            if model is not None and model_kept is None:
                model_dropped += 1
            if kept is None and vendors:
                vendor_dropped += 1
            resolver.upsert(key, list(kept or ()), model=model_kept)
        else:
            dropped += 1
    return dropped, model_dropped, vendor_dropped


def _runtime_resets(
    old: _Generation | None,
    new: _Generation,
    dropped: int,
    model_dropped: int = 0,
    vendor_dropped: int = 0,
) -> list[str]:
    """切换后不再保留的旧 generation 运行时状态（中文，供 UI 展示）."""
    router = _router(old.app) if old is not None else None
    if old is None or router is None:
        return []

    def names(pred: Callable[[Any], bool]) -> str:
        return "、".join(t.name for t in router.tiers if pred(t))

    resets: list[str] = []
    if cb := names(
        lambda t: (
            t.circuit_breaker is not None
            and t.circuit_breaker.state is not CircuitState.CLOSED
        )
    ):
        resets.append(f"熔断器已重置：{cb}")
    if limited := names(lambda t: t.is_rate_limited):
        resets.append(f"限流冷却已清除：{limited}")
    if quota := names(
        lambda t: any(
            g is not None and g.enabled for g in (t.quota_guard, t.weekly_quota_guard)
        )
    ):
        resets.append(f"配额窗口已按用量数据库重建：{quota}")
    limits = _concurrency_limits(router)
    if overridden := "、".join(
        name
        for name, models in limits.items()
        if models != old.limits.get(name) and models != new.limits.get(name)
    ):
        resets.append(f"运行时调整的并发上限已恢复为配置值：{overridden}")
    order = router.get_vendor_names()
    if order != old.order and order != new.order:
        resets.append("运行时调整的优先级顺序已恢复为配置的 tiers")
    if dropped:
        resets.append(
            f"{dropped} 个 session 绑定的 vendor 与模型（如有）均不在新链路/清单中，"
            "绑定已移除"
        )
    if vendor_dropped:
        resets.append(
            f"{vendor_dropped} 个 session 绑定的 vendor 不在新链路中，"
            "vendor 绑定已移除（模型覆盖保留）"
        )
    if model_dropped:
        resets.append(
            f"{model_dropped} 个 session 绑定的模型不在新 bindable_models 清单中，"
            "模型覆盖已移除（vendor 绑定保留）"
        )
    return resets


__all__ = [
    "DEGRADED_ERROR",
    "GenerationActivationError",
    "PreparedGeneration",
    "GenerationManager",
    "create_degraded_app",
    "get_generation_manager",
    "bootstrap_settings",
]
