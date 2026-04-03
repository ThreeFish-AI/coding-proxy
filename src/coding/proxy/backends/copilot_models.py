"""Copilot 模型解析纯函数、诊断数据类与模型目录管理策略."""

from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Protocol

import httpx

logger = logging.getLogger(__name__)


# ── 回调协议 ────────────────────────────────────────────


class _HttpRequestFn(Protocol):
    """HTTP 请求回调协议（由 CopilotBackend 注入）."""

    async def __call__(
        self,
        method: str,
        endpoint: str,
        *,
        headers: dict[str, str],
        json_body: dict[str, Any] | None = None,
    ) -> httpx.Response: ...


# ── 纯函数（模型解析） ───────────────────────────────────


def normalize_copilot_requested_model(model: str) -> str:
    """将 Anthropic 请求模型规范化为 Copilot 可协商的家族模型."""
    value = (model or "").strip()
    if not value:
        return value

    family_aliases = (
        ("claude-sonnet-", "claude-sonnet"),
        ("claude-opus-", "claude-opus"),
        ("claude-haiku-", "claude-haiku"),
    )
    for prefix, family in family_aliases:
        if value.startswith(prefix):
            remainder = value[len(prefix):]
            major = remainder.split("-", 1)[0].split(".", 1)[0]
            if major.isdigit():
                return f"{family}-{major}"
            return family
    return value


def copilot_model_family(model: str) -> str:
    normalized = normalize_copilot_requested_model(model)
    parts = normalized.split("-")
    if len(parts) >= 3 and parts[0] == "claude":
        return "-".join(parts[:2])
    return normalized


def copilot_model_major(model: str) -> int | None:
    normalized = normalize_copilot_requested_model(model)
    match = re.search(r"-(\d+)$", normalized)
    if not match:
        return None
    return int(match.group(1))


def copilot_model_version_rank(model: str) -> tuple[int, ...]:
    match = re.search(r"-(\d+(?:\.\d+)*)$", model)
    if not match:
        return ()
    return tuple(int(part) for part in match.group(1).split("."))


def select_copilot_model(
    requested_model: str,
    available_models: list[str],
) -> tuple[str | None, str]:
    """基于 Copilot 目录选择最终模型，同家族优先，不跨家族静默降级."""
    if not available_models:
        return None, "available_models_empty"

    unique_available = [model for model in dict.fromkeys(available_models) if model]
    if requested_model in unique_available:
        return requested_model, "exact_requested_model"

    normalized_model = normalize_copilot_requested_model(requested_model)
    if normalized_model in unique_available:
        return normalized_model, "normalized_requested_model"

    requested_family = copilot_model_family(requested_model)
    requested_major = copilot_model_major(requested_model)

    family_candidates = [
        model for model in unique_available
        if copilot_model_family(model) == requested_family
        and (requested_major is None or copilot_model_major(model) == requested_major)
    ]
    if not family_candidates:
        family_candidates = [
            model for model in unique_available
            if copilot_model_family(model) == requested_family
        ]
    if not family_candidates:
        return None, "no_same_family_model_available"

    ranked = sorted(
        family_candidates,
        key=lambda item: (
            len(copilot_model_version_rank(item)) == 0,
            copilot_model_version_rank(item),
            item,
        ),
        reverse=True,
    )
    return ranked[0], "same_family_highest_version"


# ── 诊断数据类 ────────────────────────────────────────────


@dataclass
class CopilotMisdirectedRequest:
    base_url: str
    status_code: int
    request: Any  # httpx.Request (avoid circular import at module level)
    headers: Any  # httpx.Headers
    body: bytes


