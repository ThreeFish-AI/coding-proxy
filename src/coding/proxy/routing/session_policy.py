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
        self._config_key_backup: dict[str, SessionPolicy] = {}
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
        """返回匹配的策略，优先精确 session_key 匹配，其次 category 匹配.

        返回的 SessionPolicy 对象应为不可变引用；调用方不应修改其内部属性，
        否则在并发 upsert/remove 场景下可能产生竞态。
        """
        with self._lock:
            policy = self._key_index.get(session_key)
        if policy:
            return policy
        return self._category_index.get(client_category)

    # ── 运行时 session → vendor 绑定 ──────────────────────────────

    def upsert(
        self,
        session_key: str,
        tier_names: list[str],
        model: str | None = None,
    ) -> SessionPolicy:
        """为指定 session key 创建或替换运行时绑定（vendor 优先级 + 可选模型覆盖）.

        运行时策略使用 ``runtime:`` 名称前缀，与配置文件驱动的策略区分。
        tier_names 为空（仅绑模型）时继承该会话原本会命中的配置策略 tiers
        （被遮蔽的 key 级策略优先，其次 category 级），兑现「不改 vendor 顺序」
        语义；无配置策略时保持空 = 全局默认顺序。
        """
        with self._lock:
            existing = self._key_index.get(session_key)
            if existing and not existing.name.startswith("runtime:"):
                self._config_key_backup[session_key] = existing
            if not tier_names:
                base = self._config_key_backup.get(session_key)
                if base is None:
                    base = self._category_index.get("cc")
                if base is not None and base.tiers:
                    tier_names = list(base.tiers)
            policy = SessionPolicy(
                name=f"runtime:{session_key}",
                match=SessionPolicyMatch(session_keys=[session_key]),
                tiers=tier_names,
                model=model,
            )
            self._key_index[session_key] = policy
        logger.info(
            "Session vendor binding upserted: session_key=%s → tiers=%s model=%s",
            session_key,
            tier_names,
            model or "-",
        )
        return policy

    def get_runtime_binding(self, session_key: str) -> SessionPolicy | None:
        """返回指定 key 的运行时绑定（仅 API 创建的），不存在或为配置态时返回 None."""
        with self._lock:
            policy = self._key_index.get(session_key)
        if policy is not None and policy.name.startswith("runtime:"):
            return policy
        return None

    def remove(self, session_key: str) -> bool:
        """删除指定 session key 的运行时绑定.

        Returns:
            True 如果找到并删除了绑定，False 如果不存在。
        """
        with self._lock:
            policy = self._key_index.get(session_key)
            if policy is None or not policy.name.startswith("runtime:"):
                return False
            del self._key_index[session_key]
            # 恢复被运行时绑定覆盖的配置策略
            backup = self._config_key_backup.pop(session_key, None)
            if backup is not None:
                self._key_index[session_key] = backup
        logger.info("Session vendor binding removed: session_key=%s", session_key)
        return True

    def list_runtime_bindings(self) -> list[dict[str, str | list[str] | None]]:
        """返回所有运行时注入的绑定快照（仅 API 创建的，不含配置文件驱动的）."""
        with self._lock:
            return [
                {
                    "session_key": key,
                    "vendors": policy.tiers,
                    "model": policy.model,
                }
                for key, policy in self._key_index.items()
                if policy.name.startswith("runtime:")
            ]
