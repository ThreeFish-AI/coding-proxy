"""Session Policy 解析引擎 — 根据 session_key + client_category 解析适用的路由策略."""

from __future__ import annotations

import logging
import threading

from ..config.session_policy import SessionPolicy, SessionPolicyMatch

logger = logging.getLogger(__name__)


class SessionPolicyResolver:
    """根据 session_key + client_category 解析适用的 SessionPolicy.

    设计要点：
    - 启动时构建索引，运行时 O(1) 查找
    - 精确匹配优先：session_key > client_category > 无策略
    - 无侵入性：不匹配时返回 None，路由行为与现有一致
    - 运行时可变：支持 API 动态 upsert/remove session → vendor 绑定
    """

    def __init__(self, policies: list[SessionPolicy] | None = None) -> None:
        self._policies = policies or []
        self._key_index: dict[str, SessionPolicy] = {}
        self._category_index: dict[str, SessionPolicy] = {}
        self._lock = threading.Lock()
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
        with self._lock:
            policy = self._key_index.get(session_key)
        if policy:
            return policy
        return self._category_index.get(client_category)

    # ── 运行时 session → vendor 绑定 ──────────────────────────────

    def upsert(self, session_key: str, tier_names: list[str]) -> SessionPolicy:
        """为指定 session key 创建或替换运行时 vendor 绑定.

        运行时策略使用 ``runtime:`` 名称前缀，与配置文件驱动的策略区分。
        """
        policy = SessionPolicy(
            name=f"runtime:{session_key}",
            match=SessionPolicyMatch(session_keys=[session_key]),
            tiers=tier_names,
        )
        with self._lock:
            self._key_index[session_key] = policy
        logger.info(
            "Session vendor binding upserted: session_key=%s → %s",
            session_key,
            tier_names,
        )
        return policy

    def remove(self, session_key: str) -> bool:
        """删除指定 session key 的运行时 vendor 绑定.

        Returns:
            True 如果找到并删除了绑定，False 如果不存在。
        """
        with self._lock:
            policy = self._key_index.get(session_key)
            if policy is None or not policy.name.startswith("runtime:"):
                return False
            del self._key_index[session_key]
        logger.info("Session vendor binding removed: session_key=%s", session_key)
        return True

    def list_runtime_bindings(self) -> list[dict[str, str | list[str]]]:
        """返回所有运行时注入的绑定快照（仅 API 创建的，不含配置文件驱动的）."""
        with self._lock:
            return [
                {"session_key": key, "vendors": policy.tiers}
                for key, policy in self._key_index.items()
                if policy.name.startswith("runtime:")
            ]
