"""``GET`` / ``PUT /api/config``：admin token、脱敏、乐观并发、错误形态、generation 接入."""

from __future__ import annotations

import asyncio
import json
import threading
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from coding.proxy.config import service as service_module
from coding.proxy.config.home import (
    admin_token_path,
    config_db_path,
    load_or_create_admin_token,
)
from coding.proxy.config.importer import (
    BootResult,
    ImportPlan,
    StoredConfigInvalidError,
    apply_import,
    ensure_store_ready,
    make_marker,
)
from coding.proxy.config.resolver import ResolvedConfig, resolve_config
from coding.proxy.config.schema import ProxyConfig
from coding.proxy.config.secrets import SecretCipher
from coding.proxy.config.service import BOOTSTRAP_MSG, ConfigService
from coding.proxy.config.store import ConfigStore, StoreBusyError
from coding.proxy.server import config_routes
from coding.proxy.server.app import create_app
from coding.proxy.server.security import ADMIN_TOKEN_HEADER, admin_token_matches

BASE = "http://127.0.0.1:3392"
PLANTED = "sk-PLANTED-api-secret-0123456789abcdef"
PLANTED_ENV = "env-PLANTED-zhipu-value-0123456789"
PLANTED_OAUTH = "oauth-PLANTED-refresh-0123456789"


@pytest.fixture
def token() -> str:
    ensure_store_ready()
    return load_or_create_admin_token()


@pytest.fixture
def app(token):
    return create_app(ProxyConfig(vendors=[{"vendor": "anthropic"}]))


@pytest.fixture
def client(app, strict_hosts):
    with TestClient(app, base_url=BASE) as c:
        yield c


def _auth(token: str) -> dict:
    return {ADMIN_TOKEN_HEADER: token}


def _view(client, token) -> dict:
    resp = client.get("/api/config", headers=_auth(token))
    assert resp.status_code == 200
    return resp.json()


def _put(client, token, payload: dict, **kw):
    return client.put("/api/config", headers=_auth(token), json=payload, **kw)


def _vendors_with(view: dict, vendor_type: str, **changes) -> list[dict]:
    vendors = view["values"]["vendors"]
    for vendor in vendors:
        if vendor["vendor"] == vendor_type:
            vendor.update(changes)
    return vendors


# ── admin token ─────────────────────────────────────────────────


class TestAdminToken:
    def test_missing_or_wrong_token(self, client, token):
        for headers in ({}, _auth("x" * 43), _auth(""), _auth(token + "x")):
            for method in ("GET", "PUT"):
                resp = client.request(
                    method, "/api/config", headers=headers, json={"sections": {}}
                )
                assert resp.status_code == 401, (method, headers)
                assert resp.json() == {"error": "admin_token_required"}

    def test_correct_token(self, client, token):
        assert client.get("/api/config", headers=_auth(token)).status_code == 200

    def test_token_in_query_or_cookie_ignored(self, client, token):
        resp = client.get("/api/config", params={ADMIN_TOKEN_HEADER: token})
        assert resp.status_code == 401
        resp = client.get("/api/config", params={"admin": token, "token": token})
        assert resp.status_code == 401
        client.cookies.set(ADMIN_TOKEN_HEADER, token)
        client.cookies.set("admin", token)
        assert client.get("/api/config").status_code == 401

    def test_invalid_configured_token_locks(self, client, monkeypatch):
        monkeypatch.setattr(config_routes, "load_or_create_admin_token", lambda: "")
        for value in ("", "x", "a" * 43):
            resp = client.get("/api/config", headers=_auth(value))
            assert resp.status_code == 401
        assert not admin_token_matches("", "")
        assert not admin_token_matches("short", "short")

    def test_token_file_unreadable_locks(self, client, monkeypatch):
        def _boom():
            raise PermissionError("denied")

        monkeypatch.setattr(config_routes, "load_or_create_admin_token", _boom)
        assert client.get("/api/config", headers=_auth("a" * 43)).status_code == 401

    def test_token_file_is_private(self, token):
        assert admin_token_path().stat().st_mode & 0o777 == 0o600

    def test_guards_apply_before_token(self, client, token):
        resp = client.get(
            "/api/config", headers={**_auth(token), "Host": "evil.example"}
        )
        assert resp.status_code == 403
        resp = client.get(
            "/api/config", headers={**_auth(token), "Sec-Fetch-Site": "cross-site"}
        )
        assert resp.status_code == 403