@dataclass
class CopilotExchangeDiagnostics:
    """最近一次 Copilot token 交换的运行时诊断."""

    raw_shape: str = ""
    token_field: str = ""
    expires_in_seconds: int = 0
    expires_at_unix: int = 0
    capabilities: dict[str, Any] = field(default_factory=dict)
    updated_at_unix: int = 0

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {}
        if self.raw_shape:
            data["raw_shape"] = self.raw_shape
        if self.token_field:
            data["token_field"] = self.token_field
        if self.expires_in_seconds:
            data["expires_in_seconds"] = self.expires_in_seconds
        if self.expires_at_unix:
            data["expires_at_unix"] = self.expires_at_unix
            data["ttl_seconds"] = max(self.expires_at_unix - int(time.time()), 0)
        if self.capabilities:
            data["capabilities"] = self.capabilities
        if self.updated_at_unix:
            data["updated_at_unix"] = self.updated_at_unix
        return data


@dataclass
class CopilotModelCatalog:
    available_models: list[str] = field(default_factory=list)
    fetched_at_unix: int = 0

    def age_seconds(self) -> int | None:
        if not self.fetched_at_unix:
            return None
        return max(int(time.time()) - self.fetched_at_unix, 0)


# ── CopilotModelResolver 策略类 ───────────────────────────


