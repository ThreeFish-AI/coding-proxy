"""全局测试隔离.

- HOME / CODING_PROXY_HOME / CWD 指向每个测试独立的临时目录；
- 清理凭证类环境变量（GITHUB_TOKEN、*_API_KEY、GOOG_*、NATIVE_*_BASE_URL、
  ANTIGRAVITY_GATEWAY_BASE_URL、CODING_PROXY_MASTER_KEY），避免宿主环境影响断言；
- audit hook 守护：测试期间任何对真实 ``~/.coding-proxy`` 的文件访问都会立即失败；
- 管理面 Host 白名单临时加入 TestClient / httpx 默认主机 ``testserver`` / ``test``
  （仅测试；安全用例经 ``strict_hosts`` fixture 还原）。

e2e 用例（``-m e2e``，显式读取真实凭证）不做隔离与守护。
"""

from __future__ import annotations

import os
import sys
from urllib.parse import unquote, urlsplit

import pytest

_REAL_ROOTS: tuple[str, ...] = tuple(
    {
        os.path.abspath(os.path.expanduser("~/.coding-proxy")),
        *(
            [os.path.abspath(os.path.expanduser(os.environ["CODING_PROXY_HOME"]))]
            if os.environ.get("CODING_PROXY_HOME", "").strip()
            else []
        ),
    }
)
# 事件 → 参数中路径的个数（rename / replace 等同时检查源与目标）
_GUARDED_EVENTS: dict[str, int] = {
    "open": 1,
    "os.mkdir": 1,
    "os.rmdir": 1,
    "os.remove": 1,
    "os.chmod": 1,
    "os.chown": 1,
    "os.listdir": 1,
    "os.scandir": 1,
    "os.truncate": 1,
    "os.utime": 1,
    "os.rename": 2,
    "os.replace": 2,
    "os.link": 2,
    "os.symlink": 2,
    "shutil.copyfile": 2,
    "shutil.rmtree": 1,
    "sqlite3.connect": 1,
}
_guard_active = [True]
_violations: list[str] = []


def _as_abspath(arg: object) -> str | None:
    if isinstance(arg, int):
        return None
    try:
        path = os.fspath(arg)  # type: ignore[arg-type]
    except TypeError:
        return None
    if isinstance(path, bytes):
        path = os.fsdecode(path)
    if path.startswith("file:"):
        path = unquote(urlsplit(path).path)
    return os.path.abspath(path) if path else None


def _audit(event: str, args: tuple) -> None:
    count = _GUARDED_EVENTS.get(event)
    if not count or not _guard_active[0]:
        return
    for arg in args[:count]:
        path = _as_abspath(arg)
        if path and any(path == r or path.startswith(r + os.sep) for r in _REAL_ROOTS):
            _violations.append(f"{event}: {path}")
            raise PermissionError(f"测试禁止访问真实 coding-proxy home: {event}")


sys.addaudithook(_audit)

_SENSITIVE_EXACT = frozenset(
    {"GITHUB_TOKEN", "CODING_PROXY_MASTER_KEY", "ANTIGRAVITY_GATEWAY_BASE_URL"}
)


def _is_sensitive_env(name: str) -> bool:
    return (
        name in _SENSITIVE_EXACT
        or name.endswith("_API_KEY")
        or name.startswith("GOOG_")
        or (name.startswith("NATIVE_") and name.endswith("_BASE_URL"))
    )


def pytest_runtest_setup(item: pytest.Item) -> None:
    # 先于该用例的所有 fixture（含 session 级）生效
    _guard_active[0] = item.get_closest_marker("e2e") is None


@pytest.fixture(autouse=True)
def _isolate_home(
    request: pytest.FixtureRequest,
    tmp_path_factory: pytest.TempPathFactory,
    monkeypatch: pytest.MonkeyPatch,
):
    if request.node.get_closest_marker("e2e") is not None:
        yield
        return
    root = tmp_path_factory.mktemp("isolated")
    home = root / "home"
    work = root / "cwd"
    home.mkdir()
    work.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("CODING_PROXY_HOME", str(home / ".coding-proxy"))
    monkeypatch.chdir(work)
    for name in list(os.environ):
        if _is_sensitive_env(name):
            monkeypatch.delenv(name, raising=False)
    before = len(_violations)
    yield
    leaked = _violations[before:]
    assert not leaked, f"测试触碰了真实 coding-proxy home: {leaked}"


_TEST_CLIENT_HOSTS = frozenset({"testserver", "test"})


@pytest.fixture(autouse=True)
def _allow_test_client_hosts(monkeypatch: pytest.MonkeyPatch):
    from coding.proxy.server import security

    monkeypatch.setattr(
        security, "LOOPBACK_HOSTS", security.LOOPBACK_HOSTS | _TEST_CLIENT_HOSTS
    )
    yield


@pytest.fixture
def strict_hosts(monkeypatch: pytest.MonkeyPatch, _allow_test_client_hosts):
    """还原产品默认的 loopback Host 集合（安全用例使用）."""
    from coding.proxy.server import security

    monkeypatch.setattr(
        security, "LOOPBACK_HOSTS", security.LOOPBACK_HOSTS - _TEST_CLIENT_HOSTS
    )
    yield
