"""管理 API：``GET`` / ``PUT /api/config``（admin token 保护；密钥只写不读）.

- 鉴权：仅认 ``X-Coding-Proxy-Admin`` 请求头（不读 cookie / query），常量时间比较；
  配置的 token 无效时管理面锁定，所有请求 401。
- ``PUT``：worker 线程中执行 ``ConfigService.apply_changes``，preflight 为
  ``manager.build(cfg)``（结果暂存）；持久化成功后 ``await manager.activate(prepared)``。
  同一 manager 的「提交 + 启用」串行，启用顺序等于提交顺序；存储未变且新配置与运行中
  一致时不切换 generation（不重置熔断器等运行态）。无 generation manager（测试 / 未接入）
  时只持久化，返回 ``applied=false``。
- 响应只含字段路径与脱敏状态，从不回显输入值。
"""

from __future__ import annotations

import asyncio
import contextlib
import functools
import importlib
import json
import logging
import weakref
from collections.abc import Callable
from types import ModuleType
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

from ..config.home import load_or_create_admin_token
from ..config.importer import BootResult
from ..config.resolver import MODE_DEGRADED, MODE_LEGACY
from ..config.schema import ProxyConfig
from ..config.secrets import MasterKeyError
from ..config.service import (
    BOOTSTRAP_SECTIONS,
    EDITABLE_SECTIONS,
    ConfigService,
    ConfigStoreMissing,
    ConfigValidationError,
)
from ..config.store import ConfigStoreError, RevisionConflict, StoreBusyError
from .security import ADMIN_TOKEN_HEADER, admin_token_matches, is_json_media_type

logger = logging.getLogger(__name__)

RESTART_REQUIRED = "restart required"
_NO_STORE = {"Cache-Control": "no-store"}
_LEGACY_MSG = "当前以旧 YAML 只读运行（导入未通过等价校验），配置不可写入"
# 请求体 JSON 最大嵌套层数（合法配置不超过 10 层；过深会使递归校验溢出）
MAX_BODY_DEPTH = 64
# manager → 「提交 + 启用」串行锁（仅在事件循环线程访问）
_apply_locks: weakref.WeakKeyDictionary[Any, asyncio.Lock] = weakref.WeakKeyDictionary()
_process_apply_lock = asyncio.Lock()


# ── generation manager（2A 提供；模块缺失时视为无 manager） ──────────


@functools.cache
def _generations() -> ModuleType | None:
    name = f"{__package__}.generations"
    try:
        return importlib.import_module(name)
    except ModuleNotFoundError as exc:
        if exc.name != name:
            raise
        return None


def get_manager(app: Any) -> Any | None:
    """当前进程的 GenerationManager；未接入时返回 None."""
    module = _generations()
    if module is None:
        return getattr(app.state, "generation_manager", None)
    return module.get_generation_manager(app)


def in_flight(app: Any) -> int:
    """当前 generation 进行中的请求数（扣除发起本次查询的请求自身）；无 manager 时为 0.

    manager 的计数覆盖全部转发请求（含本 status 请求），UI 展示前需扣减自身，
    否则空闲代理也会显示 1 个在途请求。
    """
    manager = get_manager(app)
    if manager is None:
        return 0
    return max(0, int(manager.in_flight()) - 1)


def _boot(app: Any, manager: Any | None) -> BootResult | None:
    boot = getattr(manager, "boot", None) if manager is not None else None
    if boot is None:
        boot = getattr(app.state, "boot", None)
    return boot if isinstance(boot, BootResult) else None


def _serving_app(app: Any, manager: Any | None) -> Any:
    """当前对外服务的 generation app（无 manager 时即请求所在 app）."""
    current = getattr(manager, "current_app", None)
    return current if hasattr(current, "state") else app


def _is_degraded(app: Any) -> bool:
    """degraded app 不经 create_app 构建，没有 router."""
    return getattr(app.state, "router", None) is None


def _apply_lock(manager: Any) -> asyncio.Lock:
    try:
        lock = _apply_locks.get(manager)
        if lock is None:
            lock = _apply_locks[manager] = asyncio.Lock()
    except TypeError:  # manager 不支持弱引用：退化为进程级锁
        lock = _process_apply_lock
    return lock


