"""模型名称映射器 — 按后端作用域解析模型名."""

from __future__ import annotations

import fnmatch
import logging
import re

from ..config.schema import ModelMappingRule

logger = logging.getLogger(__name__)

_DEFAULT_TARGET = "glm-5.1"
_BACKEND_ALIASES = {
    "zhipu": "fallback",
    "fallback": "fallback",
    "antigravity": "antigravity",
    "copilot": "copilot",
}


class ModelMapper:
    """将请求模型名映射到目标后端模型名."""

    def __init__(self, rules: list[ModelMappingRule]) -> None:
        self._rules = rules
        # 预编译正则表达式
        self._compiled: dict[str, re.Pattern] = {}
        for rule in rules:
            if rule.is_regex:
                self._compiled[rule.pattern] = re.compile(rule.pattern)

    @staticmethod
    def _normalize_backend(backend: str) -> str:
        normalized = backend.strip().lower()
        return _BACKEND_ALIASES.get(normalized, normalized)

    def _rule_applies_to_backend(self, rule: ModelMappingRule, backend: str) -> bool:
        if not rule.backends:
            # 向后兼容：历史规则默认只服务 fallback/zhipu
            return backend == "fallback"
        normalized = {self._normalize_backend(name) for name in rule.backends}
        return backend in normalized

    def map(self, model: str, backend: str = "fallback", default: str | None = None) -> str:
        """将源模型名映射为目标模型名.

        优先级：精确匹配 > 通配符/正则匹配 > default/_DEFAULT_TARGET。
        """
        backend_name = self._normalize_backend(backend)
        # 1. 精确匹配
        for rule in self._rules:
            if not self._rule_applies_to_backend(rule, backend_name):
                continue
            if not rule.is_regex and "*" not in rule.pattern:
                if rule.pattern == model:
                    logger.debug(
                        "Model mapped: %s -> %s (backend=%s exact)",
                        model, rule.target, backend_name,
                    )
                    return rule.target

        # 2. 通配符/正则匹配
        for rule in self._rules:
            if not self._rule_applies_to_backend(rule, backend_name):
                continue
            if rule.is_regex:
                compiled = self._compiled[rule.pattern]
                if compiled.fullmatch(model):
                    logger.debug(
                        "Model mapped: %s -> %s (backend=%s regex=%s)",
                        model, rule.target, backend_name, rule.pattern,
                    )
                    return rule.target
            elif "*" in rule.pattern:
                if fnmatch.fnmatch(model, rule.pattern):
                    logger.debug(
                        "Model mapped: %s -> %s (backend=%s glob=%s)",
                        model, rule.target, backend_name, rule.pattern,
                    )
                    return rule.target

        # 3. 默认值
        fallback_target = default or _DEFAULT_TARGET
        logger.debug(
            "Model unmapped: %s -> %s (backend=%s default)",
            model, fallback_target, backend_name,
        )
        return fallback_target
