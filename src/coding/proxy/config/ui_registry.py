"""Config 页字段元数据（单一事实源：label / widget / 适用 vendor / 中文说明）.

路径中 ``*`` 匹配任意 vendor 类型或列表元素。类型、默认值取自 Pydantic schema；
覆盖门测试保证每个非 legacy 叶子字段要么在 :data:`REGISTRY`，要么在 :data:`HIDDEN`。
"""

from __future__ import annotations

import types
from dataclasses import asdict, dataclass
from typing import Any, Union, get_args, get_origin

from pydantic import BaseModel

from .loader import _LEGACY_FLAT_KEYS
from .routing import VendorConfig
from .schema import ProxyConfig

WIDGETS = frozenset(
    {
        "text",
        "number",
        "bool",
        "enum",
        "secret",
        "list",
        "map",
        "optional_group",
        "list_section",
    }
)

_NATIVE_ANTHROPIC = ["zhipu", "minimax", "kimi", "doubao", "xiaomi", "alibaba"]
_VENDOR_TYPES = list(get_args(VendorConfig.model_fields["vendor"].annotation))


@dataclass(frozen=True)
class FieldSpec:
    path: str
    label: str
    widget: str
    description: str
    unit: str | None = None
    enum: list[str] | None = None
    min: float | None = None
    max: float | None = None
    vendors: list[str] | None = None
    group: str | None = None
    readonly: bool = False
    unset_hint: str | None = None


def _f(path: str, label: str, widget: str, description: str, **kw: Any) -> FieldSpec:
    return FieldSpec(path, label, widget, description, **kw)


def _vendor_qg(prefix: str, group: str, window_desc: str) -> list[FieldSpec]:
    return [
        _f(
            f"{prefix}.enabled",
            "Enabled",
            "bool",
            f"是否启用{window_desc}配额守卫",
            group=group,
        ),
        _f(
            f"{prefix}.token_budget",
            "Token Budget",
            "number",
            "滑动窗口内的 token 预算；0 表示仅按探测结果判定",
            unit="tokens",
            min=0,
            group=group,
        ),
        _f(
            f"{prefix}.window_hours",
            "Window",
            "number",
            "滑动窗口长度",
            unit="h",
            min=0,
            group=group,
        ),
        _f(
            f"{prefix}.threshold_percent",
            "Threshold %",
            "number",
            "用量达到预算该比例时暂停调度该 vendor",
            unit="%",
            min=0,
            max=100,
            group=group,
        ),
        _f(
            f"{prefix}.probe_interval_seconds",
            "Probe Interval",
            "number",
            "超限后探测恢复的间隔",
            unit="s",
            min=0,
            group=group,
        ),
    ]


