"""GitHub Copilot 后端 — 内置 token 交换与 Anthropic 兼容转发."""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any, AsyncIterator
from uuid import uuid4

import httpx

from ..config.schema import CopilotConfig, FailoverConfig
from ..convert.anthropic_to_openai import convert_request as convert_openai_request
from ..convert.openai_to_anthropic import convert_response as convert_openai_response
from ..streaming.anthropic_compat import normalize_anthropic_compatible_stream
from .base import (
    PROXY_SKIP_HEADERS,
    BackendCapabilities,
    BackendResponse,
    BaseBackend,
    CapabilityLossReason,
    RequestCapabilities,
    UsageInfo,
    _decode_json_body,
    _extract_error_message,
)
from .token_manager import BaseTokenManager, TokenAcquireError, TokenErrorKind

logger = logging.getLogger(__name__)

_COPILOT_VERSION = "0.26.7"
_EDITOR_VERSION = "vscode/1.98.0"
_EDITOR_PLUGIN_VERSION = f"copilot-chat/{_COPILOT_VERSION}"
_USER_AGENT = f"GitHubCopilotChat/{_COPILOT_VERSION}"
_GITHUB_API_VERSION = "2025-04-01"


def _normalize_base_url(url: str) -> str:
    return url.rstrip("/")


def build_copilot_candidate_base_urls(account_type: str, configured_base_url: str) -> list[str]:
    """构建 Copilot 候选基础地址列表."""
    if configured_base_url.strip():
        return [_normalize_base_url(configured_base_url.strip())]

    normalized = (account_type or "individual").strip().lower() or "individual"
    candidates = [f"https://api.{normalized}.githubcopilot.com"]
    candidates.append("https://api.githubcopilot.com")

    unique_candidates: list[str] = []
    for candidate in candidates:
        normalized_candidate = _normalize_base_url(candidate)
        if normalized_candidate not in unique_candidates:
            unique_candidates.append(normalized_candidate)
    return unique_candidates


def resolve_copilot_base_url(account_type: str, configured_base_url: str) -> str:
    """解析 Copilot API 基础地址.

    保留用户显式覆盖；仅当值为空时按账号类型回退到官方推荐域名。
    """
    return build_copilot_candidate_base_urls(account_type, configured_base_url)[0]


@dataclass
class CopilotMisdirectedRequest:
    base_url: str
    status_code: int
    request: httpx.Request
    headers: httpx.Headers
    body: bytes


@dataclass
class CopilotExchangeDiagnostics:
    """最近一次 Copilot token 交换的运行时诊断."""

    raw_shape: str = ""
    token_field: str = ""
    expires_in_seconds: int = 0
    expires_at_unix: int = 0
    capabilities: dict[str, Any] = field(default_factory=dict)
    updated_at_unix: int = 0

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {}
        if self.raw_shape:
            data["raw_shape"] = self.raw_shape
        if self.token_field:
            data["token_field"] = self.token_field
        if self.expires_in_seconds:
            data["expires_in_seconds"] = self.expires_in_seconds
        if self.expires_at_unix:
            data["expires_at_unix"] = self.expires_at_unix
            data["ttl_seconds"] = max(self.expires_at_unix - int(time.time()), 0)
        if self.capabilities:
            data["capabilities"] = self.capabilities
        if self.updated_at_unix:
            data["updated_at_unix"] = self.updated_at_unix
        return data


