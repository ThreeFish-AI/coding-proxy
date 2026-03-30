"""模型名称映射器 — claude-* → glm-*."""

from __future__ import annotations

import fnmatch
import logging
import re

from ..config.schema import ModelMappingRule

logger = logging.getLogger(__name__)

_DEFAULT_TARGET = "glm-5.1"


class ModelMapper:
    """将 Anthropic 模型名映射到智谱模型名."""

    def __init__(self, rules: list[ModelMappingRule]) -> None:
        self._rules = rules
        # 预编译正则表达式
        self._compiled: dict[str, re.Pattern] = {}
        for rule in rules:
            if rule.is_regex:
                self._compiled[rule.pattern] = re.compile(rule.pattern)

    def map(self, model: str) -> str:
        """将源模型名映射为目标模型名.

        优先级：精确匹配 > 通配符/正则匹配 > 默认值.
        """
        # 1. 精确匹配
        for rule in self._rules:
            if not rule.is_regex and "*" not in rule.pattern:
                if rule.pattern == model:
                    logger.debug("Model mapped: %s -> %s (exact)", model, rule.target)
                    return rule.target

        # 2. 通配符/正则匹配
        for rule in self._rules:
            if rule.is_regex:
                compiled = self._compiled[rule.pattern]
                if compiled.fullmatch(model):
                    logger.debug("Model mapped: %s -> %s (regex=%s)", model, rule.target, rule.pattern)
                    return rule.target
            elif "*" in rule.pattern:
                if fnmatch.fnmatch(model, rule.pattern):
                    logger.debug("Model mapped: %s -> %s (glob=%s)", model, rule.target, rule.pattern)
                    return rule.target

        # 3. 默认值
        logger.debug("Model unmapped: %s -> %s (default)", model, _DEFAULT_TARGET)
        return _DEFAULT_TARGET
