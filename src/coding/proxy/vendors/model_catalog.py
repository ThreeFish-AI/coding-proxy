"""内置 vendor 模型目录 — Model Bind 可选项的代码级事实源.

设计要点（SSOT）：
- 每类 vendor 一份内置目录常量（label/model/归属 tiers），随包升级自动
  触达所有用户，不受 config.db「list 整体替换」合并语义的存量冻结影响
- ``compose_bindable_catalog`` 在 app 构建时把「内置目录按激活 vendor 过滤」
  与 pricing 行（仅 native vendor：配价即入目录，``online: false`` 只标记
  下线、不可删除）及 ``session_policies.bindable_models`` 显式条目按 model
  去重合并：config 同名条目可覆盖 label/tiers（tiers 留空 = 继承内置值），
  ``exclude: true`` 可剔除内置同名条目
- 合成结果注入 ``RequestRouter._bindable_models``，/api/status 展示、
  PUT /api/session-vendor 白名单校验、generations 热切换迁移三个消费点
  共用 ``get_bindable_models()`` 单一出口，天然联动
- 绑定语义为 vendor.model 锁定：内置条目 tiers 指向原生归属 vendor，
  executor 侧严格链（见 executor._pinned_tier_names）保证绑定模型只发给
  原生支持它的 vendor，不做跨系列静默降级
- ``builtin_identity_rules`` 为 native 系 vendor 的目录内模型合成
  identity 映射规则（pattern=target，追加在用户规则之后），从构造上消灭
  ModelMapper default 回退对绑定模型的静默改写
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from ..config.routing import ModelMappingRule, ModelPricingEntry
from ..config.session_policy import BindableModel

logger = logging.getLogger(__name__)

# 走共享 ModelMapper 的 native 系 vendor（identity 规则只对这类 vendor 合成）
_NATIVE_MAPPER_VENDORS = frozenset(
    {"zhipu", "minimax", "kimi", "doubao", "xiaomi", "alibaba"}
)


@dataclass(frozen=True)
class CatalogEntry:
    """内置目录条目 — vendor.model 锁定语义的最小单元."""

    label: str
    model: str
    vendor: str
    tiers: tuple[str, ...] = field(default=())


def _entry(label: str, model: str, vendor: str) -> CatalogEntry:
    """构造 tiers 锁定原生归属 vendor 的目录条目."""
    return CatalogEntry(label=label, model=model, vendor=vendor, tiers=(vendor,))


# anthropic（Claude Plans / 原生透传）可绑定的 claude 家族
_ANTHROPIC = [
    ("Opus 4.8", "claude-opus-4-8"),
    ("Opus 4.7", "claude-opus-4-7"),
    ("Opus 4.6", "claude-opus-4-6"),
    ("Sonnet 4.6", "claude-sonnet-4-6"),
    ("Haiku 4.5", "claude-haiku-4-5"),
    ("Sonnet 5.5", "claude-sonnet-5-5"),
    ("Sonnet 5", "claude-sonnet-5"),
    ("Opus 5.5", "claude-opus-5-5"),
    ("Fable 5.1", "claude-fable-5-1"),
    ("Fable 5", "claude-fable-5"),
]

# zhipu（智谱）可绑定的 glm 家族
_ZHIPU = [
    ("GLM 5.4", "glm-5.4"),
    ("GLM 5.3", "glm-5.3"),
    ("GLM 5.3 Flash", "glm-5.3-flash"),
    ("GLM 5.2", "glm-5.2"),
    ("GLM 5.1", "glm-5.1"),
    ("GLM 5", "glm-5"),
    ("GLM 5 Turbo", "glm-5-turbo"),
    ("GLM 5V Turbo", "glm-5v-turbo"),
    ("GLM 4.7", "glm-4.7"),
    ("GLM 4.5 Air", "glm-4.5-air"),
]

# 其余 native 系 vendor 的内置目录（初版取自 pricing 表已知型号）
_MINIMAX = [
    ("MiniMax M2.7", "minimax-m2.7"),
    ("MiniMax M2.5", "minimax-m2.5"),
]
_KIMI = [("Kimi K2.5", "kimi-k2.5")]
_DOUBAO = [
    ("Doubao Seed 2.0 Code", "doubao-seed-2.0-code"),
    ("Doubao Seed 2.0 Pro", "doubao-seed-2.0-pro"),
    ("Doubao Seed 2.0 Lite", "doubao-seed-2.0-lite"),
]
_XIAOMI = [
    ("MiMo V2 Pro", "mimo-v2-pro"),
    ("MiMo V2 Omni", "mimo-v2-omni"),
    ("MiMo V2 Flash", "mimo-v2-flash"),
]
_ALIBABA = [("Qwen3.6 Plus", "qwen3.6-plus")]

BUILTIN_CATALOG: dict[str, list[CatalogEntry]] = {
    "anthropic": [_entry(lbl, mdl, "anthropic") for lbl, mdl in _ANTHROPIC],
    "zhipu": [_entry(lbl, mdl, "zhipu") for lbl, mdl in _ZHIPU],
    "minimax": [_entry(lbl, mdl, "minimax") for lbl, mdl in _MINIMAX],
    "kimi": [_entry(lbl, mdl, "kimi") for lbl, mdl in _KIMI],
    "doubao": [_entry(lbl, mdl, "doubao") for lbl, mdl in _DOUBAO],
    "xiaomi": [_entry(lbl, mdl, "xiaomi") for lbl, mdl in _XIAOMI],
    "alibaba": [_entry(lbl, mdl, "alibaba") for lbl, mdl in _ALIBABA],
    # copilot / antigravity 的可绑定面即 claude-* 家族（映射/家族匹配解析），
    # 与 anthropic 目录同 model 名，不单列目录避免下拉重复
}

# 各 vendor 原生模型名前缀族（跨系列支持性判定的静态依据）
NATIVE_MODEL_PREFIXES: dict[str, tuple[str, ...]] = {
    "anthropic": ("claude-",),
    "zhipu": ("glm-",),
    "minimax": ("minimax-",),
    "kimi": ("kimi-", "moonshot-"),
    "doubao": ("doubao-",),
    "xiaomi": ("mimo-",),
    "alibaba": ("qwen",),
}

# 全部已知原生前缀族（「他族原生名」判定 = 命中非本 vendor 的前缀族）
ALL_NATIVE_PREFIXES: tuple[str, ...] = tuple(
    sorted({p for ps in NATIVE_MODEL_PREFIXES.values() for p in ps})
)


def builtin_entries_for(vendor: str) -> list[CatalogEntry]:
    """返回指定 vendor 的内置目录条目（浅拷贝列表）."""
    return list(BUILTIN_CATALOG.get(vendor, ()))


def compose_bindable_catalog(
    active_vendor_names: list[str],
    config_entries: list[BindableModel],
    priced: Sequence[ModelPricingEntry] | None = None,
) -> list[dict[str, Any]]:
    """合成可绑定模型清单 = 内置目录（按激活 vendor 过滤）∪ pricing 行 ∪ config 显式条目.

    合并规则：
    - config 清单为空 = 整体关闭模型绑定（含内置目录与 pricing 派生条目，
      保留「空清单即禁用」的既有语义作为 off 开关）
    - pricing 行仅取 native vendor（``_NATIVE_MAPPER_VENDORS``）且在激活链：
      配价即入目录（派生条目 label = model 名，tiers 锁定该 vendor，自动获得
      identity 映射规则），填补内置目录滞后于上游新型号的缺口；按 (vendor,
      model) 精确去重，与内置/config 同名时让位
    - 下线以 model 为粒度：同名模型任一 pricing 行 ``online: false`` 即视为
      下线，条目带 ``offline: true`` 标记——不进 ``get_bindable_models()``
      下拉与 PUT 校验，executor 锁定链与热切换迁移仍可见（已绑会话保留）
    - 按 model 去重；config 同名条目覆盖 label，tiers 非空时覆盖 tiers，
      ``exclude=True`` 时剔除该内置/派生条目（纯自定义条目 exclude 无效，warning）
    - config 纯自定义条目（model 不在内置目录）原样保留：tiers 留空表示
      沿用全局链路顺序（executor 侧仍受支持性门控保护）
    - 返回条目含 vendor 字段（内置归属 / pricing 归属或 "custom"），供前端
      optgroup 分组
    """
    if not config_entries:
        return []
    active = [v for v in dict.fromkeys(active_vendor_names) if v]
    active_set = set(active)
    builtin_models = {e.model for v in active for e in builtin_entries_for(v)}
    # pricing 行登记：下线状态按 model 聚合（任一行下线即下线），派生条目按
    # (vendor, model) 精确匹配、保持配置顺序
    priced_online: dict[str, bool] = {}
    priced_rows: list[tuple[str, str]] = []
    seen_rows: set[tuple[str, str]] = set()
    for row in priced or ():
        vendor, model = str(row.vendor), str(row.model)
        online = bool(getattr(row, "online", True))
        priced_online[model] = priced_online.get(model, True) and online
        if (vendor, model) not in seen_rows:
            seen_rows.add((vendor, model))
            priced_rows.append((vendor, model))
    merged: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    # 用户 label 意图优先：内置/派生条目 label 撞上 config 中「指向不同 model」
    # 的 label 时降级（同 label 同 model 的覆盖合并不算冲突）；exclude 剔除的
    # 条目本身不进目录，其 label 不参与冲突判定
    config_label_models = {
        c.label: c.model
        for c in config_entries
        if not (c.exclude and c.model in builtin_models)
    }

    def _claim_label(label: str, model: str, owner: str) -> str:
        """label 撞 config 中指向不同 model 的 label 时降级为保证可区分的兜底名."""
        if config_label_models.get(label, model) == model:
            return label
        fallback = model
        if fallback in config_label_models:
            fallback = f"{model} ({owner})"
        logger.warning(
            "bindable label %r collides with config label, falling back to %r",
            label,
            fallback,
        )
        return fallback

    for vendor in active:
        for e in builtin_entries_for(vendor):
            if e.model in merged:
                continue
            merged[e.model] = {
                "label": _claim_label(e.label, e.model, e.vendor),
                "model": e.model,
                "tiers": list(e.tiers),
                "vendor": e.vendor,
            }
            order.append(e.model)

    for vendor, model in priced_rows:
        if vendor not in _NATIVE_MAPPER_VENDORS or vendor not in active_set:
            continue
        if model in merged:
            continue
        merged[model] = {
            "label": _claim_label(model, model, vendor),
            "model": model,
            "tiers": [vendor],
            "vendor": vendor,
        }
        order.append(model)

    for cfg in config_entries:
        existing = merged.get(cfg.model)
        if existing is not None and cfg.exclude:
            del merged[cfg.model]
            order.remove(cfg.model)
            continue
        if existing is not None:
            existing["label"] = cfg.label
            if cfg.tiers:
                existing["tiers"] = list(cfg.tiers)
        else:
            if cfg.exclude:
                logger.warning(
                    "bindable_models exclude=True on non-builtin model %s "
                    "has no effect",
                    cfg.model,
                )
            merged[cfg.model] = {
                "label": cfg.label,
                "model": cfg.model,
                "tiers": list(cfg.tiers),
                "vendor": "custom",
            }
            order.append(cfg.model)

    catalog = [merged[m] for m in order]
    for entry in catalog:
        if not priced_online.get(str(entry["model"]), True):
            entry["offline"] = True
    return catalog


def builtin_identity_rules(
    catalog: list[dict[str, Any]],
    user_rules: list[ModelMappingRule] | None = None,
) -> list[ModelMappingRule]:
    """为合成目录中 native 系 vendor 的模型合成 identity 映射规则.

    规则形如 ``pattern=target=model、vendors=[vendor]``、非 regex、追加在
    用户显式规则之后。归属 vendor 判定：entry 的锁定 tiers 优先；custom
    条目 tiers 为空时按原生模型名前缀族兜底（如 glm-9.9 → zhipu）。
    作用：目录内模型发往归属 vendor 时 exact 命中原名，杜绝 default 回退
    静默改写；漂移告警的 reason=exact 亦不误报。

    用户规则优先：某模型已被用户显式规则（exact / 通配 / 正则）覆盖到该
    vendor 作用域时跳过合成——ModelMapper 的 exact 通扫先于 pattern，合成
    exact 规则会遮蔽用户通配/正则规则（含未绑定请求），必须从源头避让。
    """
    from ..routing.model_mapper import ModelMapper

    user_mapper = ModelMapper(user_rules or [])
    rules: list[ModelMappingRule] = []
    seen: set[tuple[str, str]] = set()
    for entry in catalog:
        model = str(entry.get("model", ""))
        if not model:
            continue
        owners = [str(t) for t in entry.get("tiers", []) if t]
        if not owners:
            owners = [
                vendor
                for vendor, prefixes in NATIVE_MODEL_PREFIXES.items()
                if model.startswith(prefixes)
            ]
        for vendor in owners:
            if vendor not in _NATIVE_MAPPER_VENDORS:
                continue
            key = (vendor, model)
            if key in seen:
                continue
            _, reason = user_mapper.map_with_reason(model, vendor=vendor)
            if reason in ("exact", "pattern"):
                continue  # 用户显式规则已覆盖，不合成遮蔽性 exact 规则
            seen.add(key)
            rules.append(
                ModelMappingRule(
                    pattern=model, target=model, is_regex=False, vendors=[vendor]
                )
            )
    return rules
