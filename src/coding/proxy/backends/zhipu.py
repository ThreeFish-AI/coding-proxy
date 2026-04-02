"""智谱 GLM 后端 — 使用 API Key 认证."""

from __future__ import annotations

import copy
import logging
from typing import Any, AsyncIterator

from ..compat.canonical import CompatibilityProfile, CompatibilityStatus
from ..compat.session_store import CompatSessionRecord
from ..config.schema import ZhipuConfig
from ..routing.model_mapper import ModelMapper
from ..streaming.anthropic_compat import normalize_anthropic_compatible_stream
from .base import PROXY_SKIP_HEADERS, BackendCapabilities, BaseBackend

logger = logging.getLogger(__name__)

_HAIKU_PREFIX = "claude-haiku-"
_OPUS_PREFIX = "claude-opus-"
_SONNET_PREFIX = "claude-sonnet-"


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
        self._last_request_adaptations: list[str] = []
        self._last_tool_choice_mode: str = ""
        self._last_requested_model: str = ""
        self._last_resolved_model: str = ""
        self._last_tool_names: list[str] = []

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
            thinking=CompatibilityStatus.SIMULATED,
            tool_calling=CompatibilityStatus.SIMULATED,
            tool_streaming=CompatibilityStatus.SIMULATED,
            mcp_tools=CompatibilityStatus.SIMULATED,
            images=CompatibilityStatus.NATIVE,
            metadata=CompatibilityStatus.SIMULATED,
            json_output=CompatibilityStatus.SIMULATED,
            usage_tokens=CompatibilityStatus.SIMULATED,
        )

    def map_model(self, model: str) -> str:
        """将 Claude 模型名映射为智谱模型名."""
        return self._model_mapper.map(
            model,
            backend="zhipu",
            default=self._default_target_model(model),
        )

    @staticmethod
    def _default_target_model(model: str) -> str:
        value = (model or "").strip()
        if value.startswith(_HAIKU_PREFIX):
            return "glm-4.5-air"
        if value.startswith(_OPUS_PREFIX) or value.startswith(_SONNET_PREFIX):
            return "glm-5v-turbo"
        return "glm-5v-turbo"

    @staticmethod
    def _extract_tool_names(tools: list[dict[str, Any]]) -> list[str]:
        names: list[str] = []
        for tool in tools:
            if not isinstance(tool, dict):
                continue
            name = tool.get("name")
            if isinstance(name, str) and name:
                names.append(name)
        return names

    @staticmethod
    def _derive_tool_call_map(messages: list[dict[str, Any]]) -> dict[str, str]:
        tool_call_map: dict[str, str] = {}
        for message in messages:
            if not isinstance(message, dict):
                continue
            content = message.get("content")
            if not isinstance(content, list):
                continue
            for block in content:
                if not isinstance(block, dict):
                    continue
                if block.get("type") != "tool_use":
                    continue
                tool_id = block.get("id")
                name = block.get("name")
                if isinstance(tool_id, str) and tool_id and isinstance(name, str) and name:
                    tool_call_map[tool_id] = name
        return tool_call_map

    def _remember_session_projection(
        self,
        *,
        body: dict[str, Any],
        tool_choice_mode: str,
        request_id: str,
    ) -> None:
        if self._compat_session_record is None:
            return
        record: CompatSessionRecord = self._compat_session_record
        record.tool_call_map.update(self._derive_tool_call_map(body.get("messages", [])))
        record.provider_state[self.get_name()] = {
            "tool_choice_mode": tool_choice_mode,
            "thinking_enabled": bool(body.get("thinking")),
            "response_format_type": (
                body.get("response_format", {}).get("type")
                if isinstance(body.get("response_format"), dict) else ""
            ),
            "request_id": request_id,
            "resolved_model": body.get("model", ""),
            "tool_names": self._extract_tool_names(body.get("tools", [])),
        }

    def _prepare_tools(
        self,
        body: dict[str, Any],
        adaptations: list[str],
    ) -> str:
        tools = body.get("tools")
        if not isinstance(tools, list):
            body.pop("tool_choice", None)
            return ""

        valid_tools = [tool for tool in tools if isinstance(tool, dict) and tool.get("name")]
        if len(valid_tools) != len(tools):
            body["tools"] = valid_tools
            adaptations.append("invalid_tools_filtered")
        if not valid_tools:
            body.pop("tools", None)
            body.pop("tool_choice", None)
            adaptations.append("tool_choice_removed_without_tools")
            return "none"

        tool_choice = body.get("tool_choice")
        if not isinstance(tool_choice, dict):
            return "auto"

        choice_type = str(tool_choice.get("type", "")).lower()
        if choice_type == "tool":
            name = tool_choice.get("name")
            if isinstance(name, str) and name:
                narrowed = [tool for tool in valid_tools if tool.get("name") == name]
                if narrowed:
                    if len(narrowed) != len(valid_tools):
                        body["tools"] = narrowed
                        adaptations.append("tool_choice_tool_narrowed_tools")
                    return "tool"
            body["tool_choice"] = {"type": "auto"}
            adaptations.append("tool_choice_tool_downgraded_to_auto")
            return "auto"

        if choice_type == "any":
            adaptations.append("tool_choice_any_preserved")
            return "any"

        if choice_type == "none":
            body.pop("tools", None)
            adaptations.append("tool_choice_none_removed_tools")
            return "none"

        if choice_type == "auto":
            return "auto"

        body["tool_choice"] = {"type": "auto"}
        adaptations.append("tool_choice_unknown_downgraded_to_auto")
        return "auto"

    async def _prepare_request(
        self,
        request_body: dict[str, Any],
        headers: dict[str, str],
    ) -> tuple[dict[str, Any], dict[str, str]]:
        """映射模型名、替换认证头，并剥离智谱 API 不支持的字段."""
        body = copy.deepcopy(request_body)
        adaptations: list[str] = []
        metadata = body.pop("metadata", None)
        # 将 Anthropic thinking 格式转换为智谱格式（剥离 budget_tokens）
        thinking = body.get("thinking")
        if isinstance(thinking, dict):
            body["thinking"] = {"type": thinking.get("type", "enabled")}
            if "budget_tokens" in thinking:
                adaptations.append("thinking_budget_tokens_stripped")
            if "effort" in thinking:
                adaptations.append("thinking_effort_stripped")
        extended_thinking = body.pop("extended_thinking", None)
        if isinstance(extended_thinking, dict) and "thinking" not in body:
            body["thinking"] = {"type": extended_thinking.get("type", "enabled")}
            adaptations.append("extended_thinking_collapsed")
            if "budget_tokens" in extended_thinking:
                adaptations.append("thinking_budget_tokens_stripped")
            if "effort" in extended_thinking:
                adaptations.append("thinking_effort_stripped")
        if "model" in body:
            self._last_requested_model = str(body["model"])
            body["model"] = self.map_model(self._last_requested_model)
            self._last_resolved_model = str(body["model"])

        if isinstance(metadata, dict):
            user_id = metadata.get("user_id")
            if isinstance(user_id, str) and user_id:
                body["user_id"] = user_id
                adaptations.append("metadata_user_id_projected")

        request_id = body.get("request_id")
        if not isinstance(request_id, str) and self._compat_trace is not None:
            body["request_id"] = self._compat_trace.trace_id
            adaptations.append("request_id_injected_from_trace")
        request_id = str(body.get("request_id", ""))

        response_format = body.get("response_format")
        if isinstance(response_format, dict) and response_format.get("type"):
            body["response_format"] = response_format
            adaptations.append("response_format_preserved")

        self._last_tool_choice_mode = self._prepare_tools(body, adaptations)
        tools = body.get("tools", [])
        self._last_tool_names = self._extract_tool_names(tools if isinstance(tools, list) else [])
        if self._last_tool_names:
            logger.debug(
                "Zhipu request with %d tools: %s (tool_choice=%s)",
                len(self._last_tool_names), self._last_tool_names, self._last_tool_choice_mode or "auto",
            )

        self._remember_session_projection(
            body=body,
            tool_choice_mode=self._last_tool_choice_mode or "auto",
            request_id=request_id,
        )

        self._last_request_adaptations = sorted(set(adaptations))
        if self._compat_trace is not None:
            self._compat_trace.request_adaptations = list(self._last_request_adaptations)

        filtered = {
            k: v for k, v in headers.items()
            if k.lower() not in PROXY_SKIP_HEADERS and k.lower() != "x-api-key"
        }
        new_headers = {
            "content-type": "application/json",
            "x-api-key": self._api_key,
            "anthropic-version": headers.get("anthropic-version", "2023-06-01"),
        }
        for key, value in filtered.items():
            if key.lower() not in {item.lower() for item in new_headers}:
                new_headers[key] = value
        return body, new_headers

    def get_diagnostics(self) -> dict[str, Any]:
        diagnostics = super().get_diagnostics()
        if self._last_request_adaptations:
            diagnostics["request_adaptations"] = self._last_request_adaptations
        if self._last_tool_choice_mode:
            diagnostics["tool_choice_projection"] = self._last_tool_choice_mode
        if self._last_requested_model:
            diagnostics["requested_model"] = self._last_requested_model
        if self._last_resolved_model:
            diagnostics["resolved_model"] = self._last_resolved_model
        if self._last_tool_names:
            diagnostics["tool_names"] = self._last_tool_names
        return diagnostics

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
