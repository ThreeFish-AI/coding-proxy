"""GitHub Copilot 供应商 — 内置 token 交换与 Anthropic 兼容转发."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from typing import Any
from uuid import uuid4

import httpx

from ..compat.canonical import CompatibilityProfile, CompatibilityStatus
from ..config.schema import CopilotConfig, FailoverConfig
from ..convert.anthropic_to_openai import convert_request as convert_openai_request
from ..convert.openai_to_anthropic import convert_response as convert_openai_response
from ..routing.model_mapper import ModelMapper
from ..streaming.anthropic_compat import normalize_anthropic_compatible_stream
from .base import (
    PROXY_SKIP_HEADERS,
    BaseVendor,
    CapabilityLossReason,
    RequestCapabilities,
    UsageInfo,
    VendorCapabilities,
    VendorResponse,
    _decode_json_body,
    _extract_error_message,
)
from .copilot_models import (  # noqa: F401
    CopilotMisdirectedRequest,
    CopilotModelResolver,
    _copilot_model_family,
    _copilot_model_major,
    _copilot_model_version_rank,
    _select_copilot_model,
    normalize_copilot_requested_model,
)
from .copilot_token_manager import CopilotTokenManager
from .copilot_urls import (  # noqa: F401
    _EDITOR_PLUGIN_VERSION,
    _EDITOR_VERSION,
    _GITHUB_API_VERSION,
    _USER_AGENT,
    _normalize_base_url,
    build_copilot_candidate_base_urls,
    resolve_copilot_base_url,
)

# Copilot421RetryHandler 已从 copilot_retry.py 合并至本文件末尾
from .mixins import TokenBackendMixin

logger = logging.getLogger(__name__)


# ── Copilot 421 Misdirected 重试处理器（原 copilot_retry.py） ──


class Copilot421RetryHandler:
    """封装 Copilot 421 Misdirected 重试策略.

    GitHub Copilot API 在某些情况下返回 421 Misdirected Request，
    表示当前端点不可用，需尝试其他候选 URL。此处理器统一了
    同步请求和流式请求的 421 重试逻辑。
    """

    def __init__(self, backend: Any) -> None:
        self._backend = backend

    async def execute_request_with_retry(
        self,
        method: str,
        endpoint: str,
        *,
        headers: dict[str, str],
        json_body: dict[str, Any] | None = None,
    ) -> httpx.Response:
        """同步请求的 421 重试."""
        current_base_url = self._backend._resolved_base_url
        self._backend._begin_request(current_base_url)

        response = await self._backend._get_client().request(
            method,
            endpoint,
            json=json_body,
            headers=headers,
        )
        if response.status_code != 421:
            return response

        self._backend._last_421_base_url = current_base_url
        last_response = response

        for retry_base_url in self._backend._retry_base_urls(current_base_url):
            self._backend._last_retry_base_url = retry_base_url
            async with self._backend._create_fresh_client(
                retry_base_url
            ) as retry_client:
                retry_response = await retry_client.request(
                    method,
                    endpoint,
                    json=json_body,
                    headers=headers,
                )
            last_response = retry_response
            if retry_response.status_code != 421:
                await self._backend._activate_base_url(retry_base_url)
                return retry_response
            self._backend._last_421_base_url = retry_base_url

        return last_response

    async def execute_stream_with_retry(
        self,
        stream_fn: Any,
    ) -> AsyncIterator[bytes]:
        """流式请求的 421 重试（异步生成器）.

        Args:
            stream_fn: 接受 httpx.AsyncClient 并返回 AsyncIterator[bytes] 的可调用对象
        """
        current_base_url = self._backend._resolved_base_url
        self._backend._begin_request(current_base_url)
        last_exc: httpx.HTTPStatusError | None = None

        try:
            async for chunk in stream_fn(self._backend._get_client()):
                yield chunk
            return
        except httpx.HTTPStatusError as exc:
            if exc.response is None or exc.response.status_code != 421:
                raise
            self._backend._last_421_base_url = _normalize_base_url(current_base_url)
            last_exc = exc

        for retry_base_url in self._backend._retry_base_urls(current_base_url):
            self._backend._last_retry_base_url = retry_base_url
            async with self._backend._create_fresh_client(
                retry_base_url
            ) as retry_client:
                try:
                    async for chunk in stream_fn(retry_client):
                        yield chunk
                    await self._backend._activate_base_url(retry_base_url)
                    return
                except httpx.HTTPStatusError as retry_exc:
                    last_exc = retry_exc
                    if (
                        retry_exc.response is None
                        or retry_exc.response.status_code != 421
                    ):
                        raise
                    self._backend._last_421_base_url = retry_base_url

        if last_exc:
            raise last_exc


class CopilotVendor(TokenBackendMixin, BaseVendor):
    """GitHub Copilot API 供应商.

    通过内置 token 交换访问 GitHub Copilot 的 Anthropic 兼容端点.
    模型解析：优先使用配置规则（model_mapping），其次依赖内部家族匹配策略.
    """

    def __init__(
        self,
        config: CopilotConfig,
        failover_config: FailoverConfig,
        model_mapper: ModelMapper | None = None,
    ) -> None:
        self._account_type = (config.account_type or "individual").strip().lower()
        self._configured_base_url = config.base_url
        self._candidate_base_urls = build_copilot_candidate_base_urls(
            self._account_type, config.base_url
        )
        self._resolved_base_url = resolve_copilot_base_url(
            self._account_type, config.base_url
        )
        # 模型解析委托给 CopilotModelResolver 策略类
        self._model_resolver = CopilotModelResolver(
            models_cache_ttl_seconds=int(config.models_cache_ttl_seconds),
            model_mapper=model_mapper,
        )
        # Copilot 特有诊断字段（不在 Mixin 中）
        self._last_request_base_url = ""
        self._last_421_base_url = ""
        self._last_retry_base_url = ""
        # 421 重试处理器
        self._421_handler = Copilot421RetryHandler(self)
        # TokenBackendMixin 诊断字段（_last_requested_model / _last_resolved_model /
        # _last_model_resolution_reason / _last_request_adaptations）由 Mixin 提供
        token_manager = CopilotTokenManager(config.github_token, config.token_url)
        TokenBackendMixin.__init__(self, token_manager)
        BaseVendor.__init__(
            self, self._resolved_base_url, config.timeout_ms, failover_config
        )

    def get_name(self) -> str:
        return "copilot"

    def get_capabilities(self) -> VendorCapabilities:
        return VendorCapabilities(
            supports_tools=True,
            supports_thinking=True,
            supports_images=True,
            emits_vendor_tool_events=False,
            supports_metadata=True,
        )

    def get_compatibility_profile(self) -> CompatibilityProfile:
        return CompatibilityProfile(
            thinking=CompatibilityStatus.SIMULATED,
            tool_calling=CompatibilityStatus.NATIVE,
            tool_streaming=CompatibilityStatus.NATIVE,
            mcp_tools=CompatibilityStatus.UNKNOWN,
            images=CompatibilityStatus.NATIVE,
            metadata=CompatibilityStatus.SIMULATED,
            json_output=CompatibilityStatus.NATIVE,
            usage_tokens=CompatibilityStatus.SIMULATED,
        )

    def supports_request(
        self,
        request_caps: RequestCapabilities,
    ) -> tuple[bool, list[CapabilityLossReason]]:
        """Copilot 可通过适配层吸收 thinking 语义，不在路由阶段直接拒绝."""
        supported, reasons = super().supports_request(request_caps)
        if not supported:
            reasons = [
                reason
                for reason in reasons
                if reason is not CapabilityLossReason.THINKING
            ]
        return len(reasons) == 0, reasons

    def _get_endpoint(self) -> str:
        return "/chat/completions"

    def _build_copilot_headers(self) -> dict[str, str]:
        return {
            "copilot-integration-id": "vscode-chat",
            "editor-version": _EDITOR_VERSION,
            "editor-plugin-version": _EDITOR_PLUGIN_VERSION,
            "user-agent": _USER_AGENT,
            "openai-intent": "conversation-panel",
            "x-github-api-version": _GITHUB_API_VERSION,
            "x-request-id": str(uuid4()),
            "x-vscode-user-agent-library-version": "electron-fetch",
            "content-type": "application/json",
        }

    @staticmethod
    def _resolve_initiator(request_body: dict[str, Any]) -> str:
        for message in request_body.get("messages", []):
            if message.get("role") in {"assistant", "tool"}:
                return "agent"
        return "user"

    @staticmethod
    def _collect_request_adaptations(request_body: dict[str, Any]) -> list[str]:
        adaptations: list[str] = []

        extended_thinking = request_body.get("extended_thinking")
        thinking = request_body.get("thinking")

        if isinstance(extended_thinking, dict):
            effort = extended_thinking.get("effort", "unknown")
            budget = extended_thinking.get("budget_tokens")
            label = f"extended_thinking_mapped_to_reasoning_effort(effort={effort})"
            if isinstance(budget, int) and budget > 0:
                label += f",budget_tokens_not_supported({budget})"
            adaptations.append(label)
        elif thinking is True or isinstance(thinking, dict):
            adaptations.append("thinking_mapped_to_reasoning_effort(medium)")

        for message in request_body.get("messages", []):
            content = message.get("content")
            if not isinstance(content, list):
                continue
            has_thinking_block = any(
                isinstance(block, dict) and block.get("type") == "thinking"
                for block in content
            )
            has_text_block = any(
                isinstance(block, dict) and block.get("type") == "text"
                for block in content
            )
            if has_thinking_block and has_text_block:
                adaptations.append("thinking_block_prefixed_as_context")
            elif has_thinking_block:
                adaptations.append("thinking_block_used_as_content_fallback")
                break

        return adaptations

    def _create_fresh_client(self, base_url: str) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            base_url=base_url,
            timeout=httpx.Timeout(self._timeout_ms / 1000.0),
        )

    async def _activate_base_url(self, base_url: str) -> None:
        normalized = _normalize_base_url(base_url)
        self._resolved_base_url = normalized
        self._base_url = normalized
        if self._client is not None and not self._client.is_closed:
            await self._client.aclose()
        self._client = None

    def _begin_request(self, base_url: str) -> None:
        self._last_request_base_url = _normalize_base_url(base_url)
        self._last_421_base_url = ""
        self._last_retry_base_url = ""

    def _retry_base_urls(self, base_url: str) -> list[str]:
        """构建 421 后的重试候选：同 authority fresh connection + 备选域名."""
        normalized = _normalize_base_url(base_url)
        retry_urls = [normalized]
        if not self._configured_base_url.strip():
            retry_urls.extend(
                candidate
                for candidate in self._candidate_base_urls
                if candidate != normalized
            )
        return retry_urls

    async def _request_chat_with_model_retry(
        self,
        *,
        body: dict[str, Any],
        prepared_headers: dict[str, str],
    ) -> httpx.Response:
        response = await self._request_with_421_retry(
            "POST",
            self._get_endpoint(),
            json_body=body,
            headers=prepared_headers,
        )
        if not CopilotModelResolver.is_model_not_supported_response(response):
            return response

        retried_body = dict(body)
        retried_body["model"] = await self._resolve_model_via_resolver(
            self._last_requested_model or body.get("model", ""),
            force_refresh=True,
            refresh_reason="model_not_supported_retry",
        )
        return await self._request_with_421_retry(
            "POST",
            self._get_endpoint(),
            json_body=retried_body,
            headers=prepared_headers,
        )

    @staticmethod
    def _build_misdirected_request(
        response: httpx.Response, body: bytes, base_url: str
    ) -> CopilotMisdirectedRequest:
        return CopilotMisdirectedRequest(
            base_url=_normalize_base_url(base_url),
            status_code=response.status_code,
            request=response.request,
            headers=response.headers,
            body=body,
        )

    @staticmethod
    def _build_http_status_error_from_misdirected(
        error: CopilotMisdirectedRequest,
    ) -> httpx.HTTPStatusError:
        return httpx.HTTPStatusError(
            f"copilot API error: {error.status_code}",
            request=error.request,
            response=httpx.Response(
                error.status_code,
                content=error.body,
                headers=error.headers,
                request=error.request,
            ),
        )

    async def _request_with_421_retry(
        self,
        method: str,
        endpoint: str,
        *,
        headers: dict[str, str],
        json_body: dict[str, Any] | None = None,
    ) -> httpx.Response:
        """同步请求的 421 Misdirected 重试 — 委托给 Copilot421RetryHandler."""
        return await self._421_handler.execute_request_with_retry(
            method,
            endpoint,
            headers=headers,
            json_body=json_body,
        )

    async def _stream_from_client(
        self,
        client: httpx.AsyncClient,
        *,
        base_url: str,
        body: dict[str, Any],
        prepared_headers: dict[str, str],
        request_model: str,
    ) -> AsyncIterator[bytes]:
        async with client.stream(
            "POST",
            self._get_endpoint(),
            json=body,
            headers=prepared_headers,
        ) as response:
            if response.status_code == 421:
                error_body = await response.aread()
                self._last_421_base_url = _normalize_base_url(base_url)
                raise self._build_http_status_error_from_misdirected(
                    self._build_misdirected_request(response, error_body, base_url),
                )
            if response.status_code >= 400:
                self._on_error_status(response.status_code)
                error_body = await response.aread()
                logger.warning(
                    "%s stream error: status=%d body=%s",
                    self.get_name(),
                    response.status_code,
                    error_body[:500],
                )
                raise httpx.HTTPStatusError(
                    f"{self.get_name()} API error: {response.status_code}",
                    request=response.request,
                    response=httpx.Response(
                        response.status_code,
                        content=error_body,
                        headers=response.headers,
                        request=response.request,
                    ),
                )

            async def _upstream() -> AsyncIterator[bytes]:
                async for chunk in response.aiter_bytes():
                    yield chunk

            async for chunk in normalize_anthropic_compatible_stream(
                _upstream(),
                model=body.get("model", request_model),
            ):
                yield chunk

    async def _prepare_request(
        self,
        request_body: dict[str, Any],
        headers: dict[str, str],
        *,
        force_model_refresh: bool = False,
        model_refresh_reason: str = "request_prepare",
    ) -> tuple[dict[str, Any], dict[str, str]]:
        """透传请求体，过滤 hop-by-hop 头并注入 Copilot token."""
        filtered = {
            k: v for k, v in headers.items() if k.lower() not in PROXY_SKIP_HEADERS
        }
        prepared = self._build_copilot_headers()
        for key, value in filtered.items():
            if key.lower() not in {item.lower() for item in prepared}:
                prepared[key] = value
        token = await self._token_manager.get_token()
        prepared["authorization"] = f"Bearer {token}"
        prepared["x-initiator"] = self._resolve_initiator(request_body)
        self._last_request_adaptations = self._collect_request_adaptations(request_body)
        translated_body = convert_openai_request(request_body)
        requested_model = str(request_body.get("model", ""))
        translated_body["model"] = await self._resolve_model_via_resolver(
            requested_model,
            force_refresh=force_model_refresh,
            refresh_reason=model_refresh_reason,
        )
        return translated_body, prepared

    async def _resolve_request_model(
        self,
        requested_model: str,
        *,
        force_refresh: bool,
        refresh_reason: str,
    ) -> str:
        """向后兼容接口：委托 CopilotModelResolver 解析模型名."""
        return await self._resolve_model_via_resolver(
            requested_model,
            force_refresh=force_refresh,
            refresh_reason=refresh_reason,
        )

    async def _resolve_model_via_resolver(
        self,
        requested_model: str,
        *,
        force_refresh: bool,
        refresh_reason: str,
    ) -> str:
        """委托 CopilotModelResolver 解析模型名，并回写诊断到 Mixin 字段."""
        diagnostics: dict[str, str] = {}
        resolved = await self._model_resolver.resolve(
            requested_model,
            force_refresh=force_refresh,
            request_fn=self._request_with_421_retry,
            headers_fn=self._build_copilot_headers,
            refresh_reason=refresh_reason,
            diagnostics=diagnostics,
        )
        # 回写诊断到 TokenBackendMixin 提供的字段
        if "requested_model" in diagnostics:
            self._last_requested_model = diagnostics["requested_model"]
        if "resolved_model" in diagnostics:
            self._last_resolved_model = diagnostics["resolved_model"]
        if "resolution_reason" in diagnostics:
            self._last_model_resolution_reason = diagnostics["resolution_reason"]
        return resolved

    # _on_error_status / check_health 由 TokenBackendMixin 提供

    def get_diagnostics(self) -> dict[str, Any]:
        diagnostics: dict[str, Any] = {
            "account_type": self._account_type,
            "base_url": self._resolved_base_url,
            "configured_base_url": self._configured_base_url,
            "resolved_base_url": self._resolved_base_url,
            "candidate_base_urls": self._candidate_base_urls,
            "available_models_cache": self._model_resolver.catalog.available_models,
        }
        diagnostics.update(BaseVendor.get_diagnostics(self))
        # TokenBackendMixin 提供标准诊断（token_manager / request_adaptations /
        # requested_model / resolved_model / model_resolution_reason）
        diagnostics.update(self._get_token_diagnostics())
        # Copilot 特有诊断字段
        exchange = self._token_manager.get_exchange_diagnostics()
        if exchange:
            diagnostics["exchange"] = exchange
        if self._last_request_base_url:
            diagnostics["last_request_base_url"] = self._last_request_base_url
        if self._last_421_base_url:
            diagnostics["last_421_base_url"] = self._last_421_base_url
        if self._last_retry_base_url:
            diagnostics["last_retry_base_url"] = self._last_retry_base_url
        if self._model_resolver.last_normalized_model:
            diagnostics["normalized_model"] = self._model_resolver.last_normalized_model
        if self._model_resolver.last_model_refresh_reason:
            diagnostics["last_model_refresh_reason"] = (
                self._model_resolver.last_model_refresh_reason
            )
        cache_age = self._model_resolver.catalog.age_seconds()
        if cache_age is not None:
            diagnostics["available_models_cache_age_seconds"] = cache_age
        return diagnostics

    async def send_message(
        self,
        request_body: dict[str, Any],
        headers: dict[str, str],
    ) -> VendorResponse:
        body, prepared_headers = await self._prepare_request(request_body, headers)
        response = await self._request_chat_with_model_retry(
            body=body,
            prepared_headers=prepared_headers,
        )

        raw_content = response.content
        resp_body = _decode_json_body(response)

        if response.status_code >= 400:
            if CopilotModelResolver.is_model_not_supported_response(response):
                response = CopilotModelResolver.build_model_not_supported_response(
                    response,
                    requested_model=self._last_requested_model,
                    normalized_model=self._model_resolver.last_normalized_model,
                    resolved_model=self._last_resolved_model,
                    available_models=list(
                        self._model_resolver.catalog.available_models
                    ),
                )
                raw_content = response.content
                resp_body = _decode_json_body(response)
            self._on_error_status(response.status_code)
            return VendorResponse(
                status_code=response.status_code,
                raw_body=raw_content,
                error_type=resp_body.get("error", {}).get("type")
                if isinstance(resp_body, dict)
                and isinstance(resp_body.get("error"), dict)
                else None,
                error_message=_extract_error_message(response, resp_body),
                response_headers=dict(response.headers),
            )

        if not isinstance(resp_body, dict):
            return VendorResponse(
                status_code=502,
                raw_body=raw_content,
                error_type="api_error",
                error_message="Copilot non-stream response is not valid JSON",
                response_headers=dict(response.headers),
            )

        anthropic_resp = convert_openai_response(resp_body)
        usage = anthropic_resp.get("usage", {})
        return VendorResponse(
            status_code=response.status_code,
            raw_body=httpx.Response(
                response.status_code,
                json=anthropic_resp,
            ).content,
            usage=UsageInfo(
                input_tokens=usage.get("input_tokens", 0),
                output_tokens=usage.get("output_tokens", 0),
                cache_read_tokens=usage.get("cache_read_input_tokens", 0),
                cache_creation_tokens=usage.get("cache_creation_input_tokens", 0),
                request_id=anthropic_resp.get("id", ""),
            ),
            model_served=anthropic_resp.get("model"),
            response_headers=dict(response.headers),
        )

    async def send_message_stream(
        self,
        request_body: dict[str, Any],
        headers: dict[str, str],
    ) -> AsyncIterator[bytes]:
        body, prepared_headers = await self._prepare_request(request_body, headers)
        request_model = request_body.get("model", "unknown")

        # 首次尝试（含 421 重试）
        try:
            async for chunk in self._stream_with_421_retry(
                body, prepared_headers, request_model
            ):
                yield chunk
            return
        except httpx.HTTPStatusError as exc:
            if not CopilotModelResolver.is_model_not_supported_response(exc.response):
                raise

        # 模型不支持时强制刷新模型列表后重试
        async for chunk in self._retry_stream_with_fresh_model(
            request_body, headers, request_model
        ):
            yield chunk

    async def _stream_with_421_retry(
        self,
        stream_body: dict[str, Any],
        prepared_headers: dict[str, str],
        request_model: str,
    ) -> AsyncIterator[bytes]:
        """带 421 Misdirected 重试的流式请求."""
        current_base_url = self._resolved_base_url
        self._begin_request(current_base_url)
        last_exc: httpx.HTTPStatusError | None = None

        try:
            async for chunk in self._stream_from_client(
                self._get_client(),
                base_url=current_base_url,
                body=stream_body,
                prepared_headers=prepared_headers,
                request_model=stream_body.get("model", request_model),
            ):
                yield chunk
            return
        except httpx.HTTPStatusError as exc:
            if exc.response is None or exc.response.status_code != 421:
                raise
            last_exc = exc

        for retry_base_url in self._retry_base_urls(current_base_url):
            self._last_retry_base_url = retry_base_url
            async with self._create_fresh_client(retry_base_url) as retry_client:
                try:
                    async for chunk in self._stream_from_client(
                        retry_client,
                        base_url=retry_base_url,
                        body=stream_body,
                        prepared_headers=prepared_headers,
                        request_model=stream_body.get("model", request_model),
                    ):
                        yield chunk
                    await self._activate_base_url(retry_base_url)
                    return
                except httpx.HTTPStatusError as retry_exc:
                    last_exc = retry_exc
                    if (
                        retry_exc.response is None
                        or retry_exc.response.status_code != 421
                    ):
                        raise

        if last_exc:
            raise last_exc

    async def _retry_stream_with_fresh_model(
        self,
        request_body: dict[str, Any],
        headers: dict[str, str],
        request_model: str,
    ) -> AsyncIterator[bytes]:
        """模型不支持时强制刷新模型列表后重试流式请求."""
        retried_body, retried_headers = await self._prepare_request(
            request_body,
            headers,
            force_model_refresh=True,
            model_refresh_reason="model_not_supported_retry",
        )
        try:
            async for chunk in self._stream_with_421_retry(
                retried_body, retried_headers, request_model
            ):
                yield chunk
            return
        except httpx.HTTPStatusError as exc:
            if (
                CopilotModelResolver.is_model_not_supported_response(exc.response)
                and exc.response is not None
            ):
                raise httpx.HTTPStatusError(
                    "copilot API error: 400",
                    request=exc.request,
                    response=CopilotModelResolver.build_model_not_supported_response(
                        exc.response,
                        requested_model=self._last_requested_model,
                        normalized_model=self._model_resolver.last_normalized_model,
                        resolved_model=self._last_resolved_model,
                        available_models=list(
                            self._model_resolver.catalog.available_models
                        ),
                    ),
                ) from exc
            raise

    async def probe_models(self) -> dict[str, Any]:
        """探测当前 Copilot 会话可见模型列表."""
        available_models = await self._model_resolver.fetch_available(
            request_fn=self._request_with_421_retry,
            headers_fn=self._build_copilot_headers,
            refresh_reason="probe_models",
        )
        probe: dict[str, Any] = {
            "probe_status": "ok" if available_models else "error",
            "status_code": 200 if available_models else 502,
            "account_type": self._account_type,
            "base_url": self._resolved_base_url,
            "resolved_base_url": self._resolved_base_url,
            "candidate_base_urls": self._candidate_base_urls,
        }
        if not available_models:
            probe["failure_reason"] = "Copilot models probe returned empty directory"
            return probe
        probe["available_models"] = available_models
        probe["has_claude_opus_4_6"] = any(
            "opus" in model and "4.6" in model for model in available_models
        )
        return probe

    async def close(self) -> None:
        await self._token_manager.close()
        await super().close()


# 向后兼容别名
CopilotBackend = CopilotVendor
