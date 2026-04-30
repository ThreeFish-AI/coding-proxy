"""Session Policy 解析引擎 — 根据 session_key + client_category 解析适用的路由策略."""

from __future__ import annotations

import logging

from ..config.session_policy import SessionPolicy

logger = logging.getLogger(__name__)


class SessionPolicyResolver:
    """根据 session_key + client_category 解析适用的 SessionPolicy.

    设计要点：
    - 启动时构建索引，运行时 O(1) 查找
    - 精确匹配优先：session_key > client_category > 无策略
    - 无侵入性：不匹配时返回 None，路由行为与现有一致
    """

    def __init__(self, policies: list[SessionPolicy] | None = None) -> None:
        self._policies = policies or []
        self._key_index: dict[str, SessionPolicy] = {}
        self._category_index: dict[str, SessionPolicy] = {}
        self._build_index()

    def _build_index(self) -> None:
        """构建 session_key / client_category → SessionPolicy 的查找索引.

        按定义顺序遍历，首次出现的 key/category 获得最高优先级。
        """
        for policy in self._policies:
            for key in policy.match.session_keys:
                if key not in self._key_index:
                    self._key_index[key] = policy
            if (
                policy.match.client_category
                and policy.match.client_category not in self._category_index
            ):
                self._category_index[policy.match.client_category] = policy

        if self._key_index or self._category_index:
            logger.info(
                "SessionPolicyResolver initialized: %d key rules, %d category rules",
                len(self._key_index),
                len(self._category_index),
            )

    def resolve(
        self, session_key: str, client_category: str = "cc"
    ) -> SessionPolicy | None:
        """返回匹配的策略，优先精确 session_key 匹配，其次 category 匹配."""
        policy = self._key_index.get(session_key)
        if policy:
            return policy
        return self._category_index.get(client_category)
