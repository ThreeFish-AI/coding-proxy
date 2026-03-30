"""后端抽象基类 (Strategy 模式)."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, AsyncIterator


@dataclass
class UsageInfo:
    """一次调用的 Token 用量."""

    input_tokens: int = 0
    output_tokens: int = 0
    cache_creation_tokens: int = 0
    cache_read_tokens: int = 0
    request_id: str = ""


@dataclass
class BackendResponse:
    """后端响应结果."""

    status_code: int = 200
    usage: UsageInfo = field(default_factory=UsageInfo)
    is_streaming: bool = False
    raw_body: bytes = b"{}"
    error_type: str | None = None
    error_message: str | None = None


class BaseBackend(ABC):
    """抽象后端接口."""

    @abstractmethod
    def get_name(self) -> str:
        """返回后端名称（用于日志）."""

    @abstractmethod
    async def send_message_stream(
        self,
        request_body: dict[str, Any],
        headers: dict[str, str],
    ) -> AsyncIterator[bytes]:
        """发送消息并返回 SSE 字节流."""

    @abstractmethod
    async def send_message(
        self,
        request_body: dict[str, Any],
        headers: dict[str, str],
    ) -> BackendResponse:
        """发送非流式消息请求."""

    @abstractmethod
    def should_trigger_failover(self, status_code: int, body: dict[str, Any] | None) -> bool:
        """判断响应是否应触发故障转移."""
