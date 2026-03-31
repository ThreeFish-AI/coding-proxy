"""智谱 GLM 后端 — 使用 API Key 认证."""

from __future__ import annotations

from typing import Any

from ..config.schema import ZhipuConfig
from ..routing.model_mapper import ModelMapper
from .base import BaseBackend


class ZhipuBackend(BaseBackend):
    """智谱 GLM API 后端（终端 fallback）.

    使用 Anthropic 兼容接口，将请求转发到智谱 API.
    替换认证头和模型名称.
    """

    def __init__(
        self,
        config: ZhipuConfig,
        model_mapper: ModelMapper,
    ) -> None:
        super().__init__(config.base_url, config.timeout_ms)
        self._api_key = config.api_key
        self._model_mapper = model_mapper

    def get_name(self) -> str:
        return "zhipu"

    def _prepare_request(
        self,
        request_body: dict[str, Any],
        headers: dict[str, str],
    ) -> tuple[dict[str, Any], dict[str, str]]:
        """映射模型名、替换认证头."""
        body = {**request_body}
        if "model" in body:
            body["model"] = self._model_mapper.map(body["model"])

        new_headers = {
            "content-type": "application/json",
            "x-api-key": self._api_key,
            "anthropic-version": headers.get("anthropic-version", "2023-06-01"),
        }
        return body, new_headers