class CopilotModelResolver:
    """Copilot 模型目录管理与解析策略.

    职责:
    - 维护模型目录缓存（CopilotModelCatalog）及 TTL
    - 通过注入的 HTTP 回调获取可用模型列表
    - 基于配置规则或家族匹配策略解析最终模型名

    设计: 不直接持有 HTTP client 或 Backend 引用，通过 ``request_fn`` 回调
    注入请求能力，实现 Dependency Inversion.
    """

    def __init__(
        self,
        models_cache_ttl_seconds: int,
        model_mapper: Any = None,
    ) -> None:
        self._catalog = CopilotModelCatalog()
        self._ttl = max(models_cache_ttl_seconds, 0)
        self._model_mapper = model_mapper
        # 诊断字段
        self.last_normalized_model = ""
        self.last_model_refresh_reason = ""

    @property
    def catalog(self) -> CopilotModelCatalog:
        return self._catalog

    # ── 目录新鲜度 ─────────────────────────────────────

    def is_fresh(self) -> bool:
        if not self._catalog.available_models:
            return False
        if self._ttl == 0:
            return False
        age = self._catalog.age_seconds()
        return age is not None and age < self._ttl

    # ── 模型列表获取 ───────────────────────────────────

    async def fetch_available(
        self,
        *,
        request_fn: _HttpRequestFn,
        headers_fn: Callable[[], dict[str, str]],
        refresh_reason: str,
    ) -> list[str]:
        """从 Copilot API 获取可用模型列表并更新目录."""
        response = await request_fn(
            "GET",
            "/models",
            headers=headers_fn(),
        )
        from .base import _decode_json_body  # 延迟导入避免循环依赖

        payload = _decode_json_body(response)
        if response.status_code >= 400:
            self.last_model_refresh_reason = f"{refresh_reason}:probe_error"
            return []

        available_models = extract_available_models(payload)
        self._catalog = CopilotModelCatalog(
            available_models=available_models,
            fetched_at_unix=int(time.time()),
        )
        self.last_model_refresh_reason = refresh_reason
        return available_models

    async def get_available(
        self,
        *,
        force_refresh: bool,
        request_fn: _HttpRequestFn,
        headers_fn: Callable[[], dict[str, str]],
        refresh_reason: str,
    ) -> list[str]:
        """获取可用模型列表（带 TTL 缓存）."""
        if force_refresh or not self.is_fresh():
            self.last_model_refresh_reason = refresh_reason
            available_models = await self.fetch_available(
                request_fn=request_fn,
                headers_fn=headers_fn,
                refresh_reason=refresh_reason,
            )
            if available_models:
                self._catalog = CopilotModelCatalog(
                    available_models=list(available_models),
                    fetched_at_unix=int(time.time()),
                )
            return available_models
        return list(self._catalog.available_models)

    # ── 模型解析 ───────────────────────────────────────

    async def resolve(
        self,
        requested_model: str,
        *,
        force_refresh: bool,
        request_fn: _HttpRequestFn,
        headers_fn: Callable[[], dict[str, str]],
        refresh_reason: str,
        # 以下为诊断回写目标（由调用方传入的可变对象）
        diagnostics: dict[str, str],
    ) -> str:
        """解析请求模型名为最终模型名.

        Returns:
            解析后的模型名字符串. 同时将中间结果写入 *diagnostics* 字典.
        """
        # 优先：配置规则显式映射
        if self._model_mapper is not None:
            mapped = self._model_mapper.map(
                requested_model, backend="copilot", default=requested_model,
            )
            if mapped != requested_model:
                diagnostics["requested_model"] = requested_model
                diagnostics["normalized_model"] = requested_model
                diagnostics["resolved_model"] = mapped
                diagnostics["resolution_reason"] = "config_model_mapping"
                self.last_normalized_model = requested_model
                return mapped

        # 次级：内部家族匹配策略
        normalized_model = normalize_copilot_requested_model(requested_model)
        available_models = await self.get_available(
            force_refresh=force_refresh,
            request_fn=request_fn,
            headers_fn=headers_fn,
            refresh_reason=refresh_reason,
        )
        resolved_model, resolution_reason = select_copilot_model(
            requested_model, available_models,
        )
        if not resolved_model:
            resolved_model = normalized_model or requested_model
            resolution_reason = (
                "catalog_unavailable_fallback_to_normalized"
                if not available_models else
                "no_same_family_model_fallback_to_normalized"
            )

        diagnostics["requested_model"] = requested_model
        diagnostics["normalized_model"] = normalized_model
        diagnostics["resolved_model"] = resolved_model
        diagnostics["resolution_reason"] = resolution_reason
        self.last_normalized_model = normalized_model
        return resolved_model

    # ── 错误响应构建 ───────────────────────────────────

    @staticmethod
    def build_model_not_supported_response(
        response: httpx.Response,
        *,
        requested_model: str,
        normalized_model: str,
        resolved_model: str,
        available_models: list[str],
    ) -> httpx.Response:
        """构建 model_not_supported 错误响应."""
        payload = {
            "error": {
                "type": "invalid_request_error",
                "message": "Copilot 当前账号未开放与请求同家族匹配的模型",
                "code": "model_not_supported",
                "param": "model",
                "details": {
                    "requested_model": requested_model,
                    "normalized_model": normalized_model,
                    "resolved_model": resolved_model,
                    "available_models": available_models,
                },
            }
        }
        return httpx.Response(
            400,
            content=json.dumps(payload, ensure_ascii=False).encode(),
            headers={"content-type": "application/json"},
            request=response.request,
        )

    @staticmethod
    def is_model_not_supported_response(response: httpx.Response | None) -> bool:
        """检测响应是否为 model_not_supported 错误."""
        if response is None or response.status_code != 400:
            return False
        from .base import _decode_json_body  # 延迟导入避免循环依赖

        payload = _decode_json_body(response)
        if not isinstance(payload, dict):
            return False
        error = payload.get("error")
        if not isinstance(error, dict):
            return False
        return error.get("code") == "model_not_supported"


def extract_available_models(payload: dict[str, Any] | list[Any] | None) -> list[str]:
    """从 Copilot /models 响应中提取模型 ID 列表."""
    if not isinstance(payload, dict):
        return []
    models = payload.get("data", [])
    if not isinstance(models, list):
        return []
    return [
        item.get("id")
        for item in models
        if isinstance(item, dict) and isinstance(item.get("id"), str) and item.get("id")
    ]


# 向后兼容别名（旧名称带下划线前缀）
_copilot_model_family = copilot_model_family
_copilot_model_major = copilot_model_major
_copilot_model_version_rank = copilot_model_version_rank
_select_copilot_model = select_copilot_model
