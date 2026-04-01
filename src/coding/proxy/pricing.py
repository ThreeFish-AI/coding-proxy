"""LiteLLM 模型定价缓存.

从 LiteLLM 官方 JSON 拉取最新单价数据，按 (backend, model_served) 计算 Cost。
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass

import httpx

logger = logging.getLogger(__name__)

LITELLM_PRICING_URL = (
    "https://raw.githubusercontent.com/BerriAI/litellm"
    "/main/model_prices_and_context_window.json"
)

# 后端名称 → LiteLLM litellm_provider 字段值
# copilot  : GitHub Copilot 透传 Claude 模型，参照 Anthropic 官方定价估算
# antigravity: Google Antigravity 基于 Vertex AI 提供 Claude
# fallback : 智谱 GLM，不限 provider，按模型名直查
_BACKEND_PROVIDER: dict[str, str] = {
    "anthropic": "anthropic",
    "copilot": "anthropic",
    "antigravity": "vertex_ai",
    "fallback": "",
}


def _normalize(name: str) -> str:
    """规范化模型名称以提升匹配成功率.

    规则：
    - 去除 @版本后缀（如 @20241022）
    - 将 `.` 替换为 `-`
    - 转小写
    """
    name = re.sub(r"@[\w.]+$", "", name)
    return name.replace(".", "-").lower()


@dataclass
class ModelPricing:
    """单个模型的 Token 单价（USD/token）."""

    input_cost_per_token: float = 0.0
    output_cost_per_token: float = 0.0
    cache_creation_input_token_cost: float = 0.0
    cache_read_input_token_cost: float = 0.0


def _entry_to_pricing(entry: dict) -> ModelPricing:
    return ModelPricing(
        input_cost_per_token=float(entry.get("input_cost_per_token") or 0.0),
        output_cost_per_token=float(entry.get("output_cost_per_token") or 0.0),
        cache_creation_input_token_cost=float(
            entry.get("cache_creation_input_token_cost") or 0.0
        ),
        cache_read_input_token_cost=float(
            entry.get("cache_read_input_token_cost") or 0.0
        ),
    )


class PricingCache:
    """LiteLLM 定价缓存，支持按 (backend, model_served) 查询单价."""

    def __init__(self) -> None:
        self._raw: dict = {}
        # (litellm_provider, normalized_model_name) → ModelPricing
        self._index: dict[tuple[str, str], ModelPricing] = {}

    # ── 数据获取 ──────────────────────────────────────────────

    async def fetch(self, timeout: float = 10.0) -> bool:
        """从 LiteLLM 官方获取最新定价 JSON.

        失败时仅打印 warning，返回 False，不抛出异常。
        """
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                resp = await client.get(LITELLM_PRICING_URL)
                resp.raise_for_status()
                self._raw = resp.json()
            self._build_index()
            logger.info("定价数据加载成功，共 %d 条模型记录", len(self._raw))
            return True
        except Exception as exc:  # noqa: BLE001
            logger.warning("定价数据获取失败，Cost 列将显示 '-': %s", exc)
            return False

    def _build_index(self) -> None:
        """构建 (provider, normalized_name) → ModelPricing 双索引."""
        self._index.clear()
        for raw_key, entry in self._raw.items():
            if not isinstance(entry, dict):
                continue
            provider: str = entry.get("litellm_provider", "") or ""
            pricing = _entry_to_pricing(entry)

            # raw_key 可能形如 "vertex_ai/claude-3-5-sonnet@20241022"
            # 取 "/" 之后的部分作为 model_name
            model_part = raw_key.split("/", 1)[-1] if "/" in raw_key else raw_key

            for name in {raw_key, model_part, _normalize(raw_key), _normalize(model_part)}:
                key = (provider, name)
                if key not in self._index:  # 先写入的优先（保留精确匹配）
                    self._index[key] = pricing

    # ── 单价查询 ──────────────────────────────────────────────

    def get_pricing(self, backend: str, model_served: str) -> ModelPricing | None:
        """获取 (backend, model_served) 对应的 ModelPricing.

        查找顺序（逐步放宽）：
        1. 精确匹配：(provider, model_served)
        2. 精确匹配：(provider, normalized(model_served))
        3. 前缀匹配：provider 吻合且 indexed_name 以 normalized(model_served) 开头
        4. 无 provider 限制的精确/规范化匹配（适用 fallback/zhipu）
        5. 返回 None
        """
        if not self._raw:
            return None

        provider = _BACKEND_PROVIDER.get(backend, "")
        norm = _normalize(model_served)

        # 策略 1 & 2：精确 / 规范化精确
        for name in (model_served, norm):
            hit = self._index.get((provider, name))
            if hit is not None:
                return hit

        # 策略 3：前缀匹配（同 provider 下 key 以 norm 开头）
        for (p, n), pricing in self._index.items():
            if p == provider and n.startswith(norm):
                return pricing

        # 策略 4：忽略 provider 的兜底（适用 fallback 后端）
        if provider:
            for name in (model_served, norm):
                hit = self._index.get(("", name))
                if hit is not None:
                    return hit
            for (p, n), pricing in self._index.items():
                if not p and n.startswith(norm):
                    return pricing

        return None

    # ── 费用计算 ──────────────────────────────────────────────

    def compute_cost(
        self,
        backend: str,
        model_served: str,
        input_tokens: int,
        output_tokens: int,
        cache_creation_tokens: int,
        cache_read_tokens: int,
    ) -> float | None:
        """按单价计算总费用（USD）.

        若无匹配定价返回 None。
        """
        pricing = self.get_pricing(backend, model_served)
        if pricing is None:
            return None
        return (
            input_tokens * pricing.input_cost_per_token
            + output_tokens * pricing.output_cost_per_token
            + cache_creation_tokens * pricing.cache_creation_input_token_cost
            + cache_read_tokens * pricing.cache_read_input_token_cost
        )
