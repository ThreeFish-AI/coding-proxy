"""GenerationManager × 真实 app/store：PUT /api/config 热切换、会话绑定、degraded、uvicorn."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx
import pytest
import uvicorn
from fastapi import FastAPI

from coding.proxy.auth.providers.github import GitHubDeviceFlowProvider
from coding.proxy.config.home import config_db_path, load_or_create_admin_token
from coding.proxy.config.importer import boot, ensure_store_ready
from coding.proxy.config.service import ConfigService
from coding.proxy.config.store import ConfigStore
from coding.proxy.model.auth import ProviderTokens
from coding.proxy.server.app import create_app
from coding.proxy.server.generations import GenerationManager
from coding.proxy.server.security import ADMIN_TOKEN_HEADER
from tests.test_generations import running_lifespan

BASE = "http://127.0.0.1:3392"
PLANTED = "sk-PLANTED-lifespan-secret-0123456789"
DEFAULT_ORDER = ["zhipu", "anthropic", "copilot"]


async def _make_manager(**kw) -> GenerationManager:
    ensure_store_ready()
    return GenerationManager(boot=boot(None), **kw)


def _client(app) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url=BASE, timeout=30
    )


@asynccontextmanager
async def serving(**kw):
    manager = await _make_manager(**kw)
    async with running_lifespan(manager):
        async with _client(manager) as client:
            yield manager, client


def _auth(token: str) -> dict:
    return {ADMIN_TOKEN_HEADER: token}


async def _status_order(client) -> list[str]:
    data = (await client.get("/api/status")).json()
    return [t["name"] for t in data["tiers"]]


async def _put_tiers(client, token, tiers, section="tiers", reset=False):
    view = (await client.get("/api/config", headers=_auth(token))).json()
    body = {"revisions": {"tiers": view["revisions"]["tiers"]}}
    body["reset" if reset else "sections"] = ["tiers"] if reset else {"tiers": tiers}
    return await client.put("/api/config", headers=_auth(token), json=body)


async def _wait_closed(app: FastAPI, timeout: float = 10) -> None:
    """等待旧 generation lifespan 退出（token logger 关闭、vendor client 关闭）."""
    async with asyncio.timeout(timeout):
        while app.state.token_logger._db is not None:
            await asyncio.sleep(0.01)


# ── 端到端：PUT 后无需重启即生效 ─────────────────────────────────


async def test_put_tiers_changes_status_without_restart():
    token = load_or_create_admin_token()
    async with serving() as (manager, client):
        assert await _status_order(client) == DEFAULT_ORDER
        old_app = manager.current_app
        old_client = old_app.state.router.tiers[0].vendor._get_client()

        resp = await _put_tiers(client, token, ["anthropic", "zhipu", "copilot"])
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["applied"] is True and body["apply_error"] is None
        assert "配额窗口已按用量数据库重建" in "".join(body["reset_runtime"])

        assert await _status_order(client) == ["anthropic", "zhipu", "copilot"]
        assert manager.current_app is not old_app
        assert manager.running_revisions == dict(
            ConfigStore(config_db_path()).snapshot().revisions
        )
        await _wait_closed(old_app)  # 排空后旧 generation 关闭
        assert old_client.is_closed  # 旧 vendor client 随 lifespan 退出关闭


@pytest.mark.parametrize("bootstrap_drift", [False, True], ids=["aligned", "drift"])
async def test_noop_put_does_not_swap_generation(bootstrap_drift):
    """空 PUT 不换代；CLI 改过 bootstrap 字段（待重启）后，追平存储激活一次即止."""
    token = load_or_create_admin_token()
    async with serving() as (manager, client):
        resp = await client.put(
            "/api/config",
            headers=_auth(token),
            json={"revisions": {"tiers": 0}, "sections": {}},
        )
        assert resp.json()["applied"] is True
        if bootstrap_drift:
            await asyncio.to_thread(
                ConfigService().set_path, "logging.level", "DEBUG"
            )  # CLI 修改 bootstrap 字段，提示需重启但未重启
            # 第一次空保存：revision 追平存储，激活一次（合理）
            resp = await client.put("/api/config", headers=_auth(token), json={})
            assert resp.json()["applied"] is True
        current = manager.current_app
        for _ in range(2):
            resp = await client.put("/api/config", headers=_auth(token), json={})
            body = resp.json()
            assert body["applied"] is True and body["reset_runtime"] == []
            # pin 面已吸收 bootstrap 漂移：等价保存不再整代热切换
            assert manager.current_app is current


# ── 流式请求跨切换（真实 app）────────────────────────────────────


async def test_stream_completes_on_old_generation_real_app():
    token = load_or_create_admin_token()
    gate = asyncio.Event()

    async with serving() as (manager, client):
        old_app = manager.current_app
        router = old_app.state.router
        # 让旧 generation 的流式代理可控（不访问上游）
        stream_started = asyncio.Event()

        async def fake_stream(body, headers) -> AsyncIterator[tuple[bytes, str]]:
            stream_started.set()
            yield b"event: start\n", "zhipu"
            await gate.wait()
            yield b"event: end\n", "zhipu"

        router.route_stream = fake_stream  # type: ignore[method-assign]
        # 制造一个进行中的 vendor client，验证排空后才关闭
        tier = router.tiers[0]
        client_holder = tier.vendor._get_client()

        call = asyncio.create_task(client.post("/v1/messages", json={"stream": True}))
        await asyncio.wait_for(stream_started.wait(), 10)
        assert manager.in_flight() == 1

        await _put_tiers(client, token, ["anthropic", "zhipu", "copilot"])
        assert manager.current_app is not old_app
        assert not client_holder.is_closed  # 旧流未结束，不关闭

        gate.set()
        resp = await asyncio.wait_for(call, 10)
        assert resp.status_code == 200
        assert b"event: end" in resp.content  # 旧流完成
        await _wait_closed(old_app)
        assert client_holder.is_closed  # 排空后 vendor client 已关闭


# ── 启用失败 ─────────────────────────────────────────────────────


async def test_lifespan_failure_through_put_keeps_old_generation(caplog):
    token = load_or_create_admin_token()
    fail = False

    def build(cfg):
        app = create_app(cfg)
        if fail:
            original = app.router.lifespan_context

            from contextlib import asynccontextmanager

            @asynccontextmanager
            async def failing(a):
                async with original(a):
                    raise RuntimeError(PLANTED)
                yield  # noqa: B904 - 使 failing 成为 async generator

            app.router.lifespan_context = failing
        return app

    async with serving(build=build) as (manager, client):
        assert await _status_order(client) == DEFAULT_ORDER
        fail = True
        with caplog.at_level("DEBUG"):
            resp = await _put_tiers(client, token, ["anthropic", "zhipu", "copilot"])
        body = resp.json()
        assert body["applied"] is False
        assert body["apply_error"] and PLANTED not in body["apply_error"]
        assert await _status_order(client) == DEFAULT_ORDER  # 旧 generation 继续服务
        # 已保存（revision 前进），下次 save/重启后生效
        assert body["revisions"]["tiers"] >= 1
        assert PLANTED not in caplog.text


async def test_build_failure_through_put_is_422_and_not_persisted():
    token = load_or_create_admin_token()

    def build(cfg):
        raise RuntimeError(PLANTED)

    async with serving(build=build) as (manager, client):
        old_app = manager.current_app
        view = (await client.get("/api/config", headers=_auth(token))).json()
        resp = await client.put(
            "/api/config",
            headers=_auth(token),
            json={
                "revisions": {"tiers": view["revisions"]["tiers"]},
                "sections": {"tiers": ["anthropic", "zhipu", "copilot"]},
            },
        )
        assert resp.status_code == 422
        assert PLANTED not in resp.text
        assert manager.current_app is old_app
        now = (await client.get("/api/config", headers=_auth(token))).json()
        assert now["revisions"]["tiers"] == view["revisions"]["tiers"]  # 未持久化


# ── session 运行时绑定迁移 ───────────────────────────────────────


async def test_session_runtime_binding_survives_swap_and_drop_is_reported():
    token = load_or_create_admin_token()
    async with serving() as (manager, client):
        for key, vendors in (("s1", ["anthropic"]), ("s2", ["copilot"])):
            resp = await client.put(
                "/api/session-vendor",
                json={"session_key": key, "vendors": vendors},
            )
            assert resp.status_code == 200, resp.text

        resp = await _put_tiers(client, token, ["zhipu", "anthropic"])  # copilot 出链
        body = resp.json()
        assert body["applied"] is True
        assert any("绑定已移除" in note for note in body["reset_runtime"])

        bindings = {
            b["session_key"]: b["vendors"]
            for b in (await client.get("/api/session-vendor")).json()["bindings"]
        }
        assert bindings == {"s1": ["anthropic"]}  # s2 的 vendor 不在新链路，丢弃


# ── degraded：存储配置无效 ────────────────────────────────────────


async def test_degraded_boot_serves_admin_and_recovers_via_put():
    ensure_store_ready()
    with ConfigStore(config_db_path()).transaction() as txn:
        txn.put_section("tiers", ["no-such-vendor"])
    booted = boot(None)
    assert booted.resolved.config is None  # degraded

    manager = GenerationManager(boot=booted)
    token = load_or_create_admin_token()
    async with running_lifespan(manager):
        async with _client(manager) as client:
            resp = await client.post("/v1/messages", json={})
            assert resp.status_code == 503
            assert resp.json() == {
                "error": {
                    "type": "config_invalid",
                    "message": "存储的配置无效，请在 Dashboard → Config 修复",
                }
            }
            assert (
                await client.post("/api/gemini/v1beta/models", json={})
            ).status_code == 503
            assert (await client.get("/health")).status_code == 200
            assert (await client.get("/dashboard")).status_code == 200

            view = (await client.get("/api/config", headers=_auth(token))).json()
            assert view["mode"] == "degraded"
            assert view["running"]["tiers"] is None
            assert view["apply_supported"] is True

            # 在 Config 页修复（reset tiers section）
            resp = await _put_tiers(client, token, None, reset=True)
            assert resp.json()["applied"] is True
            assert await _status_order(client) == DEFAULT_ORDER
            view = (await client.get("/api/config", headers=_auth(token))).json()
            assert view["mode"] == "store" and view["errors"] == []


async def test_degraded_recovery_pins_database_against_later_cli_change(tmp_path):
    """degraded 启动修复后，CLI 修改 database（需重启生效）不得被下次 Dashboard 保存在线生效."""
    ensure_store_ready()
    with ConfigStore(config_db_path()).transaction() as txn:
        txn.put_section("tiers", ["no-such-vendor"])
    manager = GenerationManager(boot=boot(None))
    token = load_or_create_admin_token()
    async with running_lifespan(manager):
        async with _client(manager) as client:
            assert (await _put_tiers(client, token, None, reset=True)).json()["applied"]
            running_db = manager.current_app.state.config.database.path

            other = str(tmp_path / "other-usage.db")
            await asyncio.to_thread(ConfigService().set_path, "database.path", other)
            resp = await _put_tiers(client, token, ["anthropic", "zhipu", "copilot"])
            assert resp.json()["applied"] is True
            assert await _status_order(client) == ["anthropic", "zhipu", "copilot"]
            assert manager.current_app.state.config.database.path == running_db
    assert not (tmp_path / "other-usage.db").exists()


# ── 运行时重认证跨切换 ───────────────────────────────────────────


def _copilot_tm(app: FastAPI):
    tier = next(t for t in app.state.router.tiers if t.name == "copilot")
    return tier.vendor._token_manager


async def test_pending_reauth_is_handed_over_to_new_generation(monkeypatch):
    """切换时仍在进行的 device flow：新 generation 报 pending、不重复登录，完成后拿到新凭证."""
    fake_new = "gho_TEST_new_device_flow_token_0000000000"  # 测试假值
    started, release = asyncio.Event(), asyncio.Event()
    logins: list[int] = []

    async def fake_login(self) -> ProviderTokens:
        logins.append(1)
        started.set()
        await release.wait()
        return ProviderTokens(access_token=fake_new)

    monkeypatch.setattr(GitHubDeviceFlowProvider, "login", fake_login)
    token = load_or_create_admin_token()
    async with serving() as (manager, client):
        old_app = manager.current_app
        assert (await client.post("/api/reauth/github")).status_code == 202
        await asyncio.wait_for(started.wait(), 10)

        resp = await _put_tiers(client, token, ["anthropic", "zhipu", "copilot"])
        assert resp.json()["applied"] is True
        new_app = manager.current_app
        assert new_app is not old_app
        status = (await client.get("/api/reauth/status")).json()["providers"]
        assert status["github"]["state"] == "pending"
        # 新 generation 上再次触发：登录已在进行，不再打开第二个浏览器登录
        assert (await client.post("/api/reauth/github")).status_code == 202
        await _wait_closed(old_app)  # 旧 generation 照常排空关闭

        release.set()
        async with asyncio.timeout(10):
            while (await client.get("/api/reauth/status")).json()["providers"][
                "github"
            ]["state"] != "completed":
                await asyncio.sleep(0.01)
        assert _copilot_tm(new_app)._github_token == fake_new
        assert (
            new_app.state.reauth_coordinator._token_store.get("github").access_token
            == fake_new
        )
        assert len(logins) == 1


async def test_reauth_finishing_on_retired_generation_updates_later_generations(
    monkeypatch,
):
    """登录跨越多次切换：凭证写入最终的当前 generation，交接 task 不遗留."""
    fake_new = "gho_TEST_new_device_flow_token_1111111111"
    started, release = asyncio.Event(), asyncio.Event()

    async def fake_login(self) -> ProviderTokens:
        started.set()
        await release.wait()
        return ProviderTokens(access_token=fake_new)

    monkeypatch.setattr(GitHubDeviceFlowProvider, "login", fake_login)
    token = load_or_create_admin_token()
    async with serving() as (manager, client):
        assert (await client.post("/api/reauth/github")).status_code == 202
        await asyncio.wait_for(started.wait(), 10)
        for order in (["anthropic", "zhipu", "copilot"], ["copilot", "zhipu"]):
            assert (await _put_tiers(client, token, order)).json()["applied"] is True
        status = (await client.get("/api/reauth/status")).json()["providers"]
        assert status["github"]["state"] == "pending"
        release.set()
        async with asyncio.timeout(10):
            while manager._reauth_relays:
                await asyncio.sleep(0.01)
        assert _copilot_tm(manager.current_app)._github_token == fake_new
        status = (await client.get("/api/reauth/status")).json()["providers"]
        assert status["github"]["state"] == "completed"


# ── 启动期 lifespan 失败 ─────────────────────────────────────────


async def test_unwritable_database_path_boots_degraded(tmp_path, caplog):
    blocker = tmp_path / "planted-blocker"
    blocker.write_text("")  # 父路径是普通文件：mkdir 必然失败（root 下同样成立）
    ensure_store_ready()
    with ConfigStore(config_db_path()).transaction() as txn:
        txn.put_section("database", {"path": str(blocker / "sub" / "usage.db")})
    booted = boot(None)
    assert booted.resolved.config is not None
    manager = GenerationManager(boot=booted)
    token = load_or_create_admin_token()
    with caplog.at_level("DEBUG"):
        async with running_lifespan(manager):
            async with _client(manager) as client:
                assert (await client.post("/v1/messages", json={})).status_code == 503
                view = (await client.get("/api/config", headers=_auth(token))).json()
    assert view["mode"] == "degraded"
    assert any("运行时启动失败" in e["msg"] for e in view["errors"])
    assert str(blocker) not in json.dumps(view["errors"], ensure_ascii=False)
    assert str(blocker) not in caplog.text


# ── 真实 uvicorn（随机端口）──────────────────────────────────────


async def test_real_uvicorn_serves_manager_and_hot_swaps():
    manager = await _make_manager()
    token = load_or_create_admin_token()
    server = uvicorn.Server(
        uvicorn.Config(
            manager,
            host="127.0.0.1",
            port=0,  # 随机端口（绝不触碰 3392）
            lifespan="on",
            proxy_headers=False,
            log_config=None,
            access_log=False,
        )
    )
    serve = asyncio.create_task(server.serve())
    try:
        for _ in range(500):
            if server.started:
                break
            await asyncio.sleep(0.02)
        assert server.started
        port = server.servers[0].sockets[0].getsockname()[1]
        first_app = manager.current_app

        async with httpx.AsyncClient(
            base_url=f"http://127.0.0.1:{port}", timeout=30, trust_env=False
        ) as client:
            assert await _status_order(client) == DEFAULT_ORDER
            resp = await _put_tiers(client, token, ["anthropic", "zhipu", "copilot"])
            assert resp.json()["applied"] is True
            assert await _status_order(client) == ["anthropic", "zhipu", "copilot"]

        await _wait_closed(first_app)
    finally:
        server.should_exit = True
        await asyncio.wait_for(serve, 15)
    assert manager.current_app is not first_app