# ── GET 脱敏 ────────────────────────────────────────────────────


class TestMasking:
    def _plant(self, monkeypatch):
        service = ConfigService()
        service.set_secret("vendors.minimax.api_key", {"op": "set", "value": PLANTED})
        service.set_secret(
            "vendors.antigravity.refresh_token", {"op": "set", "value": PLANTED_OAUTH}
        )
        service.set_secret(
            "auth.google_client_secret", {"op": "set", "value": PLANTED_OAUTH}
        )
        monkeypatch.setenv("ZHIPU_API_KEY", PLANTED_ENV)

    def _assert_clean(self, text: str) -> None:
        for planted in (PLANTED, PLANTED_ENV, PLANTED_OAUTH):
            assert planted not in text

    def test_get_never_returns_plaintext(self, client, token, monkeypatch):
        self._plant(monkeypatch)
        resp = client.get("/api/config", headers=_auth(token))
        assert resp.status_code == 200
        assert resp.headers["cache-control"] == "no-store"
        self._assert_clean(resp.text)
        secrets = resp.json()["secrets"]
        assert secrets["vendors.minimax.api_key"]["kind"] == "stored"
        assert secrets["vendors.minimax.api_key"]["hint"] == "••••cdef"
        assert secrets["vendors.zhipu.api_key"]["kind"] == "env"
        assert secrets["vendors.zhipu.api_key"]["resolved"] is True
        # recorded：仅有存储记录的密钥才参与「目标地址变化 → 清空」
        assert secrets["vendors.minimax.api_key"]["recorded"] is True
        assert secrets["vendors.zhipu.api_key"]["recorded"] is False  # 包内默认推导
        assert secrets["auth.google_client_secret"]["recorded"] is True
        for vendor in resp.json()["values"]["vendors"]:
            assert "api_key" not in vendor and "refresh_token" not in vendor
        assert "google_client_secret" not in resp.json()["values"]["auth"]

    def test_error_bodies_never_echo_secrets(self, client, token, monkeypatch):
        self._plant(monkeypatch)
        view = _view(client, token)
        bodies = []
        # 未知密钥路径 + 非法值
        bodies.append(
            _put(
                client,
                token,
                {
                    "revisions": {"vendors": 0},
                    "sections": {
                        "vendors": _vendors_with(view, "zhipu", timeout_ms=PLANTED)
                    },
                    "secret_ops": {
                        "vendors.nope.api_key": {"op": "set", "value": PLANTED},
                        "vendors.kimi.api_key": {"op": "env", "var": PLANTED_ENV},
                    },
                },
            )
        )
        # 被拒的 bootstrap 写入
        bodies.append(
            _put(
                client,
                token,
                {"revisions": {"logging": 0}, "sections": {"logging": PLANTED}},
            )
        )
        # 冲突
        bodies.append(
            _put(
                client,
                token,
                {
                    "revisions": {"tiers": 7},
                    "sections": {"tiers": [PLANTED]},
                    "secret_ops": {
                        "vendors.kimi.api_key": {"op": "set", "value": PLANTED}
                    },
                },
            )
        )
        # 非法 JSON / 错误 token
        bodies.append(
            client.put(
                "/api/config",
                headers={**_auth(token), "Content-Type": "application/json"},
                content=f'{{"x": "{PLANTED}"'.encode(),
            )
        )
        bodies.append(
            client.put(
                "/api/config",
                headers=_auth("bad"),
                json={"secret_ops": {"vendors.kimi.api_key": PLANTED}},
            )
        )
        assert [r.status_code for r in bodies] == [422, 422, 409, 422, 401]
        for resp in bodies:
            self._assert_clean(resp.text)
            assert '"input"' not in resp.text


