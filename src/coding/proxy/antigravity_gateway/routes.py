"""Antigravity Gateway FastAPI 路由注册.

单一 catch-all 端点 ``/api/antigravity/{rest:path}`` 委托给
``AntigravityGatewayHandler.dispatch``。与 native_api 的 catch-all 一样，
必须在所有具体管理路由之后注册（``register_all_routes`` 末尾）。
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from fastapi import Request
from starlette.responses import Response

if TYPE_CHECKING:
    from fastapi import FastAPI

    from .handler import AntigravityGatewayHandler

logger = logging.getLogger(__name__)

_ALLOWED_METHODS: list[str] = [
    "GET",
    "POST",
    "PUT",
    "PATCH",
    "DELETE",
    "HEAD",
    "OPTIONS",
]


def register_antigravity_gateway_routes(
    app: FastAPI, handler: AntigravityGatewayHandler
) -> None:
    """在 ``app`` 上注册 Antigravity 客户端透传前缀."""

    @app.api_route(
        "/api/antigravity/{rest_path:path}",
        methods=_ALLOWED_METHODS,
        include_in_schema=False,
    )
    async def antigravity_proxy(rest_path: str, request: Request) -> Response:
        return await handler.dispatch(rest_path, request)

    logger.info("Antigravity gateway routes registered: /api/antigravity/*")


__all__ = ["register_antigravity_gateway_routes"]
