"""模型名称映射器 — 按供应商作用域解析模型名."""

from __future__ import annotations

import fnmatch
import logging
import re

from ..config.schema import ModelMappingRule

logger = logging.getLogger(__name__)

_DEFAULT_TARGET = "glm-5.1"
_VENDOR_ALIASES = {
    "zhipu": "fallback",
    "fallback": "fallback",
    "antigravity": "antigravity",
    "copilot": "copilot",
    "minimax": "minimax",
    "kimi": "kimi",
    "doubao": "doubao",
    "xiaomi": "xiaomi",
    "alibaba": "alibaba",
}


class ModelMapper:
    """将请求模型名映射到目标供应商模型名."""

    def __init__(self, rules: list[ModelMappingRule]) -> None:
        self._rules = rules
        # 预编译正则表达式
        self._compiled: dict[str, re.Pattern] = {}
        for rule in rules:
            if rule.is_regex:
                self._compiled[rule.pattern] = re.compile(rule.pattern)

    @staticmethod
    def _normalize_vendor(vendor: str) -> str:
        normalized = vendor.strip().lower()
        return _VENDOR_ALIASES.get(normalized, normalized)

    def _rule_applies_to_vendor(self, rule: ModelMappingRule, vendor: str) -> bool:
        if not rule.vendors:
            # 向后兼容：历史规则默认只服务 fallback/zhipu
            return vendor == "fallback"
        normalized = {self._normalize_vendor(name) for name in rule.vendors}
        return vendor in normalized

    def map(
        self, model: str, vendor: str = "fallback", default: str | None = None
    ) -> str:
        """将源模型名映射为目标模型名.

        优先级：精确匹配 > 通配符/正则匹配 > default/_DEFAULT_TARGET。
        """
        display_name = vendor.strip().lower()
        match_key = self._normalize_vendor(vendor)
        # 1. 精确匹配
        for rule in self._rules:
            if not self._rule_applies_to_vendor(rule, match_key):
                continue
            if not rule.is_regex and "*" not in rule.pattern:
                if rule.pattern == model:
                    return rule.target

        # 2. 通配符/正则匹配
        for rule in self._rules:
            if not self._rule_applies_to_vendor(rule, match_key):
                continue
            if rule.is_regex:
                compiled = self._compiled[rule.pattern]
                if compiled.fullmatch(model):
                    return rule.target
            elif "*" in rule.pattern:
                if fnmatch.fnmatch(model, rule.pattern):
                    return rule.target

        # 3. 默认值
        fallback_target = default or _DEFAULT_TARGET
        logger.debug(
            "Model unmapped: %s -> %s (vendor=%s default)",
            model,
            fallback_target,
            display_name,
        )
        return fallback_target