class CopilotTokenManager(BaseTokenManager):
    """GitHub Copilot token 交换管理.

    流程: GitHub token → GET copilot_internal/v2/token → Copilot access_token (~30 分钟有效期)
    """

    def __init__(self, github_token: str, token_url: str) -> None:
        super().__init__()
        self._github_token = github_token
        self._token_url = token_url
        self._last_exchange = CopilotExchangeDiagnostics()

    @staticmethod
    def _format_body_excerpt(data: Any) -> str:
        if isinstance(data, dict):
            for key in ("error_description", "error", "message"):
                value = data.get(key)
                if value:
                    return str(value)[:200]
        return str(data)[:200]

    @classmethod
    def _build_missing_token_error(
        cls, data: Any, status_code: int,
    ) -> TokenAcquireError:
        detail = cls._format_body_excerpt(data)
        lowered = detail.lower()
        capability_keys = {
            "chat_enabled", "agent_mode_auto_approval", "chat_jetbrains_enabled",
            "annotations_enabled", "code_quote_enabled",
        }
        if isinstance(data, dict) and capability_keys.intersection(data.keys()):
            return TokenAcquireError.with_kind(
                "Copilot 当前登录权限不足，需升级到可交换 chat token 的 GitHub 会话",
                kind=TokenErrorKind.PERMISSION_UPGRADE_REQUIRED,
                needs_reauth=True,
            )
        needs_reauth = status_code == 401 or any(
            pattern in lowered for pattern in ("bad credentials", "invalid token", "unauthorized")
        )
        kind = TokenErrorKind.INVALID_CREDENTIALS if needs_reauth else TokenErrorKind.TEMPORARY
        return TokenAcquireError.with_kind(
            f"Copilot token 交换返回非预期响应: status={status_code}, detail={detail}",
            kind=kind,
            needs_reauth=needs_reauth,
        )

    @staticmethod
    def _extract_capabilities(data: Any) -> dict[str, Any]:
        if not isinstance(data, dict):
            return {}
        capability_keys = (
            "chat_enabled",
            "chat_jetbrains_enabled",
            "agent_mode_auto_approval",
            "code_quote_enabled",
            "annotations_enabled",
        )
        return {key: data[key] for key in capability_keys if key in data}

    def _record_exchange(self, data: dict[str, Any], token_field: str, expires_in: int) -> None:
        expires_at = int(time.time()) + max(expires_in, 0)
        self._last_exchange = CopilotExchangeDiagnostics(
            raw_shape="token_refresh_in" if "token" in data else "access_token_expires_in",
            token_field=token_field,
            expires_in_seconds=expires_in,
            expires_at_unix=expires_at,
            capabilities=self._extract_capabilities(data),
            updated_at_unix=int(time.time()),
        )

    def get_exchange_diagnostics(self) -> dict[str, Any]:
        return self._last_exchange.to_dict()

    async def _acquire(self) -> tuple[str, float]:
        """通过 GitHub token 交换 Copilot token."""
        client = self._get_client()
        try:
            response = await client.get(
                self._token_url,
                headers={
                    "authorization": f"token {self._github_token}",
                    "accept": "application/json",
                    "editor-version": _EDITOR_VERSION,
                    "editor-plugin-version": _EDITOR_PLUGIN_VERSION,
                    "user-agent": _USER_AGENT,
                    "x-github-api-version": _GITHUB_API_VERSION,
                    "x-vscode-user-agent-library-version": "electron-fetch",
                },
            )
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 401:
                raise TokenAcquireError.with_kind(
                    "GitHub token 无效或已过期",
                    kind=TokenErrorKind.INVALID_CREDENTIALS,
                    needs_reauth=True,
                ) from exc
            raise TokenAcquireError.with_kind(
                f"Copilot token 交换失败: {exc}",
                kind=TokenErrorKind.TEMPORARY,
            ) from exc
        except (httpx.TimeoutException, httpx.ConnectError) as exc:
            raise TokenAcquireError.with_kind(
                f"Copilot token 交换网络异常: {exc}",
                kind=TokenErrorKind.TEMPORARY,
            ) from exc

        try:
            data = response.json()
        except ValueError as exc:
            raise TokenAcquireError(
                f"Copilot token 交换返回非 JSON 响应: status={response.status_code}",
            ) from exc

        if response.status_code >= 400:
            if response.status_code == 401:
                raise TokenAcquireError.with_kind(
                    "GitHub token 无效或已过期",
                    kind=TokenErrorKind.INVALID_CREDENTIALS,
                    needs_reauth=True,
                )
            raise self._build_missing_token_error(data, response.status_code)

        token_field = "token" if data.get("token") else "access_token"
        access_token = data.get("token") or data.get("access_token")
        if not access_token:
            raise self._build_missing_token_error(data, response.status_code)

        expires_in = data.get("refresh_in") or data.get("expires_in")
        if expires_in is None and data.get("expires_at"):
            expires_in = max(int(data["expires_at"]) - int(time.time()), 0)
        expires_in = int(expires_in or 1800)
        self._record_exchange(data, token_field, expires_in)
        logger.info("Copilot token exchanged, expires_in=%ds", expires_in)
        return str(access_token), float(expires_in)

    def update_github_token(self, new_token: str) -> None:
        """运行时热更新 GitHub token（重认证后调用）."""
        self._github_token = new_token
        self.invalidate()


