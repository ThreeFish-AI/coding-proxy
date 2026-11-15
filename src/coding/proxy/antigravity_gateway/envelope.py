"""v1internal 信封旁路解析 — 仅用于记账，绝不改写请求/响应体.

Antigravity 的 Cloud Code ``v1internal`` 协议在标准 Gemini
``GenerateContentRequest`` / ``GenerateContentResponse`` 外包一层信封：

- 请求体：``{"project": "aicode-consumers", "requestId": ..., "model": ...,
  "userAgent": "antigravity", "requestType": ..., "request": {...}}``；
  会话身份在内层 ``request.systemInstruction`` 的
  ``Conversation ID: <uuid>`` 标签（信封 ``request.sessionId`` 字段恒为
  FNV-1a 空串常量，不可用作会话标识）；
- 响应（流式 SSE 帧与非流式 JSON 同构）：
  ``{"response": <GenerateContentResponse>, "traceId": ..., "metadata": {}}``；
  真实流存在多行 ``data:``（pretty-printed JSON）与 CRLF 行尾，解析须按
  SSE 事件语义聚合。

本模块只做旁路（tee）解析供 ``usage_log`` 落账与 session 维度提取；
解析失败一律静默降级（空结果），不影响透传主链路。
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from typing import Any

from ..passthrough_kernel import ExtractionResult
from ..routing.usage_parser import (
    append_usage_evidence,
    build_usage_evidence_records,
)

logger = logging.getLogger(__name__)

# systemInstruction 内的稳定会话标签（与 bili / Antigravity 逆向结论对齐）
_CONVERSATION_ID_RE = re.compile(
    r"Conversation ID:\s*([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12})"
)

# requestId 已知两种形态（服务端均接受）：
#   抓包原生:  agent/{uuid}
#   仓内 vendor 构造: agent/antigravity/{session 16hex}/{seq}
_REQUEST_ID_PREFIX = "agent/"


@dataclass(frozen=True)
class RequestEnvelope:
    """v1internal 请求信封的记账视图."""

    model: str = ""
    session_key: str = ""
    request_id: str = ""
    matched: bool = False


def _extract_session_key(body: dict[str, Any], request_id: str) -> str:
    """会话身份三级回退：Conversation ID 标签 → agent/antigravity/{sid}/{seq} → agent/{uuid}."""
    # ① systemInstruction 里的 Conversation ID 标签（跨端点稳定）
    request = body.get("request")
    if isinstance(request, dict):
        si = request.get("systemInstruction")
        if isinstance(si, dict):
            parts = si.get("parts")
            if isinstance(parts, list):
                for part in parts:
                    if isinstance(part, dict) and isinstance(part.get("text"), str):
                        m = _CONVERSATION_ID_RE.search(part["text"])
                        if m:
                            return m.group(1)
    # ②③ requestId 派生
    if request_id.startswith(_REQUEST_ID_PREFIX):
        remainder = request_id[len(_REQUEST_ID_PREFIX) :]
        segments = remainder.split("/")
        if segments and segments[0]:
            if segments[0] == "antigravity" and len(segments) >= 2 and segments[1]:
                return segments[1]  # agent/antigravity/{sid}/{seq}
            return segments[0]  # agent/{uuid}
    return ""


def parse_request_envelope(body_bytes: bytes | None) -> RequestEnvelope:
    """从请求体提取 ``model`` / ``session_key`` / ``requestId``.

    body 非 JSON / 非信封形态（如 countTokens 的精简体）→ ``matched=False``，
    各字段为空，不影响透传与记账主链路。
    """
    if not body_bytes:
        return RequestEnvelope()
    try:
        body = json.loads(body_bytes)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return RequestEnvelope()
    if not isinstance(body, dict):
        return RequestEnvelope()

    request_id = body.get("requestId")
    request_id = request_id if isinstance(request_id, str) else ""
    session_key = _extract_session_key(body, request_id)

    model = body.get("model")
    matched = isinstance(model, str) and bool(model) and "request" in body
    return RequestEnvelope(
        model=model if isinstance(model, str) else "",
        session_key=session_key,
        request_id=request_id,
        matched=matched,
    )


def _set_if_nonzero(d: dict[str, Any], key: str, value: Any) -> None:
    """与 routing.usage_parser._set_if_nonzero 同语义：零值/None 不覆盖."""
    if isinstance(value, (int, float)) and value:
        d[key] = int(value)


def _unwrap_response(body: dict[str, Any]) -> dict[str, Any]:
    """兼容 ``{response: {...}}`` 信封与裸 Gemini 响应两种形态."""
    resp = body.get("response")
    return resp if isinstance(resp, dict) else body


def extract_v1internal_usage(body: dict[str, Any]) -> ExtractionResult:
    """非流式响应 usage 抽取 — 兼容信封 / 裸两种形态，含 countTokens."""
    payload = _unwrap_response(body)
    request_id = _as_str(payload.get("responseId")) or _as_str(body.get("traceId"))
    model_served = _as_str(payload.get("modelVersion"))

    um = payload.get("usageMetadata")
    if not isinstance(um, dict):
        # countTokens 形态：{"totalTokens": N}（与 native gemini extractor 口径一致）
        total = _as_int(payload.get("totalTokens"))
        if total:
            return ExtractionResult(
                input_tokens=total,
                request_id=request_id,
                model_served=model_served,
                raw_usage={"totalTokens": total},
                evidence_kind="v1internal_count_tokens",
            )
        return ExtractionResult(request_id=request_id, model_served=model_served)

    result = ExtractionResult(
        input_tokens=_as_int(um.get("promptTokenCount")),
        output_tokens=_as_int(um.get("candidatesTokenCount")),
        cache_read_tokens=_as_int(um.get("cachedContentTokenCount")),
        request_id=request_id,
        model_served=model_served,
        raw_usage=dict(um),
        evidence_kind="v1internal_usage_metadata",
    )
    result.source_field_map = {
        "input_tokens": "promptTokenCount",
        "output_tokens": "candidatesTokenCount",
        "cache_read_tokens": "cachedContentTokenCount",
    }
    for k in ("thoughtsTokenCount", "toolUsePromptTokenCount", "totalTokenCount"):
        v = _as_int(um.get(k))
        if v:
            result.extra_usage[k] = v
    return result


_EXTRA_KEY_MAP: dict[str, str] = {
    "thoughtsTokenCount": "thoughts_tokens",
    "toolUsePromptTokenCount": "tool_use_prompt_tokens",
}


# 事件分隔 = 空行（允许行间空白符）；对字节流切分，完整事件才解码（多字节安全）
_EVENT_SEPARATOR_RE = re.compile(rb"\r?\n[ \t]*\r?\n")


def _event_payload(event_text: str) -> str:
    """单事件内的 data payload — 多条 ``data:`` 行按 SSE 规范以 ``\\n`` 拼接."""
    data_lines: list[str] = []
    for raw_line in event_text.split("\n"):
        line = raw_line.rstrip("\r")
        if line.startswith("data:"):
            data_lines.append(line[5:].lstrip(" "))
        # 其他行（event:/comment/字段）忽略
    return "\n".join(data_lines)


class V1InternalStreamAccumulator:
    """v1internal SSE 流式 usage 累加器.

    帧形态 ``data: {"response": {"usageMetadata": ..., "modelVersion": ...,
    "responseId": ...}, "traceId": ...}``；兼容裸 Gemini 帧（顶层
    ``usageMetadata``）。按 SSE 事件语义解析：跨 chunk 缓冲不完整事件
    （网络读取边界可能切断帧）、聚合多行 ``data:``（pretty JSON）、
    兼容 CRLF 行尾；累加键与 ``StreamingUsageAccumulator`` 一致，终结时
    经 ``build_usage_evidence_records`` 生成 evidence。
    """

    def __init__(self) -> None:
        self._usage: dict[str, Any] = {}
        self._carry = b""  # 跨 chunk 的不完整事件字节（避免多字节字符被截断解码）

    def feed(self, chunk: bytes) -> None:
        if not chunk:
            return
        try:
            buf = self._carry + chunk
            # 仅处理以空行收尾的完整事件（字节级切分，多字节字符不被截断解码）；
            # 尾部残缺留待下一 chunk
            parts = _EVENT_SEPARATOR_RE.split(buf)
            self._carry = parts.pop()
            for event_bytes in parts:
                self._feed_event_text(event_bytes.decode("utf-8", errors="ignore"))
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning("v1internal stream usage parse failed: %s", exc)

    def _feed_event_text(self, event_text: str) -> None:
        payload = _event_payload(event_text).strip()
        if not payload or payload == "[DONE]":
            return
        try:
            data = json.loads(payload)
        except json.JSONDecodeError:
            return
        if isinstance(data, dict):
            self._feed_event(data)

    def _feed_event(self, data: dict[str, Any]) -> None:
        payload_frame = _unwrap_response(data)
        u = self._usage
        # 元数据（responseId / modelVersion）在任意帧提取，不依赖 usage 帧
        if not u.get("request_id"):
            rid = _as_str(payload_frame.get("responseId")) or _as_str(
                data.get("traceId")
            )
            if rid:
                u["request_id"] = rid
        model = _as_str(payload_frame.get("modelVersion"))
        if model:
            u["model_served"] = model
        um = payload_frame.get("usageMetadata")
        if not isinstance(um, dict):
            return
        _set_if_nonzero(u, "input_tokens", um.get("promptTokenCount"))
        _set_if_nonzero(u, "output_tokens", um.get("candidatesTokenCount"))
        _set_if_nonzero(u, "cache_read_tokens", um.get("cachedContentTokenCount"))
        extra = u.setdefault("extra_usage", {})
        if isinstance(extra, dict):
            for k, extra_key in _EXTRA_KEY_MAP.items():
                v = _as_int(um.get(k))
                if v:
                    extra[extra_key] = v
        # 与 native 通道同口径：每个 usage 帧追加 evidence 条目（finalize 消费）
        append_usage_evidence(
            u,
            evidence_kind="v1internal_usage_metadata",
            raw_usage=dict(um),
            request_id=_as_str(payload_frame.get("responseId"))
            or _as_str(data.get("traceId")),
            model_served=_as_str(payload_frame.get("modelVersion")),
        )

    def finalize(
        self, *, vendor: str, model_served: str, request_id: str = ""
    ) -> tuple[ExtractionResult, list[dict[str, Any]]]:
        """终结并生成 ExtractionResult + evidence 记录列表（冲刷残余缓冲）."""
        if self._carry.strip():
            try:
                self._feed_event_text(self._carry.decode("utf-8", errors="ignore"))
            except Exception as exc:  # pragma: no cover - defensive
                logger.warning("v1internal carry flush failed: %s", exc)
            self._carry = b""
        u = self._usage
        extra = u.get("extra_usage", {})
        if not isinstance(extra, dict):
            extra = {}
        result = ExtractionResult(
            input_tokens=_as_int(u.get("input_tokens")),
            output_tokens=_as_int(u.get("output_tokens")),
            cache_read_tokens=_as_int(u.get("cache_read_tokens")),
            request_id=_as_str(u.get("request_id")) or request_id,
            model_served=_as_str(u.get("model_served")) or model_served,
            extra_usage=dict(extra),
            evidence_kind="v1internal_stream_usage",
        )
        evidence = build_usage_evidence_records(
            u,
            vendor=vendor,
            model_served=result.model_served or model_served,
            request_id=result.request_id or request_id,
        )
        return result, evidence


def _as_int(v: Any) -> int:
    if isinstance(v, bool):
        return 0
    if isinstance(v, (int, float)) and v > 0:
        return int(v)
    return 0


def _as_str(v: Any) -> str:
    return v if isinstance(v, str) else ""


__all__ = [
    "RequestEnvelope",
    "V1InternalStreamAccumulator",
    "extract_v1internal_usage",
    "parse_request_envelope",
]
