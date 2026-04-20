"""Native API FastAPI 路由注册.

三个 catch-all 端点 — ``/api/{openai,gemini,anthropic}/{rest:path}`` — 全部委托给
``NativeProxyHandler.dispatch``。**注册顺序至关重要**：必须在所有具体管理路由
（``/api/status`` / ``/api/copilot/*`` / ``/api/reset`` / ``/api/reauth/*``）**之后**
注册，以避免 FastAPI 路由冲突（虽然 FastAPI 具体路径本身就优先于通配）。
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from fastapi import Request
from starlette.responses import Response

if TYPE_CHECKING:
    from fastapi import FastAPI

    from .handler import NativeProxyHandler

logger = logging.getLogger(__name__)

# FastAPI 层接受的 HTTP method 枚举 — 覆盖 OpenAI/Gemini/Anthropic 所有已知端点
_ALLOWED_METHODS: list[str] = [
    "GET",
    "POST",
    "PUT",
    "PATCH",
    "DELETE",
    "HEAD",
    "OPTIONS",
]


def register_native_api_routes(app: FastAPI, handler: NativeProxyHandler) -> None:
    """在 ``app`` 上注册三个原生 API 透传前缀."""

    @app.api_route(
        "/api/openai/{rest_path:path}",
        methods=_ALLOWED_METHODS,
        include_in_schema=False,
    )
    async def openai_proxy(rest_path: str, request: Request) -> Response:
        return await handler.dispatch("openai", rest_path, request)

    @app.api_route(
        "/api/gemini/{rest_path:path}",
        methods=_ALLOWED_METHODS,
        include_in_schema=False,
    )
    async def gemini_proxy(rest_path: str, request: Request) -> Response:
        return await handler.dispatch("gemini", rest_path, request)

    @app.api_route(
        "/api/anthropic/{rest_path:path}",
        methods=_ALLOWED_METHODS,
        include_in_schema=False,
    )
    async def anthropic_proxy(rest_path: str, request: Request) -> Response:
        return await handler.dispatch("anthropic", rest_path, request)

    logger.info(
        "Native API passthrough routes registered: /api/{openai,gemini,anthropic}/*"
    )


__all__ = ["register_native_api_routes"]