class CopilotBackend(BaseBackend):
    """GitHub Copilot API 后端.

    通过内置 token 交换访问 GitHub Copilot 的 Anthropic 兼容端点.
    透传请求体（无模型映射），Claude 模型名原生支持.
    """

    def __init__(self, config: CopilotConfig, failover_config: FailoverConfig) -> None:
        self._account_type = (config.account_type or "individual").strip().lower()
        self._configured_base_url = config.base_url
        self._candidate_base_urls = build_copilot_candidate_base_urls(self._account_type, config.base_url)
        self._resolved_base_url = resolve_copilot_base_url(self._account_type, config.base_url)
        self._last_request_adaptations: list[str] = []
        self._last_request_base_url = ""
        self._last_421_base_url = ""
        self._last_retry_base_url = ""
        super().__init__(self._resolved_base_url, config.timeout_ms, failover_config)
        self._token_manager = CopilotTokenManager(config.github_token, config.token_url)

    def get_name(self) -> str:
        return "copilot"

    def get_capabilities(self) -> BackendCapabilities:
        return BackendCapabilities(
            supports_tools=True,
            supports_thinking=False,
            supports_images=True,
            emits_vendor_tool_events=False,
            supports_metadata=True,
        )

    def supports_request(
        self, request_caps: RequestCapabilities,
    ) -> tuple[bool, list[CapabilityLossReason]]:
        """Copilot 可通过适配层吸收 thinking 语义，不在路由阶段直接拒绝."""
        supported, reasons = super().supports_request(request_caps)
        if not supported:
            reasons = [reason for reason in reasons if reason is not CapabilityLossReason.THINKING]
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

        if request_body.get("thinking") or request_body.get("extended_thinking"):
            adaptations.append("thinking_downgraded_to_text")

        for message in request_body.get("messages", []):
            content = message.get("content")
            if not isinstance(content, list):
                continue
            if any(
                isinstance(block, dict) and block.get("type") == "thinking"
                for block in content
            ):
                adaptations.append("thinking_block_merged_into_text")
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
                candidate for candidate in self._candidate_base_urls
                if candidate != normalized
            )
        return retry_urls

    @staticmethod
    def _build_misdirected_request(response: httpx.Response, body: bytes, base_url: str) -> CopilotMisdirectedRequest:
        return CopilotMisdirectedRequest(
            base_url=_normalize_base_url(base_url),
            status_code=response.status_code,
            request=response.request,
            headers=response.headers,
            body=body,
        )

    @staticmethod
    def _build_http_status_error_from_misdirected(error: CopilotMisdirectedRequest) -> httpx.HTTPStatusError:
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
        current_base_url = self._resolved_base_url
        self._begin_request(current_base_url)

        response = await self._get_client().request(
            method,
            endpoint,
            json=json_body,
            headers=headers,
        )
        if response.status_code != 421:
            return response

        self._last_421_base_url = current_base_url
        last_response = response

        for retry_base_url in self._retry_base_urls(current_base_url):
            self._last_retry_base_url = retry_base_url
            async with self._create_fresh_client(retry_base_url) as retry_client:
                retry_response = await retry_client.request(
                    method,
                    endpoint,
                    json=json_body,
                    headers=headers,
                )
            last_response = retry_response
            if retry_response.status_code != 421:
                await self._activate_base_url(retry_base_url)
                return retry_response
            self._last_421_base_url = retry_base_url

        return last_response

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
                    self.get_name(), response.status_code, error_body[:500],
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
    ) -> tuple[dict[str, Any], dict[str, str]]:
        """透传请求体，过滤 hop-by-hop 头并注入 Copilot token."""
        filtered = {k: v for k, v in headers.items() if k.lower() not in PROXY_SKIP_HEADERS}
        prepared = self._build_copilot_headers()
        for key, value in filtered.items():
            if key.lower() not in {item.lower() for item in prepared}:
                prepared[key] = value
        token = await self._token_manager.get_token()
        prepared["authorization"] = f"Bearer {token}"
        prepared["x-initiator"] = self._resolve_initiator(request_body)
        self._last_request_adaptations = self._collect_request_adaptations(request_body)
        return convert_openai_request(request_body), prepared

    def _on_error_status(self, status_code: int) -> None:
        """401/403 时标记 token 失效以触发被动刷新."""
        if status_code in (401, 403):
            self._token_manager.invalidate()

    async def check_health(self) -> bool:
        """检查 Copilot token 交换是否有效（免费操作）."""
        try:
            token = await self._token_manager.get_token()
            return bool(token)
        except Exception:
            return False

    def get_diagnostics(self) -> dict[str, Any]:
        diagnostics: dict[str, Any] = {
            "account_type": self._account_type,
            "base_url": self._resolved_base_url,
            "configured_base_url": self._configured_base_url,
            "resolved_base_url": self._resolved_base_url,
            "candidate_base_urls": self._candidate_base_urls,
        }
        token_manager = self._token_manager.get_diagnostics()
        if token_manager:
            diagnostics["token_manager"] = token_manager
        exchange = self._token_manager.get_exchange_diagnostics()
        if exchange:
            diagnostics["exchange"] = exchange
        if self._last_request_adaptations:
            diagnostics["request_adaptations"] = self._last_request_adaptations
        if self._last_request_base_url:
            diagnostics["last_request_base_url"] = self._last_request_base_url
        if self._last_421_base_url:
            diagnostics["last_421_base_url"] = self._last_421_base_url
        if self._last_retry_base_url:
            diagnostics["last_retry_base_url"] = self._last_retry_base_url
        return diagnostics

    async def send_message(
        self,
        request_body: dict[str, Any],
        headers: dict[str, str],
    ) -> BackendResponse:
        body, prepared_headers = await self._prepare_request(request_body, headers)
        response = await self._request_with_421_retry(
            "POST",
            self._get_endpoint(),
            json_body=body,
            headers=prepared_headers,
        )

        raw_content = response.content
        resp_body = _decode_json_body(response)

        if response.status_code >= 400:
            self._on_error_status(response.status_code)
            return BackendResponse(
                status_code=response.status_code,
                raw_body=raw_content,
                error_type=resp_body.get("error", {}).get("type") if isinstance(resp_body, dict) and isinstance(resp_body.get("error"), dict) else None,
                error_message=_extract_error_message(response, resp_body),
                response_headers=dict(response.headers),
            )

        if not isinstance(resp_body, dict):
            return BackendResponse(
                status_code=502,
                raw_body=raw_content,
                error_type="api_error",
                error_message="Copilot non-stream response is not valid JSON",
                response_headers=dict(response.headers),
            )

        anthropic_resp = convert_openai_response(resp_body)
        usage = anthropic_resp.get("usage", {})
        return BackendResponse(
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
        current_base_url = self._resolved_base_url
        self._begin_request(current_base_url)
        request_model = request_body.get("model", "unknown")
        last_exc: httpx.HTTPStatusError | None = None

        try:
            async for chunk in self._stream_from_client(
                self._get_client(),
                base_url=current_base_url,
                body=body,
                prepared_headers=prepared_headers,
                request_model=request_model,
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
                        body=body,
                        prepared_headers=prepared_headers,
                        request_model=request_model,
                    ):
                        yield chunk
                    await self._activate_base_url(retry_base_url)
                    return
                except httpx.HTTPStatusError as retry_exc:
                    last_exc = retry_exc
                    if retry_exc.response is None or retry_exc.response.status_code != 421:
                        raise

        if last_exc:
            raise last_exc

    async def probe_models(self) -> dict[str, Any]:
        """探测当前 Copilot 会话可见模型列表."""
        token = await self._token_manager.get_token()
        response = await self._request_with_421_retry(
            "GET",
            "/models",
            headers={
                **self._build_copilot_headers(),
                "authorization": f"Bearer {token}",
            },
        )
        probe: dict[str, Any] = {
            "probe_status": "ok" if response.status_code < 400 else "error",
            "status_code": response.status_code,
            "account_type": self._account_type,
            "base_url": self._resolved_base_url,
            "resolved_base_url": self._resolved_base_url,
            "candidate_base_urls": self._candidate_base_urls,
        }
        data = _decode_json_body(response) or {}
        if response.status_code >= 400:
            error = data.get("error", {}) if isinstance(data, dict) else {}
            probe["failure_reason"] = (
                error.get("message")
                or CopilotTokenManager._format_body_excerpt(data)
            )
            return probe

        models = data.get("data", []) if isinstance(data, dict) else []
        available_models = [
            item.get("id")
            for item in models
            if isinstance(item, dict) and item.get("id")
        ]
        probe["available_models"] = available_models
        probe["has_claude_opus_4_6"] = any("opus" in model and "4.6" in model for model in available_models)
        return probe

    async def close(self) -> None:
        await self._token_manager.close()
        await super().close()