REGISTRY: list[FieldSpec] = [
    # ── Server / Logging / Database / Auth（bootstrap，只读，经 CLI 修改） ──
    _f(
        "server.host",
        "Host",
        "text",
        "监听地址；修改需经 CLI 并重启",
        group="Server",
        readonly=True,
    ),
    _f(
        "server.port",
        "Port",
        "number",
        "监听端口；修改需经 CLI 并重启",
        min=1,
        max=65535,
        group="Server",
        readonly=True,
    ),
    _f(
        "server.allowed_hosts",
        "Allowed Hosts",
        "list",
        "Dashboard 与管理 API 额外允许的 Host（主机名或 IP，IPv6 可不加方括号；loopback 与本机直连 IP 恒允许）；修改需经 CLI 并重启",
        group="Server",
        readonly=True,
    ),
    _f(
        "logging.level",
        "Level",
        "enum",
        "控制台日志级别",
        enum=["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"],
        group="Logging",
        readonly=True,
    ),
    _f(
        "logging.file",
        "Log File",
        "text",
        "文件日志路径；未设置时为 .logs/coding-proxy.log，空串禁用",
        group="Logging",
        readonly=True,
    ),
    _f(
        "logging.max_bytes",
        "Max Bytes",
        "number",
        "单个日志文件上限，超过后轮转",
        unit="B",
        min=0,
        group="Logging",
        readonly=True,
    ),
    _f(
        "logging.backup_count",
        "Backups",
        "number",
        "保留的压缩备份文件数",
        min=0,
        group="Logging",
        readonly=True,
    ),
    _f(
        "database.path",
        "Usage DB",
        "text",
        "Token 用量数据库路径，默认 $CODING_PROXY_HOME/usage.db",
        group="Database",
        readonly=True,
    ),
    _f(
        "database.compat_state_path",
        "Compat DB",
        "text",
        "兼容层会话状态数据库路径，默认 $CODING_PROXY_HOME/compat.db",
        group="Database",
        readonly=True,
    ),
    _f(
        "database.compat_state_ttl_seconds",
        "Compat TTL",
        "number",
        "兼容层会话状态保留时长",
        unit="s",
        min=0,
        group="Database",
        readonly=True,
    ),
    _f(
        "auth.google_client_id",
        "Google Client",
        "text",
        "Google OAuth Client ID（Antigravity 登录用）",
        group="Auth",
        readonly=True,
    ),
    _f(
        "auth.google_client_secret",
        "Google Secret",
        "secret",
        "Google OAuth Client Secret，仅用于 CLI 登录流程（auth login、start 自动登录）；未设置时使用内置公开凭据",
        group="Auth",
        unset_hint="使用内置默认",
    ),
    _f(
        "auth.token_store_path",
        "Token Store",
        "text",
        "OAuth token 存储文件，默认 $CODING_PROXY_HOME/tokens.json",
        group="Auth",
        readonly=True,
    ),
    # ── Vendors ──
    _f(
        "vendors.*.enabled",
        "Enabled",
        "bool",
        "是否启用该 vendor；禁用后不参与路由",
        group="General",
    ),
    _f(
        "vendors.*.base_url",
        "Base URL",
        "text",
        "上游 API 地址；修改后已存密钥会被清空，需重新设置",
        group="General",
    ),
    _f(
        "vendors.*.timeout_ms",
        "Timeout",
        "number",
        "单次请求超时",
        unit="ms",
        min=0,
        group="General",
    ),
    _f(
        "vendors.*.github_token",
        "GitHub Token",
        "secret",
        "GitHub token；留空时使用 `coding-proxy auth login` 获取的 OAuth 凭证",
        vendors=["copilot"],
        group="Credentials",
        unset_hint="使用 OAuth 登录凭证",
    ),
    _f(
        "vendors.*.account_type",
        "Account Type",
        "enum",
        "Copilot 账户类型，决定默认 API 地址",
        enum=["individual", "business", "enterprise"],
        vendors=["copilot"],
        group="General",
    ),
    _f(
        "vendors.*.token_url",
        "Token URL",
        "text",
        "Copilot token 交换端点；修改后已存密钥会被清空",
        vendors=["copilot"],
        group="General",
    ),
    _f(
        "vendors.*.models_cache_ttl_seconds",
        "Models TTL",
        "number",
        "模型列表缓存时长",
        unit="s",
        min=0,
        vendors=["copilot"],
        group="General",
    ),
    _f(
        "vendors.*.client_id",
        "Client ID",
        "secret",
        "Antigravity 使用的 Google OAuth2 Client ID；留空且凭证来自 OAuth 登录时使用内置公开 Client ID",
        vendors=["antigravity"],
        group="Credentials",
        unset_hint="经 OAuth 登录时使用内置公开凭据",
    ),
    _f(
        "vendors.*.client_secret",
        "Client Secret",
        "secret",
        "Antigravity 使用的 Google OAuth2 Client Secret；留空且凭证来自 OAuth 登录时使用内置公开 Client Secret",
        vendors=["antigravity"],
        group="Credentials",
        unset_hint="经 OAuth 登录时使用内置公开凭据",
    ),
    _f(
        "vendors.*.refresh_token",
        "Refresh Token",
        "secret",
        "Google OAuth2 refresh token；留空时使用 OAuth 登录凭证",
        vendors=["antigravity"],
        group="Credentials",
        unset_hint="使用 OAuth 登录凭证",
    ),
    _f(
        "vendors.*.model_endpoint",
        "Endpoint",
        "text",
        "Antigravity 模型端点路径",
        vendors=["antigravity"],
        group="General",
    ),
    _f(
        "vendors.*.api_key",
        "API Key",
        "secret",
        "Anthropic 兼容端点的 API Key",
        vendors=list(_NATIVE_ANTHROPIC),
        group="Credentials",
        unset_hint="请求会因缺少 API Key 直接失败",
    ),
    _f(
        "vendors.*.circuit_breaker",
        "Breaker",
        "optional_group",
        "熔断器；关闭时不熔断，是否兜底由 tiers 中的位置决定",
        group="Circuit Breaker",
    ),
    _f(
        "vendors.*.circuit_breaker.failure_threshold",
        "Fail Threshold",
        "number",
        "连续失败多少次后熔断",
        min=1,
        group="Circuit Breaker",
    ),
    _f(
        "vendors.*.circuit_breaker.recovery_timeout_seconds",
        "Recovery",
        "number",
        "熔断后首次尝试恢复的等待时间",
        unit="s",
        min=0,
        group="Circuit Breaker",
    ),
    _f(
        "vendors.*.circuit_breaker.success_threshold",
        "Success Thr.",
        "number",
        "半开状态下连续成功多少次后恢复",
        min=1,
        group="Circuit Breaker",
    ),
    _f(
        "vendors.*.circuit_breaker.max_recovery_seconds",
        "Max Recovery",
        "number",
        "恢复等待时间指数退避的上限",
        unit="s",
        min=0,
        group="Circuit Breaker",
    ),
    *_vendor_qg("vendors.*.quota_guard", "Quota Guard", ""),
    *_vendor_qg("vendors.*.weekly_quota_guard", "Weekly Quota", "周"),
    _f(
        "vendors.*.concurrency",
        "Concurrency",
        "optional_group",
        "每模型并发限制；未设置时使用内置默认（每模型 3 个）",
        vendors=["zhipu"],
        group="Concurrency",
    ),
    _f(
        "vendors.*.concurrency.default",
        "Default Limit",
        "number",
        "未单独配置的模型的并发上限",
        min=1,
        max=20,
        vendors=["zhipu"],
        group="Concurrency",
    ),
    _f(
        "vendors.*.concurrency.models",
        "Per-Model",
        "map",
        "按映射后模型名单独设置并发上限",
        vendors=["zhipu"],
        group="Concurrency",
    ),
    # ── Tiers ──
    _f(
        "tiers",
        "Tier Order",
        "list",
        "降级链路优先级；未设置时按 vendors 列表顺序",
        enum=list(_VENDOR_TYPES),
    ),
    # ── Failover ──
    _f("failover.status_codes", "Status Codes", "list", "触发故障转移的 HTTP 状态码"),
    _f("failover.error_types", "Error Types", "list", "触发故障转移的上游错误类型"),
    _f(
        "failover.error_message_patterns",
        "Msg Patterns",
        "list",
        "错误消息包含任一关键字时触发故障转移",
    ),
    # ── Model Mapping ──
    _f(
        "model_mapping",
        "Mappings",
        "list_section",
        "模型名映射规则，按顺序匹配，首个命中生效",
    ),
    _f(
        "model_mapping.*.pattern",
        "Pattern",
        "text",
        "匹配的请求模型名（正则或精确匹配）",
    ),
    _f("model_mapping.*.target", "Target", "text", "映射后的上游模型名"),
    _f("model_mapping.*.is_regex", "Regex", "bool", "pattern 是否按正则匹配"),
    _f(
        "model_mapping.*.vendors",
        "Vendor Scope",
        "list",
        "仅对这些 vendor 生效；为空时对全部生效",
        enum=list(_VENDOR_TYPES),
    ),
    # ── Pricing ──
    _f(
        "pricing",
        "Pricing",
        "list_section",
        "模型单价（每百万 token），用于用量成本统计；修改会重算历史成本",
    ),
    _f("pricing.*.vendor", "Vendor", "text", "供应商名，对应用量表的供应商列"),
    _f("pricing.*.model", "Model", "text", "实际服务模型名"),
    _f(
        "pricing.*.input_cost_per_mtok",
        "Input",
        "number",
        "输入 token 单价（$ 或 ¥）",
        unit="/Mtok",
        min=0,
    ),
    _f(
        "pricing.*.output_cost_per_mtok",
        "Output",
        "number",
        "输出 token 单价（$ 或 ¥）",
        unit="/Mtok",
        min=0,
    ),
    _f(
        "pricing.*.cache_write_cost_per_mtok",
        "Cache Write",
        "number",
        "缓存写入 token 单价（$ 或 ¥）",
        unit="/Mtok",
        min=0,
    ),
    _f(
        "pricing.*.cache_read_cost_per_mtok",
        "Cache Read",
        "number",
        "缓存读取 token 单价（$ 或 ¥）",
        unit="/Mtok",
        min=0,
    ),
]

