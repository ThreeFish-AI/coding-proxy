"""智谱 GLM 后端 — 使用 API Key 认证."""

from __future__ import annotations

import logging
from typing import Any, AsyncIterator

from ..compat.canonical import CompatibilityProfile, CompatibilityStatus
from ..config.schema import ZhipuConfig
from ..routing.model_mapper import ModelMapper
from ..streaming.anthropic_compat import normalize_anthropic_compatible_stream
from .base import BackendCapabilities, BaseBackend

logger = logging.getLogger(__name__)


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

    def get_capabilities(self) -> BackendCapabilities:
        return BackendCapabilities(
            supports_tools=True,              # GLM 支持 function calling
            supports_thinking=True,           # GLM-5.1 原生支持深度思考
            supports_images=True,
            emits_vendor_tool_events=False,   # normalize_anthropic_compatible_stream 已规范化输出
            supports_metadata=True,           # metadata 在 _prepare_request 中静默剥离
        )

    def get_compatibility_profile(self) -> CompatibilityProfile:
        return CompatibilityProfile(
            thinking=CompatibilityStatus.NATIVE,
            tool_calling=CompatibilityStatus.NATIVE,
            tool_streaming=CompatibilityStatus.SIMULATED,
            mcp_tools=CompatibilityStatus.SIMULATED,
            images=CompatibilityStatus.NATIVE,
            metadata=CompatibilityStatus.SIMULATED,
            json_output=CompatibilityStatus.SIMULATED,
            usage_tokens=CompatibilityStatus.SIMULATED,
        )

    def map_model(self, model: str) -> str:
        """将 Claude 模型名映射为智谱模型名."""
        return self._model_mapper.map(model, backend="zhipu")

    async def _prepare_request(
        self,
        request_body: dict[str, Any],
        headers: dict[str, str],
    ) -> tuple[dict[str, Any], dict[str, str]]:
        """映射模型名、替换认证头，并剥离智谱 API 不支持的字段."""
        body = {**request_body}
        metadata = body.pop("metadata", None)
        # 将 Anthropic thinking 格式转换为智谱格式（剥离 budget_tokens）
        thinking = body.get("thinking")
        if isinstance(thinking, dict):
            body["thinking"] = {"type": thinking.get("type", "enabled")}
        if "model" in body:
            body["model"] = self._model_mapper.map(body["model"], backend="zhipu")

        if isinstance(metadata, dict):
            user_id = metadata.get("user_id")
            if isinstance(user_id, str) and user_id:
                body["user_id"] = user_id

        request_id = body.get("request_id")
        if not isinstance(request_id, str) and self._compat_trace is not None:
            body["request_id"] = self._compat_trace.trace_id

        response_format = body.get("response_format")
        if isinstance(response_format, dict) and response_format.get("type"):
            body["response_format"] = response_format

        # 诊断：记录工具调用请求信息
        tools = body.get("tools", [])
        if tools:
            tool_names = [t.get("name", "?") for t in tools if isinstance(t, dict)]
            logger.debug("Zhipu request with %d tools: %s", len(tools), tool_names)

        new_headers = {
            "content-type": "application/json",
            "x-api-key": self._api_key,
            "anthropic-version": headers.get("anthropic-version", "2023-06-01"),
        }
        return body, new_headers

    def get_diagnostics(self) -> dict[str, Any]:
        return super().get_diagnostics()

    async def send_message_stream(
        self,
        request_body: dict[str, Any],
        headers: dict[str, str],
    ) -> AsyncIterator[bytes]:
        upstream = super().send_message_stream(request_body, headers)
        async for chunk in normalize_anthropic_compatible_stream(
            upstream, model=self.map_model(request_body.get("model", "unknown")),
        ):
            yield chunk
