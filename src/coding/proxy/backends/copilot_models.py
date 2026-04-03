"""Copilot 模型解析纯函数与诊断数据类."""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from typing import Any


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


# 向后兼容别名（旧名称带下划线前缀）
_copilot_model_family = copilot_model_family
_copilot_model_major = copilot_model_major
_copilot_model_version_rank = copilot_model_version_rank
_select_copilot_model = select_copilot_model