for _provider in ("openai", "gemini", "anthropic"):
    REGISTRY += [
        _f(
            f"native_api.{_provider}.enabled",
            "Enabled",
            "bool",
            "是否暴露该 provider 的原生透传端点（默认启用）",
            group=_provider,
        ),
        _f(
            f"native_api.{_provider}.base_url",
            "Base URL",
            "text",
            "上游域名前缀（不含 /v1）；可被 NATIVE_*_BASE_URL 环境变量覆盖",
            group=_provider,
        ),
        _f(
            f"native_api.{_provider}.timeout_ms",
            "Timeout",
            "number",
            "单次请求超时",
            unit="ms",
            min=1000,
            group=_provider,
        ),
        _f(
            f"native_api.{_provider}.connect_timeout_ms",
            "Conn. Timeout",
            "number",
            "连接建立超时",
            unit="ms",
            min=500,
            group=_provider,
        ),
    ]

REGISTRY += [
    # ── Session Policies ──
    _f(
        "session_policies.policies",
        "Policies",
        "list_section",
        "Session 路由策略，按顺序求值，首个命中生效",
    ),
    _f("session_policies.policies.*.name", "Name", "text", "策略名称，用于日志与排障"),
    _f(
        "session_policies.policies.*.match.session_keys",
        "Session Keys",
        "list",
        "精确匹配的 session key",
    ),
    _f(
        "session_policies.policies.*.tiers",
        "Tiers",
        "list",
        "命中后使用的 vendor 优先级",
        enum=list(_VENDOR_TYPES),
    ),
    _f(
        "session_policies.policies.*.model",
        "Model",
        "text",
        "会话级模型覆盖：非空时该 Session 的请求统一改写为此模型",
    ),
    _f(
        "session_policies.bindable_models",
        "Model List",
        "list_section",
        "可绑定模型清单，Model Bind 下拉选项的唯一来源",
    ),
    _f(
        "session_policies.bindable_models.*.label",
        "Label",
        "text",
        "UI 显示名，清单内唯一",
    ),
    _f(
        "session_policies.bindable_models.*.model",
        "Model",
        "text",
        "覆盖目标模型名，清单内唯一",
    ),
    _f(
        "session_policies.bindable_models.*.tiers",
        "Tiers",
        "list",
        "随绑定重排的 vendor 优先级，空则不改顺序",
        enum=list(_VENDOR_TYPES),
    ),
    _f(
        "session_policies.title_vendor_bindings",
        "Title Bindings",
        "list_section",
        "Session 标题前缀到 vendor 的自动绑定，首个命中生效",
    ),
    _f(
        "session_policies.title_vendor_bindings.*.prefix",
        "Prefix",
        "text",
        "标题前缀（大小写敏感，不能为空）",
    ),
    _f(
        "session_policies.title_vendor_bindings.*.vendor",
        "Vendor",
        "text",
        "命中后绑定的 vendor",
    ),
    _f(
        "session_policies.title_exempt_prefixes",
        "Title Exempt",
        "list",
        "以这些前缀开头的候选标题不写入 Session 标题",
    ),
]

