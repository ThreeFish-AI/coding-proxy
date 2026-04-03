"""智谱 GLM 后端 — 原生 Anthropic 兼容端点薄透传代理.

官方端点 (https://open.bigmodel.cn/api/anthropic) 已完整支持
Anthropic Messages API 协议，本模块仅做两项最小适配：
  1. 模型名映射（Claude → GLM）
  2. 认证头替换（x-api-key）
"""

from __future__ import annotations

import copy
import json
import logging
from typing import Any, AsyncIterator

import httpx

from ..config.schema import ZhipuConfig
from ..routing.model_mapper import ModelMapper
from .base import PROXY_SKIP_HEADERS, BackendCapabilities, BackendResponse, BaseBackend

logger = logging.getLogger(__name__)


class ZhipuBackend(BaseBackend):
    """智谱 GLM 原生 Anthropic 兼容端点后端（薄透传）.

    通过官方 /api/anthropic 端点转发请求，
    仅替换模型名和认证头，其余原样透传。
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
            supports_tools=True,
            supports_thinking=True,
            supports_images=True,
            emits_vendor_tool_events=False,
            supports_metadata=True,
        )

    def get_compatibility_profile(self):
        from ..compat.canonical import CompatibilityProfile, CompatibilityStatus

        return CompatibilityProfile(
            thinking=CompatibilityStatus.NATIVE,
            tool_calling=CompatibilityStatus.NATIVE,
            tool_streaming=CompatibilityStatus.NATIVE,
            mcp_tools=CompatibilityStatus.NATIVE,
            images=CompatibilityStatus.NATIVE,
            metadata=CompatibilityStatus.NATIVE,
            json_output=CompatibilityStatus.NATIVE,
            usage_tokens=CompatibilityStatus.NATIVE,
        )

    def map_model(self, model: str) -> str:
        """将 Claude 模型名映射为智谱模型名，完全委托 ModelMapper."""
        return self._model_mapper.map(model, backend="zhipu")

    async def _prepare_request(
        self,
        request_body: dict[str, Any],
        headers: dict[str, str],
    ) -> tuple[dict[str, Any], dict[str, str]]:
        """深拷贝请求体、映射模型名、替换认证头.

        其余字段（tools, thinking, metadata, system 等）原样透传。
        """
        body = copy.deepcopy(request_body)

        if "model" in body:
            body["model"] = self.map_model(body["model"])

        # 剥离原始认证头（authorization / x-api-key），由下方 new_headers 重建
        filtered = {
            k: v for k, v in headers.items()
            if k.lower() not in PROXY_SKIP_HEADERS
            and k.lower() not in ("x-api-key", "authorization")
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

    # ── 401 错误归一化 ──────────────────────────────────────

    @staticmethod
    def _normalize_auth_error_payload(payload: dict[str, Any] | None) -> dict[str, Any]:
        if not isinstance(payload, dict):
            return {
                "error": {
                    "type": "authentication_error",
                    "message": "Zhipu API 认证失败，请检查 api_key 或兼容端点权限",
                }
            }
        error = payload.get("error")
        if not isinstance(error, dict):
            payload["error"] = {
                "type": "authentication_error",
                "message": "Zhipu API 认证失败，请检查 api_key 或兼容端点权限",
            }
            return payload
        payload["error"] = {
            **error,
            "type": "authentication_error",
            "message": str(error.get("message") or "Zhipu API 认证失败，请检查 api_key 或兼容端点权限"),
        }
        return payload

    @staticmethod
    def _missing_api_key_payload() -> dict[str, Any]:
        return {
            "error": {
                "type": "authentication_error",
                "message": "Zhipu API key 未配置，无法访问 Claude 兼容端点",
            }
        }

    def _normalize_backend_error(
        self,
        status_code: int,
        raw_body: bytes,
    ) -> tuple[bytes, dict[str, Any] | None]:
        payload: dict[str, Any] | None = None
        if raw_body:
            try:
                decoded = json.loads(raw_body)
                if isinstance(decoded, dict):
                    payload = decoded
            except (json.JSONDecodeError, UnicodeDecodeError, TypeError):
                payload = None
        if status_code != 401:
            return raw_body, payload
        payload = self._normalize_auth_error_payload(payload)
        body = copy.deepcopy(payload)
        return json.dumps(body, ensure_ascii=False).encode(), body

    # ── 请求发送（覆写基类以处理 401 归一化） ───────────────

    async def send_message_stream(
        self,
        request_body: dict[str, Any],
        headers: dict[str, str],
    ) -> AsyncIterator[bytes]:
        if not self._api_key:
            payload = self._missing_api_key_payload()
            raw = json.dumps(payload, ensure_ascii=False).encode()
            request = httpx.Request("POST", f"{self._base_url}{self._get_endpoint()}")
            raise httpx.HTTPStatusError(
                "zhipu API error: 401",
                request=request,
                response=httpx.Response(401, content=raw, headers={"content-type": "application/json"}, request=request),
            )
        try:
            async for chunk in super().send_message_stream(request_body, headers):
                yield chunk
        except httpx.HTTPStatusError as exc:
            response = exc.response
            if response is not None and response.status_code == 401:
                raw_body = response.content or b"{}"
                normalized_raw, _ = self._normalize_backend_error(
                    response.status_code,
                    raw_body,
                )
                raise httpx.HTTPStatusError(
                    str(exc),
                    request=exc.request,
                    response=httpx.Response(
                        response.status_code,
                        content=normalized_raw,
                        headers=dict(response.headers),
                        request=exc.request,
                    ),
                ) from exc
            raise

    async def send_message(
        self,
        request_body: dict[str, Any],
        headers: dict[str, str],
    ) -> BackendResponse:
        if not self._api_key:
            raw = json.dumps(self._missing_api_key_payload(), ensure_ascii=False).encode()
            return BackendResponse(
                status_code=401,
                raw_body=raw,
                error_type="authentication_error",
                error_message="Zhipu API key 未配置，无法访问 Claude 兼容端点",
                response_headers={"content-type": "application/json"},
            )
        response = await super().send_message(request_body, headers)
        if response.status_code != 401:
            return response
        raw_body, payload = self._normalize_backend_error(
            response.status_code,
            response.raw_body,
        )
        error = payload.get("error", {}) if isinstance(payload, dict) else {}
        return BackendResponse(
            status_code=response.status_code,
            raw_body=raw_body,
            error_type=error.get("type") if isinstance(error, dict) else "authentication_error",
            error_message=error.get("message") if isinstance(error, dict) else "Zhipu API 认证失败",
            response_headers=response.response_headers,
            usage=response.usage,
            model_served=response.model_served,
        )
