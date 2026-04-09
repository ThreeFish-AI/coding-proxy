"""原生 Anthropic 兼容端点薄透传代理 — 公共基类.

适用于所有仅需 模型名映射 + x-api-key 认证头替换 的供应商
（如智谱、MiniMax、Kimi、Doubao、Xiaomi、Alibaba 等）。

端点已完整支持 Anthropic Messages API 协议，本模块仅做两项最小适配：
  1. 模型名映射（Claude -> 供应商模型）
  2. 认证头替换（x-api-key）
"""

from __future__ import annotations

import copy
import json
import logging
from collections.abc import AsyncIterator
from typing import Any

import httpx

from ..config.schema import FailoverConfig
from ..routing.model_mapper import ModelMapper
from .base import (
    PROXY_SKIP_HEADERS,
    BaseVendor,
    VendorCapabilities,
    VendorResponse,
)

logger = logging.getLogger(__name__)


class NativeAnthropicVendor(BaseVendor):
    """原生 Anthropic 兼容端点薄透传代理基类.

    所有子类行为一致：
    1. 模型名映射（Claude → 供应商模型）
    2. 认证头替换（x-api-key）
    3. 401 错误归一化
    4. 能力声明全部为 NATIVE

    子类需覆写 ``_vendor_name`` 和 ``_display_name`` 类属性。
    """

    # ── 子类需覆写的类属性 ──────────────────────────────────
    _vendor_name: str = ""  # get_name() 返回值 & ModelMapper vendor 参数
    _display_name: str = ""  # 错误消息中的显示名（如 "Zhipu"、"MiniMax"）

    def __init__(
        self,
        config: Any,
        model_mapper: ModelMapper,
        failover_config: FailoverConfig | None = None,
    ) -> None:
        super().__init__(
            config.base_url, config.timeout_ms, failover_config=failover_config
        )
        self._api_key = config.api_key
        self._model_mapper = model_mapper

    def get_name(self) -> str:
        return self._vendor_name

    def get_capabilities(self) -> VendorCapabilities:
        return VendorCapabilities(
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
        """将 Claude 模型名映射为供应商模型名，完全委托 ModelMapper."""
        return self._model_mapper.map(model, vendor=self._vendor_name)

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
            k: v
            for k, v in headers.items()
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

    # ── 响应处理钩子 ──────────────────────────────────────

    async def send_message(
        self,
        request_body: dict[str, Any],
        headers: dict[str, str],
    ) -> VendorResponse:
        """最小化覆写：API key 缺失时快速返回 401 响应，其余委托基类（含 _normalize_error_response 钩子）."""
        if not self._api_key:
            raw = json.dumps(
                self._missing_api_key_payload(), ensure_ascii=False
            ).encode()
            return VendorResponse(
                status_code=401,
                raw_body=raw,
                error_type="authentication_error",
                error_message=f"{self._display_name} API key 未配置，无法访问 Claude 兼容端点",
                response_headers={"content-type": "application/json"},
            )
        return await super().send_message(request_body, headers)

    def _normalize_error_response(
        self,
        status_code: int,
        response: httpx.Response,
        backend_resp: VendorResponse,
    ) -> VendorResponse:
        """仅对 401 错误执行归一化，其余状态码透传."""
        if status_code != 401:
            return backend_resp
        raw_body, payload = self._normalize_backend_error(
            status_code,
            response.content if response else backend_resp.raw_body,
        )
        error = payload.get("error", {}) if isinstance(payload, dict) else {}
        return VendorResponse(
            status_code=status_code,
            raw_body=raw_body,
            error_type=error.get("type")
            if isinstance(error, dict)
            else "authentication_error",
            error_message=error.get("message")
            if isinstance(error, dict)
            else f"{self._display_name} API 认证失败",
            response_headers=backend_resp.response_headers,
            usage=backend_resp.usage,
            model_served=backend_resp.model_served,
        )

    # ── 流式 401 归一化包装 ───────────────────────────────

    async def send_message_stream(
        self,
        request_body: dict[str, Any],
        headers: dict[str, str],
    ) -> AsyncIterator[bytes]:
        """轻量包装：API key 缺失时快速失败，否则委托基类流式发送并捕获 401 归一化."""
        if not self._api_key:
            payload = self._missing_api_key_payload()
            raw = json.dumps(payload, ensure_ascii=False).encode()
            request = httpx.Request("POST", f"{self._base_url}{self._get_endpoint()}")
            raise httpx.HTTPStatusError(
                f"{self._vendor_name} API error: 401",
                request=request,
                response=httpx.Response(
                    401,
                    content=raw,
                    headers={"content-type": "application/json"},
                    request=request,
                ),
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

    # ── 401 错误归一化工具方法 ────────────────────────────

    def _normalize_auth_error_payload(
        self, payload: dict[str, Any] | None
    ) -> dict[str, Any]:
        default_msg = (
            f"{self._display_name} API 认证失败，请检查 api_key 或兼容端点权限"
        )
        if not isinstance(payload, dict):
            return {
                "error": {
                    "type": "authentication_error",
                    "message": default_msg,
                }
            }
        error = payload.get("error")
        if not isinstance(error, dict):
            payload["error"] = {
                "type": "authentication_error",
                "message": default_msg,
            }
            return payload
        payload["error"] = {
            **error,
            "type": "authentication_error",
            "message": str(error.get("message") or default_msg),
        }
        return payload

    def _missing_api_key_payload(self) -> dict[str, Any]:
        return {
            "error": {
                "type": "authentication_error",
                "message": f"{self._display_name} API key 未配置，无法访问 Claude 兼容端点",
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