def _up_to_date(result: Any, manager: Any, running_cfg: Any) -> bool:
    """运行中的 generation 已对应提交后的存储：revision 与有效配置（含密钥）均一致.

    比较时忽略 CLI ``-p/-h`` 对 server.host/port 的内存覆盖。
    """
    if result.changed_sections or not isinstance(running_cfg, ProxyConfig):
        return False
    running_revs = getattr(manager, "running_revisions", None) or {}
    if any(
        result.revisions.get(s, 0) != running_revs.get(s, 0)
        for s in {*result.revisions, *running_revs}
    ):
        return False
    new: ProxyConfig = result.config
    server = new.server.model_copy(
        update={"host": running_cfg.server.host, "port": running_cfg.server.port}
    )
    return new.model_copy(update={"server": server}) == running_cfg


def _activation_error(exc: Exception) -> str:
    module = _generations()
    error_cls = getattr(module, "GenerationActivationError", None)
    if error_cls is not None and isinstance(exc, error_cls) and str(exc):
        return str(exc)
    return f"新配置已保存但启用失败（{type(exc).__name__}），重启后生效"


# ── 响应工具 ────────────────────────────────────────────────────


def _json(status: int, content: dict) -> JSONResponse:
    return JSONResponse(status_code=status, content=content, headers=_NO_STORE)


def _configured_token() -> str:
    try:
        return load_or_create_admin_token()
    except OSError as exc:
        logger.warning("admin token 不可用，管理 API 已锁定: %s", type(exc).__name__)
        return ""


def _require_admin(request: Request) -> JSONResponse | None:
    provided = request.headers.get(ADMIN_TOKEN_HEADER)
    if provided and admin_token_matches(provided, _configured_token()):
        return None
    return _json(401, {"error": "admin_token_required"})


def _reject_constant(name: str) -> Any:
    raise ValueError(f"unsupported JSON constant {name}")


def _too_deep(value: Any, limit: int = MAX_BODY_DEPTH) -> bool:
    stack: list[tuple[Any, int]] = [(value, 1)]
    while stack:
        node, depth = stack.pop()
        if isinstance(node, dict):
            children: Any = node.values()
        elif isinstance(node, list):
            children = node
        else:
            continue
        if depth > limit:
            return True
        stack.extend((child, depth + 1) for child in children)
    return False


def _unique(errors: list[dict]) -> list[dict]:
    seen: set[str] = set()
    out: list[dict] = []
    for err in errors:
        key = json.dumps(err, sort_keys=True, ensure_ascii=False, default=str)
        if key not in seen:
            seen.add(key)
            out.append(err)
    return out


def _unavailable(exc: Exception) -> JSONResponse:
    """存储 / 主密钥不可用（消息只含路径与恢复提示）."""
    if isinstance(exc, MasterKeyError):
        return _json(503, {"error": "master_key_unavailable", "message": str(exc)})
    response = _json(503, {"error": "store_unavailable", "message": str(exc)})
    if isinstance(exc, StoreBusyError):
        response.headers["Retry-After"] = "1"
    return response


def _all_revisions(revisions: dict[str, int]) -> dict[str, int]:
    sections = {*EDITABLE_SECTIONS, *BOOTSTRAP_SECTIONS, *revisions}
    return {s: revisions.get(s, 0) for s in sorted(sections)}


def _running(app: Any, manager: Any | None, boot: BootResult | None) -> dict:
    """``view(running)`` 的运行态信息.

    mode / errors 反映当前对外服务的 generation：legacy_readonly 为进程级（重启前不变）；
    degraded 仅在服务中的仍是 degraded app 时覆盖并附启动时的错误（存储已修复但尚未
    启用时同样如实报告），启用新配置后由 view 依据当前存储判定。
    """
    serving = _serving_app(app, manager)
    router = getattr(serving.state, "router", None)
    running: dict[str, Any] = {
        "tiers": router.get_vendor_names() if router is not None else None,
        "boot_revisions": dict(getattr(manager, "running_revisions", None) or {}),
        "apply_supported": manager is not None,
    }
    if boot is not None:
        if boot.resolved.mode == MODE_LEGACY:
            running["mode"] = MODE_LEGACY
        running["notices"] = list(boot.notices)
        running["sources"] = dict(boot.resolved.sources)
    if running.get("mode") != MODE_LEGACY and _is_degraded(serving):
        running["mode"] = MODE_DEGRADED
        if boot is not None:
            running["errors"] = list(boot.resolved.errors)
    return running