# ── PUT ─────────────────────────────────────────────────────────


class TestPut:
    def test_happy_path_persists_and_bumps_revision(self, client, token):
        view = _view(client, token)
        assert view["revisions"]["tiers"] == 0
        assert view["apply_supported"] is False
        assert view["running"]["tiers"] == ["anthropic"]

        resp = _put(
            client,
            token,
            {"revisions": {"tiers": 0}, "sections": {"tiers": ["zhipu", "anthropic"]}},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["applied"] is False
        assert body["apply_error"] == config_routes.RESTART_REQUIRED
        assert body["revisions"]["tiers"] == 1
        assert body["revisions"]["vendors"] == 0
        assert body["cleared_secrets"] == [] and body["reset_runtime"] == []

        assert resolve_config().config.tiers == ["zhipu", "anthropic"]
        view = _view(client, token)
        assert view["revisions"]["tiers"] == 1
        assert "tiers" in view["overridden"]

    def test_secret_set_and_cleared_by_target_change(self, client, token):
        resp = _put(
            client,
            token,
            {"secret_ops": {"vendors.zhipu.api_key": {"op": "set", "value": PLANTED}}},
        )
        assert resp.status_code == 200
        assert PLANTED.encode() not in config_db_path().read_bytes()
        view = _view(client, token)
        vendors = _vendors_with(view, "zhipu", base_url="https://other.example")
        resp = _put(
            client,
            token,
            {
                "revisions": {"vendors": view["revisions"]["vendors"]},
                "sections": {"vendors": vendors},
            },
        )
        assert resp.status_code == 200
        assert resp.json()["cleared_secrets"] == ["vendors.zhipu.api_key"]

    def test_stale_revision_conflict(self, client, token):
        payload = {"revisions": {"tiers": 0}, "sections": {"tiers": ["zhipu"]}}
        assert _put(client, token, payload).status_code == 200
        resp = _put(client, token, payload)
        assert resp.status_code == 409
        assert resp.json() == {"error": "conflict", "sections": ["tiers"]}

    def test_validation_loc_mapping_without_input(self, client, token):
        view = _view(client, token)
        vendors = _vendors_with(view, "zhipu", timeout_ms="PLANTED-not-int")
        resp = _put(
            client,
            token,
            {"revisions": {"vendors": 0}, "sections": {"vendors": vendors}},
        )
        assert resp.status_code == 422
        errors = resp.json()["errors"]
        assert ["vendors", "zhipu", "timeout_ms"] in [e["loc"] for e in errors]
        assert all(set(e) == {"loc", "msg"} for e in errors)
        assert "PLANTED" not in resp.text

    def test_vendor_inline_secret_rejected_like_other_sections(self, client, token):
        """vendors 内联密钥与非 vendors section 一致：422 拒绝而非静默丢弃."""
        view = _view(client, token)
        vendors = _vendors_with(view, "zhipu", api_key=PLANTED)
        resp = _put(
            client,
            token,
            {"revisions": {"vendors": 0}, "sections": {"vendors": vendors}},
        )
        assert resp.status_code == 422
        assert resp.json()["errors"] == [
            {
                "loc": ["vendors", "zhipu", "api_key"],
                "msg": "密钥请使用 secret_ops / `config set-secret`",
            }
        ]
        assert PLANTED not in resp.text
        assert PLANTED.encode() not in config_db_path().read_bytes()

    def test_bootstrap_sections_rejected(self, client, token):
        for section in ("server", "logging", "database", "auth", "primary"):
            resp = _put(
                client, token, {"revisions": {section: 0}, "sections": {section: {}}}
            )
            assert resp.status_code == 422
            assert resp.json() == {"errors": [{"loc": [section], "msg": BOOTSTRAP_MSG}]}

    def test_env_binding_rejected_over_http(self, client, token):
        resp = _put(
            client,
            token,
            {"secret_ops": {"vendors.kimi.api_key": {"op": "env", "var": "HOME"}}},
        )
        assert resp.status_code == 422
        assert resp.json()["errors"][0]["loc"] == ["secret_ops", "vendors.kimi.api_key"]

    def test_requires_json_content_type(self, client, token):
        body = json.dumps({"sections": {}}).encode()
        for ctype in ("text/plain", "application/x-www-form-urlencoded"):
            resp = client.put(
                "/api/config",
                headers={**_auth(token), "Content-Type": ctype},
                content=body,
            )
            assert resp.status_code == 415
        resp = client.put("/api/config", headers=_auth(token))  # 无 body / 无类型
        assert resp.status_code == 415

    def test_malformed_json_and_non_object(self, client, token):
        headers = {**_auth(token), "Content-Type": "application/json"}
        for raw in (b"{", b"NaN", b'{"revisions": {"tiers": Infinity}}'):
            resp = client.put("/api/config", headers=headers, content=raw)
            assert resp.status_code == 422
        resp = client.put("/api/config", headers=headers, content=b"[1]")
        assert resp.status_code == 422
        assert resp.json()["errors"][0]["loc"] == []

    def test_deeply_nested_body_is_422(self, client, token):
        headers = {**_auth(token), "Content-Type": "application/json"}
        limit = config_routes.MAX_BODY_DEPTH
        for depth in (limit, 2000):
            nested = "[" * depth + "]" * depth
            raw = f'{{"sections": {{"model_mapping": {nested}}}}}'
            resp = client.put("/api/config", headers=headers, content=raw.encode())
            assert resp.status_code == 422, depth
            assert set(resp.json()) == {"errors"}
        assert resp.json()["errors"] == [
            {"loc": [], "msg": f"请求体嵌套超过 {limit} 层"}
        ]
        assert ConfigService().revision("model_mapping") == 0

    def test_legacy_readonly_returns_423(self, app, client, token):
        cfg = ProxyConfig(vendors=[{"vendor": "anthropic"}])
        app.state.boot = BootResult(
            resolved=ResolvedConfig(
                config=cfg,
                effective_raw={},
                mode="legacy_readonly",
                errors=[{"loc": ["tiers"], "msg": "import mismatch"}],
            ),
            notices=["legacy notice"],
        )
        resp = _put(client, token, {"revisions": {"tiers": 0}, "sections": {}})
        assert resp.status_code == 423
        assert resp.json()["error"] == "legacy_readonly"
        view = _view(client, token)
        assert view["mode"] == "legacy_readonly"
        assert view["notices"] == ["legacy notice"]
        assert {"loc": ["tiers"], "msg": "import mismatch"} in view["errors"]

    def test_degraded_generation_reported_until_activated(self, app, client, token):
        """degraded 启动后存储被 CLI 修复但未启用：GET 仍报告 degraded 与启动错误."""
        boot_error = {"loc": ["vendors", "zhipu", "timeout_ms"], "msg": "boot error"}
        boot = BootResult(
            resolved=ResolvedConfig(
                config=None, effective_raw={}, mode="degraded", errors=[boot_error]
            ),
            notices=["n1"],
        )
        degraded = FastAPI()
        degraded.state.boot = boot
        manager = _FakeManager(degraded)
        manager.boot = boot
        app.state.generation_manager = manager

        view = _view(client, token)
        assert view["mode"] == "degraded"
        assert view["errors"] == [boot_error]
        assert view["running"]["tiers"] is None
        assert view["notices"] == ["n1"]

        resp = _put(client, token, {})
        assert resp.json()["applied"] is True and len(manager.activated) == 1
        view = _view(client, token)
        assert view["mode"] == "store"
        assert view["errors"] == []

    def test_store_busy_maps_to_503(self, client, token, monkeypatch):
        def _busy(self, *a, **kw):
            raise StoreBusyError(config_db_path())

        monkeypatch.setattr(ConfigService, "view", _busy)
        resp = client.get("/api/config", headers=_auth(token))
        assert resp.status_code == 503
        assert resp.headers["retry-after"] == "1"
        assert resp.json()["error"] == "store_unavailable"


# ── generation manager 接入 ─────────────────────────────────────


class _FakeRouter:
    def __init__(self, cfg: ProxyConfig) -> None:
        self._names = list(cfg.tiers or [v.vendor for v in cfg.vendors])

    def get_vendor_names(self) -> list[str]:
        return self._names


class _FakePrepared:
    """模拟 PreparedGeneration：携带（pin 后的）配置供 no-op 判定比较."""

    def __init__(self, cfg: ProxyConfig) -> None:
        self.config = cfg


class _FakeManager:
    """模拟 2A GenerationManager：activate 成功后切换 current_app 并同步 revision."""

    def __init__(self, app=None, *, fail_build=False, fail_activate=False):
        self.fail_build = fail_build
        self.fail_activate = fail_activate
        self.built: list[ProxyConfig] = []
        self.activated: list[object] = []
        self.running_revisions = {"tiers": 0}
        self.boot = None
        self.current_app = app

    def build(self, cfg):
        if self.fail_build:
            raise RuntimeError(PLANTED)
        self.built.append(cfg)
        return _FakePrepared(cfg)

    async def activate(self, prepared, *, revisions=None):
        if self.fail_activate:
            raise RuntimeError(PLANTED)
        self.activated.append(prepared)
        cfg = prepared.config
        self.current_app = FastAPI()
        self.current_app.state.config = cfg
        self.current_app.state.router = _FakeRouter(cfg)
        self.running_revisions = dict(ConfigService().store.snapshot().revisions)
        return {"reset_runtime": ["熔断器状态将重置"]}

    def in_flight(self) -> int:
        return 3


class TestGenerationManager:
    def _install(self, app, **kw) -> _FakeManager:
        manager = _FakeManager(app, **kw)
        app.state.generation_manager = manager
        return manager

    def test_put_builds_then_activates(self, app, client, token):
        manager = self._install(app)
        view = _view(client, token)
        assert view["apply_supported"] is True
        assert view["running"]["boot_revisions"] == {"tiers": 0}
        resp = _put(
            client, token, {"revisions": {"tiers": 0}, "sections": {"tiers": ["zhipu"]}}
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["applied"] is True and body["apply_error"] is None
        assert body["reset_runtime"] == ["熔断器状态将重置"]
        assert manager.built[-1].tiers == ["zhipu"]
        assert len(manager.activated) == len(manager.built)
        assert manager.activated[-1].config is manager.built[-1]

    def test_build_failure_is_422_and_not_persisted(self, app, client, token):
        self._install(app, fail_build=True)
        resp = _put(
            client, token, {"revisions": {"tiers": 0}, "sections": {"tiers": ["zhipu"]}}
        )
        assert resp.status_code == 422
        assert PLANTED not in resp.text
        assert ConfigService().revision("tiers") == 0

    def test_activation_failure_persists(self, app, client, token):
        self._install(app, fail_activate=True)
        resp = _put(
            client, token, {"revisions": {"tiers": 0}, "sections": {"tiers": ["zhipu"]}}
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["applied"] is False
        assert body["apply_error"] and PLANTED not in body["apply_error"]
        assert ConfigService().revision("tiers") == 1

    def test_noop_put_does_not_swap_generation(self, app, client, token):
        """空 PUT / 原值重提交 / 只 keep 密钥：不切换 generation、不重置运行态."""
        manager = self._install(app)
        resp = _put(
            client, token, {"revisions": {"tiers": 0}, "sections": {"tiers": ["zhipu"]}}
        )
        assert resp.status_code == 200 and len(manager.activated) == 1
        view = _view(client, token)
        for payload in (
            {},
            {"sections": {}},
            {
                "revisions": {"tiers": view["revisions"]["tiers"]},
                "sections": {"tiers": view["values"]["tiers"]},
            },
            {"secret_ops": {"vendors.zhipu.api_key": {"op": "keep"}}},
        ):
            resp = _put(client, token, payload)
            assert resp.status_code == 200, payload
            body = resp.json()
            assert body["applied"] is True and body["apply_error"] is None
            assert body["reset_runtime"] == []
        assert len(manager.activated) == 1

    def test_secret_only_change_still_activates(self, app, client, token):
        manager = self._install(app)
        resp = _put(
            client,
            token,
            {"secret_ops": {"vendors.zhipu.api_key": {"op": "set", "value": PLANTED}}},
        )
        assert resp.status_code == 200
        assert resp.json()["applied"] is True
        assert len(manager.activated) == 1
        assert PLANTED not in resp.text

    def test_store_ahead_of_running_still_activates(self, app, client, token):
        """存储经 CLI 改动但尚未启用时，空 PUT 也会启用最新存储（running 追上 store）."""
        manager = self._install(app)
        manager.current_app.state.config = ProxyConfig(vendors=[{"vendor": "zhipu"}])
        assert _put(client, token, {}).json()["applied"] is True
        assert len(manager.activated) == 1

    def test_concurrent_puts_activate_in_commit_order(self, app, token, strict_hosts):
        """首个提交者在提交后被拖慢时，后提交者不得抢先启用（运行配置 == 存储）."""
        manager = self._install(app)
        original_info = service_module.logger.info
        gate = threading.Event()
        first = threading.Lock()

        def slow_info(*args, **kwargs):
            if first.acquire(blocking=False):
                gate.wait(timeout=5)
            return original_info(*args, **kwargs)

        async def scenario():
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url=BASE) as ac:
                put_a = asyncio.create_task(
                    ac.put(
                        "/api/config",
                        headers=_auth(token),
                        json={
                            "revisions": {"tiers": 0},
                            "sections": {"tiers": ["zhipu"]},
                        },
                    )
                )
                for _ in range(200):
                    if ConfigService().revision("tiers") == 1:
                        break
                    await asyncio.sleep(0.01)
                put_b = asyncio.create_task(
                    ac.put(
                        "/api/config",
                        headers=_auth(token),
                        json={
                            "revisions": {"tiers": 1},
                            "sections": {"tiers": ["anthropic"]},
                        },
                    )
                )
                await asyncio.sleep(0.2)
                assert not put_b.done()  # 串行：等待 A 启用完成
                gate.set()
                return await put_a, await put_b

        service_module.logger.info = slow_info
        try:
            resp_a, resp_b = asyncio.run(scenario())
        finally:
            service_module.logger.info = original_info
        assert [resp_a.status_code, resp_b.status_code] == [200, 200]
        assert resp_b.json()["revisions"]["tiers"] == 2
        assert [p.config.tiers for p in manager.activated] == [["zhipu"], ["anthropic"]]
        assert manager.current_app.state.config.tiers == resolve_config().config.tiers

    def test_status_in_flight(self, app, client):
        assert client.get("/api/status").json()["in_flight"] == 0
        manager = self._install(app)
        # in_flight 扣除发起查询的请求自身：空闲代理（仅本请求在途）显示 0
        manager.in_flight = lambda: 1
        assert client.get("/api/status").json()["in_flight"] == 0
        manager.in_flight = lambda: 3
        assert client.get("/api/status").json()["in_flight"] == 2


# ── create_app ──────────────────────────────────────────────────


class TestCreateApp:
    def test_no_legacy_yaml_fallback(self):
        # 旧搜索链会读取 ./config.yaml；create_app() 只读 config.db。
        # cwd 存在待迁移 YAML 时 ensure_store_ready 会拦截，这里直接初始化空 store
        Path("config.yaml").write_text("server:\n  port: 4567\n")
        apply_import(
            ConfigStore(config_db_path()),
            SecretCipher(),
            ImportPlan(source="", source_sha256=""),
            make_marker(None, None),
            replace=False,
        )
        app = create_app()
        assert app.state.config.server.port == 3392
        assert app.docs_url is None and app.openapi_url is None

    def test_invalid_store_raises(self):
        ensure_store_ready()
        with ConfigStore(config_db_path()).transaction() as txn:
            txn.put_section("server", {"port": "PLANTED-not-int"})
        with pytest.raises(StoredConfigInvalidError) as exc_info:
            create_app()
        assert "server.port" in str(exc_info.value)
        assert "PLANTED" not in str(exc_info.value)
