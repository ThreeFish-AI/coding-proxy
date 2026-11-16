"""订阅状态聚合服务 — Overview「订阅状态」卡片的数据源.

三家订阅的档位与用量获取途径（均为上游未文档化接口，解析一律宽容式，
单家失败不影响其他家）：

- **GLM（智谱 Coding Plan）**：用已配置的 api_key 主动调官方监控端点
  ``GET {host}/api/monitor/usage/quota/limit``（智谱官方插件
  zai-coding-plugins 生产同款；``Authorization`` 为裸 key、无 Bearer 前缀，
  字段存在两种历史形态需防御式解析）。host 由 zhipu ``base_url`` 派生。
- **Antigravity（Google）**：用 vendor 已持有的 OAuth token 主动调
  ``v1internal:loadCodeAssist``（取订阅档位与 cloudaicompanionProject）
  + ``v1internal:retrieveUserQuotaSummary``（分组配额桶剩余比例）。
  UA 门控：请求必须带 Antigravity 客户端指纹（同
  :mod:`..vendors.antigravity`），否则 403 或缺 Gemini 组。
- **Claude Code（Anthropic 订阅）**：纯透传不持有凭证，无主动通道；
  消费 :mod:`..routing.subscription_signals` 的被动快照（unified
  ratelimit 响应头旁路采集）。

上游错误一律收敛为短消息（不含凭证/URL 细节，遵循 I2 泄露纪律）。
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any

import httpx

from ..config.schema import AntigravityConfig
from ..routing.subscription_signals import get_anthropic_snapshot
from ..vendors.antigravity import AntigravityVendor, GoogleOAuthTokenManager
from ..vendors.token_manager import TokenAcquireError, TokenErrorKind
from .factory import _resolve_antigravity_credentials

logger = logging.getLogger(__name__)

# ── 上游契约常量 ──────────────────────────────────────────────

# 智谱监控端点路径（挂在 base_url 的 host 根下，非 /api/anthropic 路径）
_ZHIPU_QUOTA_PATH = "/api/monitor/usage/quota/limit"

# Antigravity v1internal：loadCodeAssist 在 cloudcode-pa，配额汇总在
# daily-cloudcode-pa（Gemini 组口径以此为准，域名漂移史见 antigravity_gateway/config.py）
_AG_LOAD_CODE_ASSIST_URL = (
    "https://cloudcode-pa.googleapis.com/v1internal:loadCodeAssist"
)
_AG_QUOTA_SUMMARY_URL = (
    "https://daily-cloudcode-pa.googleapis.com/v1internal:retrieveUserQuotaSummary"
)

# Antigravity 客户端指纹（与 vendors/antigravity.py:36-40 对齐；UA 门控必需）
_AG_USER_AGENT = (
    "Antigravity/4.1.31 (Macintosh; Intel Mac OS X 10_15_7) "
    "Chrome/132.0.6834.160 Electron/39.2.3"
)
_AG_CLIENT_NAME = "antigravity"
_AG_CLIENT_VERSION = "4.1.31"

# loadCodeAssist 结果（档位 + project）缓存时长：账号级事实，低频变化
_AG_PROJECT_CACHE_TTL = 3600.0

_ZHIPU_DEFAULT_BASE_URL = "https://open.bigmodel.cn/api/anthropic"

_WINDOW_LABELS = {"5h": "5 小时", "weekly": "每周"}


class SubscriptionError(Exception):
    """订阅查询失败（消息面向 Dashboard 展示，短且无敏感细节）."""


# ── 数值解析工具 ──────────────────────────────────────────────


def _clamp_percent(value: float) -> float:
    return max(0.0, min(100.0, float(value)))


def _epoch_seconds(value: Any) -> int | None:
    """epoch 自适应：毫秒（>1e11）转秒，其余按秒."""
    if value is None:
        return None
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    if v <= 0:
        return None
    return int(v / 1000) if v > 1e11 else int(v)


def _iso_to_epoch(value: Any) -> int | None:
    """ISO 8601（含 Z 后缀）→ epoch 秒."""
    if not isinstance(value, str) or not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return int(dt.timestamp())


def _pick(item: dict[str, Any], *keys: str) -> Any:
    """多形态字段防御式取值：返回第一个存在且非 None 的键值."""
    for key in keys:
        if key in item and item[key] is not None:
            return item[key]
    return None


def _zhipu_window_kind(item: dict[str, Any]) -> str | None:
    """按 unit/number 编码判定窗口类型（官方插件契约）.

    unit=3 & number=5 → 5 小时窗口；unit=6 & number=1 → 每周窗口。
    老套餐可能只有一条 TOKENS_LIMIT；未知组合返回 None（跳过）。
    """
    unit, number = item.get("unit"), item.get("number")
    if unit == 3 and number == 5:
        return "5h"
    if unit == 6 and number == 1:
        return "weekly"
    return None


# ── GLM fetcher ──────────────────────────────────────────────


class ZhipuUsageFetcher:
    """智谱 Coding Plan 套餐额度查询（官方监控端点，裸 key 鉴权）."""

    def __init__(
        self,
        api_key: str,
        base_url: str | None = None,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        timeout: float = 10.0,
    ) -> None:
        self._api_key = api_key
        parsed = httpx.URL(base_url or _ZHIPU_DEFAULT_BASE_URL)
        # httpx.URL.netloc 返回 bytes（f-string 会拼出 b'host' 非法值），用 host/port
        root = f"{parsed.scheme}://{parsed.host}"
        if parsed.port is not None:
            root = f"{root}:{parsed.port}"
        self._monitor_root = root
        self._transport = transport
        self._timeout = timeout

    def _get_client(self) -> httpx.AsyncClient:
        # 短生命周期 client（查询为低频操作，不值得常驻连接池）
        return httpx.AsyncClient(timeout=self._timeout, transport=self._transport)

    async def fetch(self) -> dict[str, Any]:
        """返回 ``{"plan": str|None, "windows": [...]}``，失败抛 SubscriptionError."""
        url = f"{self._monitor_root}{_ZHIPU_QUOTA_PATH}"
        headers = {
            # 官方插件契约：裸 key，无 Bearer 前缀（加 Bearer 会 401）
            "Authorization": self._api_key,
            "Accept": "application/json",
            "Content-Type": "application/json",
        }
        try:
            async with self._get_client() as client:
                resp = await client.get(url, headers=headers)
        except httpx.HTTPError as exc:
            logger.warning("zhipu quota query network error: %s", type(exc).__name__)
            raise SubscriptionError("网络异常") from None
        if resp.status_code != 200:
            raise SubscriptionError(f"HTTP {resp.status_code}")
        try:
            payload = resp.json()
        except ValueError:
            raise SubscriptionError("响应非 JSON") from None

        data = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(data, dict):
            raise SubscriptionError("响应缺少 data")

        plan = data.get("level")
        windows: list[dict[str, Any]] = []
        for item in data.get("limits") or []:
            if not isinstance(item, dict) or item.get("type") != "TOKENS_LIMIT":
                continue
            kind = _zhipu_window_kind(item)
            percent = _pick(item, "percentage", "used_percent")
            if kind is None or not isinstance(percent, (int, float)):
                continue
            windows.append(
                {
                    "id": kind,
                    "label": _WINDOW_LABELS[kind],
                    "used_percent": _clamp_percent(percent),
                    "reset_epoch": _epoch_seconds(
                        _pick(item, "nextResetTime", "reset_time")
                    ),
                }
            )
        return {"plan": str(plan).capitalize() if plan else None, "windows": windows}


# ── Antigravity fetcher ──────────────────────────────────────


class AntigravityUsageFetcher:
    """Google Antigravity 订阅档位 + 分组配额查询（v1internal，OAuth token）."""

    def __init__(
        self,
        token_getter: Callable[[], Awaitable[str]],
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        timeout: float = 15.0,
    ) -> None:
        self._token_getter = token_getter
        self._transport = transport
        self._timeout = timeout
        # loadCodeAssist 缓存：(tier_name, project, expires_at_monotonic)
        self._tier_cache: tuple[str, str, float] | None = None

    def _get_client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(timeout=self._timeout, transport=self._transport)

    def _headers(self, token: str) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "User-Agent": _AG_USER_AGENT,
            "x-client-name": _AG_CLIENT_NAME,
            "x-client-version": _AG_CLIENT_VERSION,
        }

    async def _post_json(
        self, client: httpx.AsyncClient, url: str, token: str, body: dict[str, Any]
    ) -> dict[str, Any]:
        try:
            resp = await client.post(url, json=body, headers=self._headers(token))
        except httpx.HTTPError as exc:
            logger.warning("antigravity query network error: %s", type(exc).__name__)
            raise SubscriptionError("网络异常") from None
        if resp.status_code == 403:
            # UA 门控 / 订阅不符 / project 缺失都会落 403，统一短消息
            raise SubscriptionError("无有效订阅许可 (403)")
        if resp.status_code != 200:
            raise SubscriptionError(f"HTTP {resp.status_code}")
        try:
            data = resp.json()
        except ValueError:
            raise SubscriptionError("响应非 JSON") from None
        if not isinstance(data, dict):
            raise SubscriptionError("响应结构异常")
        return data

    async def _ensure_tier(
        self, client: httpx.AsyncClient, token: str
    ) -> tuple[str, str]:
        """loadCodeAssist 取订阅档位 + cloudaicompanionProject（带缓存）."""
        if self._tier_cache and time.monotonic() < self._tier_cache[2]:
            return self._tier_cache[0], self._tier_cache[1]
        data = await self._post_json(
            client,
            _AG_LOAD_CODE_ASSIST_URL,
            token,
            {
                "metadata": {
                    "ideType": "ANTIGRAVITY",
                    "platform": "PLATFORM_UNSPECIFIED",
                    "pluginType": "GEMINI",
                }
            },
        )
        tier = data.get("currentTier") or data.get("paidTier") or {}
        tier_name = tier.get("name") if isinstance(tier, dict) else None
        raw_project = data.get("cloudaicompanionProject")
        if isinstance(raw_project, dict):
            project = raw_project.get("id") or raw_project.get("projectId") or ""
        else:
            project = raw_project if isinstance(raw_project, str) else ""
        if not project:
            raise SubscriptionError("loadCodeAssist 未返回 project")
        self._tier_cache = (
            str(tier_name or ""),
            project,
            time.monotonic() + _AG_PROJECT_CACHE_TTL,
        )
        return str(tier_name or ""), project

    async def fetch(self) -> dict[str, Any]:
        """返回 ``{"plan": str|None, "windows": [...]}``，失败抛 SubscriptionError."""
        try:
            token = await self._token_getter()
        except TokenAcquireError as exc:
            if exc.kind == TokenErrorKind.INVALID_CREDENTIALS:
                raise SubscriptionError("凭证失效，需重新登录") from None
            raise SubscriptionError("token 刷新失败") from None
        async with self._get_client() as client:
            plan, project = await self._ensure_tier(client, token)
            data = await self._post_json(
                client, _AG_QUOTA_SUMMARY_URL, token, {"project": project}
            )

        windows: list[dict[str, Any]] = []
        for group in data.get("groups") or []:
            if not isinstance(group, dict):
                continue
            group_name = str(group.get("displayName") or "").strip()
            for bucket in group.get("buckets") or []:
                if not isinstance(bucket, dict):
                    continue
                remaining = bucket.get("remainingFraction")
                if not isinstance(remaining, (int, float)):
                    continue
                window = str(bucket.get("window") or "").strip()
                label = " ".join(x for x in (group_name, window) if x) or str(
                    bucket.get("bucketId") or "配额"
                )
                windows.append(
                    {
                        "id": str(bucket.get("bucketId") or label),
                        "label": label,
                        "used_percent": _clamp_percent((1.0 - remaining) * 100.0),
                        "reset_epoch": _iso_to_epoch(bucket.get("resetTime")),
                        "exhausted": bool(bucket.get("disabled")) or remaining <= 0,
                    }
                )
        return {"plan": plan or None, "windows": windows}


# ── 聚合服务 ──────────────────────────────────────────────────


def _build_zhipu_fetcher(config: Any) -> ZhipuUsageFetcher | None:
    vendors = getattr(config, "vendors", None) or []
    cfg = next(
        (v for v in vendors if getattr(v, "vendor", "") == "zhipu" and v.enabled),
        None,
    )
    if cfg is None or not getattr(cfg, "api_key", ""):
        return None
    return ZhipuUsageFetcher(cfg.api_key, cfg.base_url)


def _build_antigravity_fetcher(
    router: Any, config: Any = None, token_store: Any = None
) -> AntigravityUsageFetcher | None:
    """优先复用 vendor 通道的 token manager（reauth 热更新天然跟随）.

    回退路径：Antigravity vendor 未进路由链路（如仅用 IDE gateway 透传）时，
    用 config + token store 合成独立 manager；此时 CLI 重新登录后需重启才会
    对齐新凭证（Dashboard reauth 仅热更新 vendor 通道）。注意 gateway 通道
    （IDE 自带凭证）与凭证通道账号可能不同，本面板展示凭证通道口径。
    """
    for tier in getattr(router, "tiers", None) or []:
        vendor = getattr(tier, "vendor", None)
        if isinstance(vendor, AntigravityVendor):
            token_manager = vendor._token_manager  # noqa: SLF001 — 与 app.py reauth 装配同款访问
            return AntigravityUsageFetcher(token_manager.get_token)
    if config is None or token_store is None:
        return None
    entry = next(
        (
            v
            for v in getattr(config, "vendors", None) or []
            if getattr(v, "vendor", "") == "antigravity"
        ),
        None,
    )
    cfg = AntigravityConfig(
        client_id=getattr(entry, "client_id", "") or "",
        client_secret=getattr(entry, "client_secret", "") or "",
        refresh_token=getattr(entry, "refresh_token", "") or "",
    )
    cfg = _resolve_antigravity_credentials(cfg, token_store)
    if not cfg.refresh_token:
        return None
    manager = GoogleOAuthTokenManager(
        cfg.client_id, cfg.client_secret, cfg.refresh_token
    )
    return AntigravityUsageFetcher(manager.get_token)


class SubscriptionStatusService:
    """三家订阅状态聚合（TTL 缓存 + per-vendor 失败隔离）.

    主动查询低频执行（Dashboard 刷新驱动，TTL 内多请求只打一次上游），
    错误同样入缓存以防刷新风暴打爆未文档化端点。
    """

    def __init__(
        self,
        config: Any,
        router: Any,
        *,
        token_store: Any = None,
        ttl_seconds: float = 120.0,
        zhipu_fetcher: ZhipuUsageFetcher | None = None,
        antigravity_fetcher: AntigravityUsageFetcher | None = None,
    ) -> None:
        self._ttl = ttl_seconds
        self._zhipu = (
            zhipu_fetcher if zhipu_fetcher is not None else _build_zhipu_fetcher(config)
        )
        self._antigravity = (
            antigravity_fetcher
            if antigravity_fetcher is not None
            else _build_antigravity_fetcher(router, config, token_store)
        )
        self._cache: dict[str, tuple[float, dict[str, Any]]] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    def _lock(self, key: str) -> asyncio.Lock:
        return self._locks.setdefault(key, asyncio.Lock())

    @staticmethod
    def _claude_entry() -> dict[str, Any]:
        snapshot = get_anthropic_snapshot()
        if snapshot is None:
            return {
                "key": "claude",
                "plan": None,
                "status": "waiting",
                "windows": [],
            }
        windows = []
        if snapshot.five_hour_percent is not None:
            windows.append(
                {
                    "id": "5h",
                    "label": "5 小时",
                    "used_percent": snapshot.five_hour_percent,
                    "reset_epoch": snapshot.five_hour_reset_epoch,
                }
            )
        if snapshot.seven_day_percent is not None:
            windows.append(
                {
                    "id": "weekly",
                    "label": "7 天",
                    "used_percent": snapshot.seven_day_percent,
                    "reset_epoch": snapshot.seven_day_reset_epoch,
                }
            )
        return {
            "key": "claude",
            "plan": None,
            "status": snapshot.status or "ok",
            "windows": windows,
            "updated_at": snapshot.captured_at_epoch,
        }

    async def _cached_entry(
        self,
        key: str,
        fetcher: Any,
        fetcher_label: str,
    ) -> dict[str, Any]:
        async with self._lock(key):
            hit = self._cache.get(key)
            now = time.monotonic()
            if hit and now - hit[0] < self._ttl:
                return hit[1]
            entry: dict[str, Any]
            if fetcher is None:
                entry = {"key": key, "status": "not_configured", "windows": []}
            else:
                try:
                    result = await fetcher.fetch()
                    entry = {"key": key, "status": "ok", **result}
                except SubscriptionError as exc:
                    entry = {
                        "key": key,
                        "status": "error",
                        "error": str(exc) or fetcher_label + " 查询失败",
                        "windows": [],
                    }
                except Exception as exc:
                    # I2 泄露纪律：未预期异常只暴露类型名
                    logger.warning(
                        "subscription fetch %s failed: %s",
                        key,
                        type(exc).__name__,
                    )
                    entry = {
                        "key": key,
                        "status": "error",
                        "error": "查询异常",
                        "windows": [],
                    }
            self._cache[key] = (now, entry)
            return entry

    async def get_status(self) -> dict[str, Any]:
        """聚合三家快照（并发主动查询，单家失败隔离）."""
        glm_task = self._cached_entry("glm", self._zhipu, "GLM")
        ag_task = self._cached_entry("antigravity", self._antigravity, "Antigravity")
        glm_entry, ag_entry = await asyncio.gather(glm_task, ag_task)
        return {"vendors": [self._claude_entry(), glm_entry, ag_entry]}