# 不渲染的字段（前缀匹配）：legacy 顶层字段、传输层重试、预留字段
HIDDEN: frozenset[str] = frozenset(
    {
        *_LEGACY_FLAT_KEYS,
        "vendors.*.vendor",
        "vendors.*.retry",
        "auth.github_client_id",
        "session_policies.policies.*.quota",
        "session_policies.policies.*.match.client_category",
    }
)


# ── schema 遍历 ────────────────────────────────────────────────


def _unwrap_optional(annotation: Any) -> tuple[Any, bool]:
    origin = get_origin(annotation)
    if origin in (Union, types.UnionType):
        args = [a for a in get_args(annotation) if a is not type(None)]
        if len(args) == 1:
            return args[0], True
    return annotation, False


def _is_model(annotation: Any) -> bool:
    return isinstance(annotation, type) and issubclass(annotation, BaseModel)


def _list_item_model(annotation: Any) -> type[BaseModel] | None:
    if get_origin(annotation) is list:
        args = get_args(annotation)
        if args and _is_model(args[0]):
            return args[0]
    return None


def walk_schema(
    model: type[BaseModel] = ProxyConfig, prefix: str = ""
) -> tuple[set[str], set[str]]:
    """遍历 schema → ``(required, all_paths)``.

    ``required``：叶子字段、可选分组（``Model | None``）与列表 section；
    ``all_paths``：在此基础上再加普通嵌套分组，供 registry 路径存在性校验。
    """
    required: set[str] = set()
    all_paths: set[str] = set()
    for name, info in model.model_fields.items():
        if model is ProxyConfig and name in _LEGACY_FLAT_KEYS:
            continue
        path = f"{prefix}{name}"
        if model is ProxyConfig and name == "vendors":
            sub_req, sub_all = walk_schema(VendorConfig, "vendors.*.")
            required |= sub_req
            all_paths |= sub_all | {"vendors"}
            continue
        annotation, optional = _unwrap_optional(info.annotation)
        item_model = _list_item_model(annotation)
        if _is_model(annotation):
            sub_req, sub_all = walk_schema(annotation, f"{path}.")
            required |= sub_req
            all_paths |= sub_all | {path}
            if optional:
                required.add(path)
        elif item_model is not None:
            sub_req, sub_all = walk_schema(item_model, f"{path}.*.")
            required |= sub_req | {path}
            all_paths |= sub_all | {path}
        else:
            required.add(path)
            all_paths.add(path)
    return required, all_paths


def is_hidden(path: str) -> bool:
    return any(path == h or path.startswith(f"{h}.") for h in HIDDEN)


def match_path(pattern: str, path: str) -> bool:
    """``*`` 匹配单段的路径匹配."""
    p_parts, parts = pattern.split("."), path.split(".")
    return len(p_parts) == len(parts) and all(
        p == "*" or p == q for p, q in zip(p_parts, parts, strict=True)
    )


def find_spec(path: str) -> FieldSpec | None:
    for spec in REGISTRY:
        if match_path(spec.path, path):
            return spec
    return None


def registry_payload() -> dict:
    return {
        "fields": [asdict(spec) for spec in REGISTRY],
        "hidden": sorted(HIDDEN),
    }


__all__ = [
    "FieldSpec",
    "REGISTRY",
    "HIDDEN",
    "WIDGETS",
    "walk_schema",
    "is_hidden",
    "match_path",
    "find_spec",
    "registry_payload",
]