# ── 路由 ────────────────────────────────────────────────────────


def install_admin_surface(
    app: FastAPI, *, service_factory: Callable[..., ConfigService] = ConfigService
) -> None:
    """注册 ``GET`` / ``PUT /api/config``（create_app 与 degraded app 共用）."""

    def _service(boot: BootResult | None) -> ConfigService:
        # 每请求新建（仅持有路径）；legacy_readonly 时 view 经 boot 返回导入错误
        return service_factory(boot=boot.resolved if boot is not None else None)

    @app.get("/api/config", include_in_schema=False)
    async def get_config(request: Request) -> JSONResponse:
        if (denied := _require_admin(request)) is not None:
            return denied
        manager = get_manager(request.app)
        boot = _boot(request.app, manager)
        running = _running(request.app, manager, boot)
        service = _service(boot)
        try:
            payload = await run_in_threadpool(service.view, running)
        except ConfigStoreError as exc:
            return _unavailable(exc)
        payload["errors"] = _unique(payload["errors"])
        return _json(200, payload)

    @app.put("/api/config", include_in_schema=False)
    async def put_config(request: Request) -> JSONResponse:
        if (denied := _require_admin(request)) is not None:
            return denied
        manager = get_manager(request.app)
        boot = _boot(request.app, manager)
        if boot is not None and boot.resolved.mode == MODE_LEGACY:
            return _json(423, {"error": "legacy_readonly", "message": _LEGACY_MSG})
        if not is_json_media_type(request.headers.get("content-type")):
            return _json(
                415,
                {
                    "error": {
                        "type": "invalid_request_error",
                        "message": "请求体必须为 application/json",
                    }
                },
            )
        try:
            payload = json.loads(await request.body(), parse_constant=_reject_constant)
        except (ValueError, RecursionError):
            return _json(422, {"errors": [{"loc": [], "msg": "请求体必须为合法 JSON"}]})
        if _too_deep(payload):
            msg = f"请求体嵌套超过 {MAX_BODY_DEPTH} 层"
            return _json(422, {"errors": [{"loc": [], "msg": msg}]})

        service = _service(boot)
        prepared: dict[str, Any] = {}

        def _preflight(cfg: Any) -> None:
            # 冲突重试时可能执行多次，只保留最后一次（未进入 lifespan，丢弃无副作用）
            prepared["generation"] = manager.build(cfg)

        applied, apply_error, reset_runtime = False, RESTART_REQUIRED, []
        serial = (
            _apply_lock(manager) if manager is not None else contextlib.nullcontext()
        )
        async with serial:
            try:
                result = await run_in_threadpool(
                    service.apply_changes,
                    payload,
                    actor="dashboard",
                    allow_bootstrap=False,
                    allow_env_any=False,
                    preflight=_preflight if manager is not None else None,
                )
            except RevisionConflict as exc:
                return _json(409, {"error": "conflict", "sections": exc.sections})
            except ConfigValidationError as exc:
                return _json(422, {"errors": exc.errors})
            except (ConfigStoreMissing, ConfigStoreError, MasterKeyError) as exc:
                return _unavailable(exc)

            if manager is not None and "generation" in prepared:
                running_cfg = getattr(
                    _serving_app(request.app, manager).state, "config", None
                )
                if _up_to_date(result, manager, running_cfg):
                    applied, apply_error = True, None
                else:
                    try:
                        summary = await manager.activate(
                            prepared["generation"], revisions=result.revisions
                        )
                    except Exception as exc:
                        apply_error = _activation_error(exc)
                        logger.warning(
                            "新配置启用失败（已持久化）: %s", type(exc).__name__
                        )
                    else:
                        applied, apply_error = True, None
                        reset_runtime = list((summary or {}).get("reset_runtime") or [])
        return _json(
            200,
            {
                "revisions": _all_revisions(result.revisions),
                "applied": applied,
                "apply_error": apply_error,
                "cleared_secrets": list(result.cleared_secrets),
                "reset_runtime": reset_runtime,
            },
        )


__all__ = [
    "RESTART_REQUIRED",
    "MAX_BODY_DEPTH",
    "install_admin_surface",
    "get_manager",
    "in_flight",
]
