"""路由执行器 — 统一的 tier 迭代门控引擎.

封装 ``route_stream`` / ``route_message`` 共享的 tier 循环、
门控判断与错误处理逻辑，消除两个路由方法间的重复代码。
"""

from __future__ import annotations

import html
import json
import logging
import re
import time
import traceback
from collections.abc import AsyncIterator
from typing import TYPE_CHECKING, Any

import httpx

if TYPE_CHECKING:
    from ..config.session_policy import TitleVendorBinding

from ..vendors.base import (
    NoCompatibleVendorError,
    RequestCapabilities,
    VendorResponse,
)
from ..vendors.token_manager import TokenAcquireError, TokenErrorKind
from .error_classifier import (
    build_request_capabilities,
    extract_error_payload_from_http_status,
    is_semantic_rejection,
    is_structural_validation_error,
)
from .rate_limit import (
    compute_effective_retry_seconds,
    compute_rate_limit_deadline,
    parse_rate_limit_headers,
)
from .session_manager import RouteSessionManager
from .session_policy import SessionPolicy, SessionPolicyResolver
from .tier import VendorTier
from .usage_parser import (
    build_usage_evidence_records,
    has_missing_input_usage_signals,
    parse_usage_from_chunk,
)
from .usage_recorder import UsageRecorder

# 向后兼容别名
BackendResponse = VendorResponse
NoCompatibleBackendError = NoCompatibleVendorError
from ..compat.canonical import (
    CanonicalPartType,
    CompatibilityStatus,
    build_canonical_request,
)
from ..model.compat import CanonicalMessagePart, CanonicalRequest

logger = logging.getLogger(__name__)


def _log_unexpected(scope: str, tier_name: str, exc: BaseException) -> None:
    """记录未预期异常：类型 + 堆栈帧，不含 ``str(exc)``（I2）.

    请求头构建失败等异常的消息体会携带密钥明文（如非法 header value 的
    bytes repr），只记类型与调用栈即可定位，不落异常消息。
    """
    logger.error(
        "Tier %s %s unexpected error: %s\n%s",
        tier_name,
        scope,
        type(exc).__name__,
        "".join(traceback.format_tb(exc.__traceback__)),
    )


_SESSION_TITLE_MAX_LEN = 600
# 回退标题截取长度 — 工具结果等非用户直接输入的摘要上限。
_FALLBACK_TITLE_MAX_LEN = 80
# 注入式载荷标题截取长度 — 家族标签 + 载荷合计上限，保持列表可扫读。
_INJECTED_PAYLOAD_TITLE_MAX_LEN = 160

# Claude Code 注入的"噪声"标签 — 系统级上下文,不应进入 Session 标题。
# 这些标签由 CC harness 在首个 user 消息 content 中拼接,高度同质,
# 直接用作标题会导致跨会话标题无差异化,丧失辨识度。
_NOISE_TAG_PATTERN = re.compile(
    r"<(?P<tag>system-reminder|user-preferences|"
    r"local-command-stdout|local-command-stderr|"
    r"bash-input|bash-stdout|bash-stderr|"
    r"ide_selection|ide_opened_file|stdin|system_instruction|session|"
    r"artifactMetadata|thinking)\b[^>]*>"
    r".*?</(?P=tag)>",
    flags=re.DOTALL | re.IGNORECASE,
)

# <session> 标签需要特殊处理:当用户文本在 <session> 标签内部时,
# 完整块剥离会连同用户文本一起删除。此模式仅去除外壳标签(保留内容),
# 用于首轮完整剥离结果为空时的二次回退提取。
_SESSION_TAG_WRAPPER = re.compile(r"</?session\b[^>]*>", flags=re.IGNORECASE)

# Slash command 子标签:用于识别 /commit、/review 等命令式调用,
# 合成"命令 + 参数"式标题。
_CMD_NAME_PATTERN = re.compile(r"<command-name>(.*?)</command-name>", flags=re.DOTALL)
_CMD_ARGS_PATTERN = re.compile(r"<command-args>(.*?)</command-args>", flags=re.DOTALL)
# 残留 command-* 包裹标签清除(command-message/command-stdout 等次要标签)。
_CMD_WRAPPER_PATTERN = re.compile(
    r"<command-[\w-]+>.*?</command-[\w-]+>", flags=re.DOTALL
)

# <pasted_content> 仅剥外壳标签保留内容 — 粘贴体本身是真实用户输入。
_PASTED_CONTENT_WRAPPER = re.compile(r"</?pasted_content\b[^>]*>", re.IGNORECASE)

# [Image #N] 粘贴图片占位符 — 剥离后保留周边真实文本。
_IMAGE_PLACEHOLDER_PATTERN = re.compile(r"\[Image #\d+\]\s*", re.IGNORECASE)

# 空白折叠
_WHITESPACE_PATTERN = re.compile(r"\s+")


def _sanitize_user_text(raw: str) -> str:
    """剔除 Claude Code 注入的系统级 XML 块,还原真实用户输入。

    处理顺序:
    1. Slash command 优先识别 — 若检测到 <command-name>,合成"命令 + 参数"
       式标题(因为残留文本通常为空,直接取标签内容更有意义)。
    2. 通用噪声剥离 — 移除已知白名单内的 system-reminder 等标签。
    3. <session> 二次回退 — 若首轮剥离后为空,说明用户文本可能在 <session>
       标签内部;此时仅去除外壳标签,保留内部文本再做噪声剥离。
    4. 残留 command-* 包裹清除 — 兜底去除 command-message 等次要标签。
    5. 前后空白归一化 — 折叠连续空白为单空格。
    """
    if not raw:
        return ""

    # 阶段一: slash command 短路
    cmd = _CMD_NAME_PATTERN.search(raw)
    if cmd:
        name = cmd.group(1).strip()
        args_match = _CMD_ARGS_PATTERN.search(raw)
        args = args_match.group(1).strip() if args_match else ""
        composed = f"{name} {args}".strip() if args else name
        if composed:
            return composed

    # 阶段二: 通用噪声剥离
    cleaned = _NOISE_TAG_PATTERN.sub("", raw)
    cleaned = _CMD_WRAPPER_PATTERN.sub("", cleaned)
    cleaned = _PASTED_CONTENT_WRAPPER.sub("", cleaned)
    cleaned = _IMAGE_PLACEHOLDER_PATTERN.sub("", cleaned)
    cleaned = _WHITESPACE_PATTERN.sub(" ", cleaned).strip()
    if cleaned:
        return cleaned

    # 阶段三: <session> 二次回退
    # 当首轮全部剥离为空时,用户文本很可能被 <session> 标签完整包裹。
    # 此时不去除 <session> 块,而是仅剥掉外壳标签,保留内部文本后重新剥离。
    if "<session" in raw.lower():
        inner = _SESSION_TAG_WRAPPER.sub("", raw)
        cleaned = _NOISE_TAG_PATTERN.sub("", inner)
        cleaned = _CMD_WRAPPER_PATTERN.sub("", cleaned)
        cleaned = _WHITESPACE_PATTERN.sub(" ", cleaned).strip()
        if cleaned:
            return cleaned

    return ""


# ── Session 标题提取: 多层级回退策略 ──────────────────────────────
#
# Level 1: user TEXT → 噪声剥离 → 首条非空文本 (原有逻辑)
# Level 2: user TOOL_RESULT → text 截取 → "[Tool output] <snippet>"
# Level 3: user IMAGE → 计数 → "[1 Image]" / "[N Images]"
# Level 4: 请求元数据 → tool_names → "[Tool call] Bash, Read"
#          （模型名兜底已移除：合成模型名无业务语义）
# ─────────────────────────────────────────────────────────────────


# ── 注入式 Prompt（agent-as-tool 模板）载荷提取 ──────────────────
#
# Agent harness（记忆压缩器、会话命名器等）以子会话形式调用模型，首条 user
# 消息是固定话术模板（如 "You are a memory relevance compressor..."），
# 模板头无区分度，真实业务载荷内嵌在 "User query:" 等分隔符之后。
# 家族表按序匹配：(opener, 家族标签, 载荷分隔符, 尾部样板截断)；
# 命中后剥模板头截载荷，产出 "[家族标签] 载荷" 形式标题。
_INJECTED_PROMPT_FAMILIES: tuple[
    tuple[re.Pattern[str], str, re.Pattern[str], re.Pattern[str] | None, bool], ...
] = (
    (
        re.compile(r"(?i)^you are a memory relevance compressor\b"),
        "[记忆压缩]",
        re.compile(r"(?i)\bUser query\s*[:：]\s*"),
        re.compile(r"(?i)\bRetrieved\s+\S+\s+context fragments\b"),
        False,
    ),
    (
        re.compile(r"(?i)^Name this chat\b"),
        "[会话命名]",
        re.compile(r"(?i)\bFirst request\s*[:：]\s*"),
        re.compile(r"(?i)\bThis is a naming-only task\b"),
        False,
    ),
    (
        re.compile(r"(?i)^Generate a short git branch name\b"),
        "[分支命名]",
        re.compile(r"(?i)\bUser request\s*[:：]\s*"),
        None,
        False,
    ),
    (
        re.compile(r"(?i)^Use the \S+ tool to run exactly this one command\b"),
        "[单命令]",
        re.compile(r"(?i)(?:\band nothing else|The command is)\s*[:：]\s*"),
        None,
        False,
    ),
    # 通用兜底：其余 "You are ..." 注入式模板。分隔符要求句界锚定（防止真实
    # persona 消息中句中的 "task:"/"query:" 误截断）；无分隔符时按真实输入
    # 处理（fallback_real_input=True），避免误伤手打 "You are ..." 的用户消息。
    (
        re.compile(r"(?i)^you are\b"),
        "[注入任务]",
        re.compile(
            r"(?i)(?:^|(?<=[.。!?\n]))\s*(?:User query|User request|First request)\s*[:：]\s*"
        ),
        re.compile(r"(?i)\bRetrieved\s+\S+\s+context fragments\b"),
        True,
    ),
)


# task-notification 载荷凝缩：注入载荷常为 harness 的任务通知 XML 块，
# 其中的 <summary> 行（如 Agent "xx" finished）才是可扫读标识；
# 无 summary 时整块剥离后取残余文本。
_TASK_SUMMARY_PATTERN = re.compile(r"<summary>\s*(.*?)\s*</summary>", re.DOTALL)
_TASK_NOTIFICATION_BLOCK = re.compile(
    r"<task-notification\b[^>]*>.*?</task-notification>", re.DOTALL | re.IGNORECASE
)
# 未闭合的 <task-notification ...> 开标签兜底（块正则不生效时剥开头标签）。
_NOTIFICATION_OPEN_TAG = re.compile(r"<task-notification\b[^>]*>", re.IGNORECASE)
# 已知元数据标签：成对剥除时连同内文一起移除（id/路径/状态无扫读价值），
# 避免剥壳留内文产生孤立 id 残段。
_NOTIFICATION_ID_PAIRS = re.compile(
    r"<(?P<tag>task-id|tool-use-id|output-file|status|event|note)\b[^>]*>"
    r".*?</(?P=tag)>",
    re.DOTALL | re.IGNORECASE,
)
# 未成对的残余元数据标签（截断形态）与 summary 外壳：剥标签本身、保留 summary 内文。
_NOTIFICATION_ID_TAGS = re.compile(
    r"</?(?:task-id|tool-use-id|output-file|status|event|note|summary)\b[^>]*/?>",
    re.IGNORECASE,
)
# 实体转义形态的通知标记（&lt;task-notification / &gt;task-notification）：
# 仅命中时才对载荷做全量实体解码（legacy 无分号实体随之解码，属可接受代价），
# 其余载荷只做下方安全五元组的定向解码，避免 &not→¬ 类改写真实业务文本。
_ESCAPED_NOTIFICATION_MARK = re.compile(
    r"&(?:lt|gt|quot);/?(?:task-notification)", re.IGNORECASE
)
_SAFE_ENTITIES_MAP = {
    "&lt;": "<",
    "&gt;": ">",
    "&amp;": "&",
    "&quot;": '"',
    "&#39;": "'",
}
_SAFE_ENTITY_PATTERN = re.compile(
    "|".join(re.escape(k) for k in _SAFE_ENTITIES_MAP), re.IGNORECASE
)


def _condense_notification_payload(payload: str) -> str:
    """注入载荷为任务通知 XML 块时凝缩为可读文本，否则原样返回.

    ``<summary>`` 提取限定在 task-notification 块内（载荷正文自身出现的
    ``<summary>`` 字面量不触发凝缩）；块未闭合时退化为剥标签取残余文本。
    """
    if "<task-notification" not in payload.lower():
        return payload
    block = _TASK_NOTIFICATION_BLOCK.search(payload)
    if block:
        m = _TASK_SUMMARY_PATTERN.search(block.group(0))
        if m and m.group(1).strip():
            return m.group(1).strip()
    # 未闭合/无 summary 兜底：成对元数据标签连同内文剥除，summary 剥壳保内文，
    # 使残余文本与闭合块路径口径一致（无孤立 id 残段、无残留标签）。
    stripped = _TASK_NOTIFICATION_BLOCK.sub(" ", payload)
    stripped = _NOTIFICATION_OPEN_TAG.sub(" ", stripped)
    stripped = _NOTIFICATION_ID_PAIRS.sub(" ", stripped)
    stripped = _NOTIFICATION_ID_TAGS.sub(" ", stripped)
    return _WHITESPACE_PATTERN.sub(" ", stripped).strip()


def _extract_injected_prompt_title(cleaned: str) -> str | None:
    """注入式 Prompt 载荷标题提取.

    Returns:
        ``None`` — 非注入式消息（调用方按真实用户输入处理）；
        ``""`` — 命中注入家族但无载荷（模板全同质、无区分度，调用方应跳过该条）；
        ``str`` — ``[家族标签] 载荷`` 形式的标题。
    """
    for opener, tag, delim, tail, fallback_real_input in _INJECTED_PROMPT_FAMILIES:
        if not opener.search(cleaned):
            continue
        m = delim.search(cleaned)
        if not m:
            # 具名家族模板高度特异，无载荷即整条跳过；通用家族 opener 宽松，
            # 无载荷时按真实输入处理以免误伤手打 "You are ..." 的用户消息。
            return None if fallback_real_input else ""
        payload = cleaned[m.end() :]
        if tail:
            t = tail.search(payload)
            if t:
                payload = payload[: t.start()]
        # 实体解码分两档：转义形态的通知 XML 全量解码（先于凝缩守卫，转义形态
        # 需还原为字面标签才能被识别）；其余载荷仅定向解码带分号的安全实体
        # （&lt;/&gt;/&amp;/&quot;/&#39;），避免 html.unescape 的 HTML5 legacy
        # 无分号规则（&not→¬ 等）改写真实业务文本。
        has_escaped_notification = bool(_ESCAPED_NOTIFICATION_MARK.search(payload))
        if has_escaped_notification:
            payload = html.unescape(payload)
        payload = _condense_notification_payload(payload)
        if not has_escaped_notification:
            payload = _SAFE_ENTITY_PATTERN.sub(
                lambda m: _SAFE_ENTITIES_MAP[m.group(0).lower()], payload
            )
        payload = _WHITESPACE_PATTERN.sub(" ", payload).strip()
        if not payload:
            return ""
        return f"{tag} {payload}"[:_INJECTED_PAYLOAD_TITLE_MAX_LEN]
    return None


# ── CC 会话标题探测消息（session-title probe）载荷提取 ─────────
#
# Claude Code 在会话早期发送内部标题探测请求（haiku，querySource=
# generate_session_title），user 文本为固定指令模板，形态取自 CC 2.1.325
# 取证：可选未闭合 <session> 壳 + "Write the title in ..." 指令句；
# legacy 变体为裸 <session> 壳直接包裹真实输入。指令内嵌首条用户消息
# 原文时剥离指令取载荷；纯指令无载荷时跳过该条，待延迟补写回填。
# 此前靠 title_exempt_prefixes 配置豁免（且 <session> 前缀形态漏匹配，
# 历史库残留 256 条垃圾标题），现收敛为代码内建识别，配置可清零。
_PROBE_SESSION_TAG_PREFIX = re.compile(r"^\s*<session\b[^>]*>\s*", re.IGNORECASE)
_PROBE_INSTRUCTION_OPEN = re.compile(r"^\s*Write the title in\b", re.IGNORECASE)
# 指令块句尾锚定（当前两代模板 + legacy 逗号变体）；未命中的未知变体
# 整条跳过（返回 ""），宁可延迟补写也不产出垃圾标题。
_PROBE_INSTRUCTION_BLOCK = re.compile(
    r"Write the title in\b.*?"
    r"(?:in their original form\.|the English of these instructions\.|wrote in,)\s*",
    re.DOTALL | re.IGNORECASE,
)


def _extract_session_probe_payload(cleaned: str) -> str | None:
    """CC 标题探测消息 → 真实业务载荷.

    Returns:
        ``None`` — 非探测消息（调用方按真实用户输入/注入家族继续处理）；
        ``""`` — 探测指令无载荷（调用方跳过该条，靠延迟补写回填）；
        ``str`` — 剥离壳与指令后的真实载荷（首条用户消息原文）。
    """
    tag = _PROBE_SESSION_TAG_PREFIX.match(cleaned)
    text = cleaned[tag.end() :] if tag else cleaned
    if not _PROBE_INSTRUCTION_OPEN.match(text):
        # 裸 <session> 壳包裹真实输入（legacy 形态）：标签后即载荷
        if tag:
            return text.strip()
        return None
    stripped = _PROBE_INSTRUCTION_BLOCK.sub("", text, count=1)
    if stripped == text:
        # 指令块句尾锚定未命中（未知模板变体）：整条按无载荷跳过
        return ""
    return stripped.strip()


def _derive_title_from_user_texts(
    texts, exempt_prefixes: list[str] | None = None
) -> str:
    """从一批用户原始文本推导标题 — 供请求路径与历史回填共用.

    逐条清洗 → 豁免前缀跳过 → 注入式载荷提取 → 首条真实输入。
    豁免前缀同时作用于清洗文本与派生的注入式标题（豁免家族标签时
    跳过该条继续查找，而非丢弃整个 Level 1）。
    全部跳过时返回空串（调用方按无候选处理）。
    """
    prefixes = [p for p in (exempt_prefixes or []) if p]

    def _exempt(candidate: str) -> bool:
        if any(candidate.startswith(p) for p in prefixes):
            logger.debug(
                "Title candidate skipped by exempt prefix (snippet=%r)",
                candidate[:60],
            )
            return True
        return False

    for raw in texts:
        cleaned = _sanitize_user_text(raw)
        if not cleaned:
            continue
        # 整条消息即 harness 固定话术：背景任务通知 / 会话续接摘要，无区分度。
        if cleaned.startswith("<task-notification") or cleaned.startswith(
            "This session is being continued"
        ):
            continue
        if _exempt(cleaned):
            continue
        probe = _extract_session_probe_payload(cleaned)
        if probe is not None:
            # 探测家族命中：载荷即真实首条输入，直接成题；
            # 无载荷/未知变体跳过该条，靠延迟补写回填。
            if probe and not _exempt(probe):
                return probe[:_SESSION_TITLE_MAX_LEN]
            continue
        injected = _extract_injected_prompt_title(cleaned)
        if injected is None:
            return cleaned[:_SESSION_TITLE_MAX_LEN]
        if injected and not _exempt(injected):
            return injected
    return ""


def _extract_title_from_user_text(
    messages: list[CanonicalMessagePart],
    exempt_prefixes: list[str] | None = None,
) -> str:
    """Level 1: 从 user TEXT 部分提取经噪声剥离后的首条非空文本.

    ``exempt_prefixes`` 非空时，清洗后文本以任一前缀开头则跳过该输入、继续向后
    查找——用于过滤注入式 Prompt，避免其被误用作 Session 标题。匹配作用于
    ``_sanitize_user_text`` 清洗后的纯文本（而非带 XML 标签的原始 raw）。
    注入式模板消息（agent-as-tool 话术）经 ``_extract_injected_prompt_title``
    剥离模板头、截取业务载荷；无载荷则跳过该条继续查找。
    """
    texts = [
        part.text
        for part in messages
        if part.role == "user" and part.type == CanonicalPartType.TEXT
    ]
    return _derive_title_from_user_texts(texts, exempt_prefixes=exempt_prefixes)


def _extract_title_from_tool_results(messages: list[CanonicalMessagePart]) -> str:
    """Level 2: 从 user TOOL_RESULT 部分截取文本摘要."""
    for part in messages:
        if part.role != "user" or part.type != CanonicalPartType.TOOL_RESULT:
            continue
        if not part.text:
            continue
        cleaned = _sanitize_user_text(part.text)
        if cleaned:
            snippet = cleaned[:_FALLBACK_TITLE_MAX_LEN]
            return f"[Tool output] {snippet}"
    return ""


def _extract_title_from_images(messages: list[CanonicalMessagePart]) -> str:
    """Level 3: 统计 user IMAGE 部分数量,生成图片描述标题."""
    count = sum(
        1 for p in messages if p.role == "user" and p.type == CanonicalPartType.IMAGE
    )
    if count == 0:
        return ""
    return f"[{count} Image{'s' if count > 1 else ''}]"


def _extract_title_from_metadata(request: CanonicalRequest) -> str:
    """Level 4: 从请求元数据 (tool_names) 合成兜底标题.

    模型名兜底（``[Session] <model>``）已移除：合成模型名无业务语义，
    与「标题总具备实际识别意义的业务 message」目标冲突；无 user 输入的
    会话保持空标题，待延迟补写经 upsert 回填真实输入。
    """
    if request.tool_names:
        names = ", ".join(request.tool_names[:3])
        return f"[Tool call] {names}"
    return ""


def _extract_session_title(
    request: CanonicalRequest,
    exempt_prefixes: list[str] | None = None,
) -> str:
    """从规范化请求中提取 session 标题 — 多层级回退策略。

    依次尝试: user TEXT 噪声剥离 → TOOL_RESULT 摘要 → IMAGE 计数 → 元数据兜底。
    任一层级产出候选且未被豁免即返回,确保 Dashboard 尽可能展示有辨识度的标题。

    ``exempt_prefixes`` 对所有层级统一生效：任一层产出的候选标题若以任一前缀
    开头，则视为豁免、继续向下一层回退。Level 1 内部仍保留「逐条消息跳过」
    语义（清洗后文本以豁免前缀开头则跳过该条、继续查找下一条 user TEXT）；
    本函数在编排层对每层最终候选再做一次 ``_is_exempt`` 拦截，使 Level 2/3/4
    的合成标题（如 ``[Tool output]``、``[N Image(s)]``、``[Tool call]``）
    也能被用户显式豁免。全部层级均被豁免时返回空串，调用方
    ``if title:`` 据此跳过写库，session 标题保持空，待后续请求经
    ``set_session_title`` 幂等 upsert 回填有意义标题。默认 ``None`` 时行为与历史一致。
    """
    # 防御性过滤空串前缀："" startswith 恒真会误豁免一切。入参虽经配置层与
    # 构造器两层归一化，模块级直接调用（如测试）仍可能传入 [""]，此处兜底。
    prefixes = [p for p in (exempt_prefixes or []) if p]

    def _is_exempt(candidate: str) -> bool:
        """候选标题是否命中任一豁免前缀（大小写敏感 startswith）。"""
        return bool(prefixes) and any(candidate.startswith(p) for p in prefixes)

    messages = request.messages
    # Level 1: 内部已对每条 user TEXT 做豁免跳过；外层 _is_exempt 对其最终候选
    # 再做一次拦截（正常路径下冗余，作为 _sanitize_user_text 一致性兜底）。
    title = _extract_title_from_user_text(messages, exempt_prefixes=prefixes)
    if title and not _is_exempt(title):
        return title[:_SESSION_TITLE_MAX_LEN]
    # Level 2 / Level 3: 候选为合成标题，统一经 _is_exempt 拦截后回退。
    for extractor in (
        _extract_title_from_tool_results,
        _extract_title_from_images,
    ):
        title = extractor(messages)
        if title and not _is_exempt(title):
            return title[:_SESSION_TITLE_MAX_LEN]
    # Level 4: 签名不同（依赖 request 元数据）。被豁免则落到末尾返回空串，
    # 使调用方 if title: 跳过写库、保留空标题待后续回填。
    title = _extract_title_from_metadata(request)
    if title and not _is_exempt(title):
        return title[:_SESSION_TITLE_MAX_LEN]
    return ""


def _build_semantic_rejection_diagnostic(body: dict[str, Any]) -> str:
    """构建语义拒绝的请求体诊断上下文.

    在 semantic rejection 日志中附加请求体的可疑参数快照，
    用于定位供应商参数校验失败的具体祸根参数。

    覆盖范围：
      * 模型 / messages 数（baseline）
      * thinking 系列顶层参数 + history thinking_blocks 数
      * system 形态（string / blocks，含 cache_control 计数）
      * tools 数量 + tool_choice 形态
      * 采样参数（max_tokens / temperature / top_p / top_k / stop_sequences）
      * stream / metadata 形态
      * cache_control 存在性
      * messages.content 类型分布
      * 请求体大小估算（json.dumps 字节数）
    """
    parts: list[str] = []

    # ── 模型 + 消息数（baseline，始终输出）──
    parts.append(f"model={body.get('model', 'N/A')}")
    parts.append(f"messages={len(body.get('messages', []))}")

    # ── 顶层 thinking 系列参数 ──
    for key in ("thinking", "extended_thinking", "reasoning_effort"):
        if key in body:
            val = body[key]
            parts.append(f"{key}={val!r:.80}")

    # ── system 形态 ──
    system = body.get("system")
    if isinstance(system, str):
        parts.append(f"system_kind=string(len={len(system)})")
    elif isinstance(system, list):
        cc_count = sum(
            1 for item in system if isinstance(item, dict) and "cache_control" in item
        )
        if cc_count:
            parts.append(f"system_blocks={len(system)},cc={cc_count}")
        else:
            parts.append(f"system_blocks={len(system)}")

    # ── tools 与 tool_choice ──
    tools = body.get("tools")
    if isinstance(tools, list):
        parts.append(f"tools={len(tools)}")
    tool_choice = body.get("tool_choice")
    if tool_choice is not None:
        parts.append(f"tool_choice={tool_choice!r:.60}")

    # ── 采样参数（仅存在时输出）──
    for key in ("max_tokens", "temperature", "top_p", "top_k"):
        if key in body:
            parts.append(f"{key}={body[key]!r:.40}")
    stop_sequences = body.get("stop_sequences")
    if isinstance(stop_sequences, list) and stop_sequences:
        parts.append(f"stop_sequences={len(stop_sequences)}")

    # ── stream / metadata ──
    if "stream" in body:
        parts.append(f"stream={body['stream']}")
    metadata = body.get("metadata")
    if isinstance(metadata, dict) and metadata:
        parts.append(f"metadata_keys={len(metadata)}")

    # ── 会话历史中的 thinking blocks 与 content_types 分布 ──
    thinking_count = 0
    content_type_counts: dict[str, int] = {}
    for msg in body.get("messages", []):
        content = msg.get("content")
        if isinstance(content, str):
            content_type_counts["string"] = content_type_counts.get("string", 0) + 1
            continue
        if not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, dict):
                continue
            btype = block.get("type")
            if isinstance(btype, str):
                content_type_counts[btype] = content_type_counts.get(btype, 0) + 1
            if btype in ("thinking", "redacted_thinking"):
                thinking_count += 1
    if thinking_count:
        parts.append(f"thinking_blocks_in_history={thinking_count}")
    if content_type_counts:
        type_repr = ",".join(f"{k}:{v}" for k, v in sorted(content_type_counts.items()))
        parts.append(f"content_types={{{type_repr}}}")

    # ── cache_control 存在检测（messages / tools，不含 system 因已单独统计）──
    has_cc = False
    sections: list[Any] = []
    for m in body.get("messages", []):
        if isinstance(m.get("content"), list):
            sections.append(m["content"])
    if isinstance(body.get("tools"), list):
        sections.append(body["tools"])
    for section in sections:
        for item in section:
            if isinstance(item, dict) and "cache_control" in item:
                has_cc = True
                break
        if has_cc:
            break
    if has_cc:
        parts.append("cache_control_fields=present")

    # ── 请求体大小估算 ──
    try:
        body_bytes = len(json.dumps(body, ensure_ascii=False).encode("utf-8"))
        parts.append(f"body_bytes={body_bytes}")
    except (TypeError, ValueError):
        # 极少数情况下 body 含非可序列化对象，跳过
        pass

    return f" [{', '.join(parts)}]" if parts else ""


def _build_semantic_rejection_diagnostic(body: dict[str, Any]) -> str:
    """构建语义拒绝的请求体诊断上下文.

    在 semantic rejection 日志中附加请求体的可疑参数快照，
    用于定位供应商参数校验失败的具体祸根参数。

    覆盖范围：
      * 模型 / messages 数（baseline）
      * thinking 系列顶层参数 + history thinking_blocks 数
      * system 形态（string / blocks，含 cache_control 计数）
      * tools 数量 + tool_choice 形态
      * 采样参数（max_tokens / temperature / top_p / top_k / stop_sequences）
      * stream / metadata 形态
      * cache_control 存在性
      * messages.content 类型分布
      * 请求体大小估算（json.dumps 字节数）
    """
    parts: list[str] = []

    # ── 模型 + 消息数（baseline，始终输出）──
    parts.append(f"model={body.get('model', 'N/A')}")
    parts.append(f"messages={len(body.get('messages', []))}")

    # ── 顶层 thinking 系列参数 ──
    for key in ("thinking", "extended_thinking", "reasoning_effort"):
        if key in body:
            val = body[key]
            parts.append(f"{key}={val!r:.80}")

    # ── system 形态 ──
    system = body.get("system")
    if isinstance(system, str):
        parts.append(f"system_kind=string(len={len(system)})")
    elif isinstance(system, list):
        cc_count = sum(
            1 for item in system if isinstance(item, dict) and "cache_control" in item
        )
        if cc_count:
            parts.append(f"system_blocks={len(system)},cc={cc_count}")
        else:
            parts.append(f"system_blocks={len(system)}")

    # ── tools 与 tool_choice ──
    tools = body.get("tools")
    if isinstance(tools, list):
        parts.append(f"tools={len(tools)}")
    tool_choice = body.get("tool_choice")
    if tool_choice is not None:
        parts.append(f"tool_choice={tool_choice!r:.60}")

    # ── 采样参数（仅存在时输出）──
    for key in ("max_tokens", "temperature", "top_p", "top_k"):
        if key in body:
            parts.append(f"{key}={body[key]!r:.40}")
    stop_sequences = body.get("stop_sequences")
    if isinstance(stop_sequences, list) and stop_sequences:
        parts.append(f"stop_sequences={len(stop_sequences)}")

    # ── stream / metadata ──
    if "stream" in body:
        parts.append(f"stream={body['stream']}")
    metadata = body.get("metadata")
    if isinstance(metadata, dict) and metadata:
        parts.append(f"metadata_keys={len(metadata)}")

    # ── 会话历史中的 thinking blocks 与 content_types 分布 ──
    thinking_count = 0
    content_type_counts: dict[str, int] = {}
    for msg in body.get("messages", []):
        content = msg.get("content")
        if isinstance(content, str):
            content_type_counts["string"] = content_type_counts.get("string", 0) + 1
            continue
        if not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, dict):
                continue
            btype = block.get("type")
            if isinstance(btype, str):
                content_type_counts[btype] = content_type_counts.get(btype, 0) + 1
            if btype in ("thinking", "redacted_thinking"):
                thinking_count += 1
    if thinking_count:
        parts.append(f"thinking_blocks_in_history={thinking_count}")
    if content_type_counts:
        type_repr = ",".join(f"{k}:{v}" for k, v in sorted(content_type_counts.items()))
        parts.append(f"content_types={{{type_repr}}}")

    # ── cache_control 存在检测（messages / tools，不含 system 因已单独统计）──
    has_cc = False
    sections: list[Any] = []
    for m in body.get("messages", []):
        if isinstance(m.get("content"), list):
            sections.append(m["content"])
    if isinstance(body.get("tools"), list):
        sections.append(body["tools"])
    for section in sections:
        for item in section:
            if isinstance(item, dict) and "cache_control" in item:
                has_cc = True
                break
        if has_cc:
            break
    if has_cc:
        parts.append("cache_control_fields=present")

    # ── 请求体大小估算 ──
    try:
        body_bytes = len(json.dumps(body, ensure_ascii=False).encode("utf-8"))
        parts.append(f"body_bytes={body_bytes}")
    except (TypeError, ValueError):
        # 极少数情况下 body 含非可序列化对象，跳过
        pass

    return f" [{', '.join(parts)}]" if parts else ""


def _log_http_error_detail(
    tier_name: str,
    exc: Exception,
    *,
    is_stream: bool = False,
    tier: VendorTier | None = None,
) -> None:
    """记录 HTTP 错误的详细信息（状态码 / 响应体摘要 / 异常类型 / 熔断器快照）.

    替代原先单行 ``logger.warning("Tier %s stream failed: %s", ...)``，
    在非 200 响应时输出更丰富的诊断上下文，便于跟踪上游故障根因。
    """
    detail_parts = [f"Tier {tier_name} {'stream' if is_stream else 'message'} failed:"]
    detail_parts.append(f"  exc_type={type(exc).__name__}")
    if isinstance(exc, httpx.HTTPStatusError) and exc.response is not None:
        resp = exc.response
        detail_parts.append(f"  status={resp.status_code}")
        body_preview = (
            (resp.text[:300] if resp.text else "(empty)")
            if resp.content
            else "(no content)"
        )
        detail_parts.append(f"  response_body={body_preview}")
        # 尝试提取 error type / message
        try:
            payload = resp.json() if resp.content else None
        except Exception:
            payload = None
        if isinstance(payload, dict):
            err = payload.get("error", {})
            if isinstance(err, dict):
                detail_parts.append(f"  error_type={err.get('type', 'N/A')}")
                detail_parts.append(f"  error_msg={err.get('message', 'N/A')[:200]}")
    else:
        detail_parts.append(f"  message={str(exc)[:300]}")
    # 熔断器状态快照
    if tier and tier.circuit_breaker:
        cb = tier.circuit_breaker
        cb_info = cb.get_info()
        detail_parts.append(
            f"  circuit_breaker={cb_info['state']} "
            f"(failures={cb_info['failure_count']}/{cb._failure_threshold})"
        )
    logger.warning("\n".join(detail_parts))


def _has_tool_results(body: dict[str, Any]) -> bool:
    """检测请求体是否包含 tool_result 内容块.

    用于诊断日志中标记「当前请求是否处于工具执行循环」，
    帮助快速定位 vendor 对 tool_result 处理不兼容的问题（如 Zhipu 500）.
    """
    for msg in body.get("messages", []):
        content = msg.get("content")
        if not isinstance(content, list):
            continue
        if any(isinstance(b, dict) and b.get("type") == "tool_result" for b in content):
            return True
    return False


def _is_likely_request_format_error(
    status_code: int,
    error_body_text: str | None,
    body: dict[str, Any],
) -> bool:
    """判断 HTTP 错误是否可能由请求格式不兼容导致（而非供应商故障）.

    当请求包含 tool_result 且供应商返回 400 时，极大概率是消息格式转换
    问题（如 tool_result 错位、字段缺失等），此类错误不应计入熔断器，
    因为重试同一格式的请求必然再次失败。

    此函数是 :func:`is_semantic_rejection` 的补充——后者依赖结构化 error body
    （JSON），而部分供应商（如 Copilot）的 400 响应可能是纯文本 ``Bad Request``。
    """
    if status_code != 400:
        return False
    if not _has_tool_results(body):
        return False
    # 400 + 有 tool_result + 无法解析为结构化错误 → 高概率格式问题
    if error_body_text is not None:
        trimmed = error_body_text.strip().lower()
        # 纯文本 400 响应（Copilot 等）或无意义的错误体
        if trimmed in ("bad request", "bad request\n", ""):
            return True
        # 非结构化响应体（非 JSON）
        if not trimmed.startswith("{") and len(trimmed) < 200:
            return True
        # 结构化 JSON 400 但含 tool_call 格式错误码 → 格式不兼容
        # （如 Copilot 返回 {"error":{"code":"invalid_tool_call_format",...}}）
        if "invalid_tool_call_format" in trimmed:
            return True
    return False


def _log_vendor_response_error(
    tier_name: str,
    resp: VendorResponse,
    body: dict[str, Any],
    *,
    is_stream: bool = False,
) -> None:
    """记录供应商返回的非 200 VendorResponse 详细信息.

    补充 :func:`_log_http_error_detail` 的覆盖盲区：
    当 ``send_message()`` 返回 ``VendorResponse(status_code>=400)``
    而非抛出 httpx 异常时，该函数提供等价的诊断日志能力。

    典型场景：Zhipu 等薄透传供应商将上游 500 原样包装为
    VendorResponse 返回，executor 的异常捕获路径不会触发。
    """
    mode = "stream" if is_stream else "message"
    detail_parts = [f"Tier {tier_name} {mode} vendor error response:"]
    detail_parts.append(f"  status={resp.status_code}")
    detail_parts.append(f"  error_type={resp.error_type or 'N/A'}")
    detail_parts.append(f"  error_msg={(resp.error_message or 'N/A')[:300]}")
    # 请求上下文（模型 / 工具 / 工具结果）
    model = body.get("model", "unknown")
    has_tools = bool(body.get("tools"))
    has_tool_results = _has_tool_results(body)
    detail_parts.append(f"  model={model}")
    detail_parts.append(f"  has_tools={has_tools}")
    detail_parts.append(f"  has_tool_results={has_tool_results}")
    # 响应体摘要
    if resp.raw_body:
        try:
            raw_text = resp.raw_body.decode("utf-8", errors="replace")[:500]
        except (AttributeError, UnicodeDecodeError):
            raw_text = f"(binary, {len(resp.raw_body)} bytes)"
        detail_parts.append(f"  response_body_preview={raw_text}")
    logger.warning("\n".join(detail_parts))


# tier.name → 上游 Vendor 协议标签映射（用于 token 用量日志标注）
_VENDOR_PROTOCOL_LABEL_MAP: dict[str, str] = {
    "anthropic": "Anthropic",
    "zhipu": "Anthropic",
    "minimax": "Anthropic",
    "kimi": "Anthropic",
    "doubao": "Anthropic",
    "xiaomi": "Anthropic",
    "alibaba": "Anthropic",
    "copilot": "OpenAI",
    "antigravity": "Gemini",
}


class _RouteExecutor:
    """统一的 tier 迭代门控引擎.

    职责：
    - 按优先级遍历 tiers，执行能力门控与健康检查
    - 委托具体的流式/非流式执行给调用方回调
    - 统一处理 TokenAcquireError / HTTP 错误 / 语义拒绝
    - 成功后委托 UsageRecorder 记录用量
    """

    def __init__(
        self,
        router: Any,  # RequestRouter 引用，用于写入活跃供应商状态
        tiers: list[VendorTier],
        usage_recorder: UsageRecorder,
        session_manager: RouteSessionManager,
        reauth_coordinator: Any | None = None,
        session_policy_resolver: SessionPolicyResolver | None = None,
        title_vendor_bindings: list[TitleVendorBinding] | None = None,
        title_exempt_prefixes: list[str] | None = None,
    ) -> None:
        self._router = router
        self._tiers = tiers
        self._recorder = usage_recorder
        self._session_mgr = session_manager
        self._reauth_coordinator = reauth_coordinator
        self._policy_resolver = session_policy_resolver or SessionPolicyResolver()
        self._title_vendor_bindings = title_vendor_bindings or []
        # 豁免前缀名单：构造期二次归一化（strip + 去空），双重防御配置层 field_validator。
        # 即便绕过配置层直接构造 executor，也能避免空串 startswith 恒真与纯空白失配。
        self._title_exempt_prefixes: list[str] = [
            stripped
            for p in (title_exempt_prefixes or [])
            if isinstance(p, str) and (stripped := p.strip())
        ]
        self._validate_title_vendor_bindings()

        # Tier 名称 → OAuth provider 名称的映射
        self._tier_provider_map: dict[str, str] = {
            "copilot": "github",
            "antigravity": "google",
        }

    def _validate_title_vendor_bindings(self) -> None:
        """启动期校验标题绑定引用的 vendor 均存在,缺失则告警.

        与手动绑定 API（拒绝未知 vendor）的语义对齐：此处不硬失败，
        仅记录警告——避免单条误配置阻断整个代理启动；运行时
        `_resolve_effective_tiers` 会静默跳过未知 vendor 回退默认顺序。
        """
        if not self._title_vendor_bindings:
            return
        valid = {t.name for t in self._tiers}
        for binding in self._title_vendor_bindings:
            if binding.vendor not in valid:
                logger.warning(
                    "title_vendor_bindings 引用了未知 vendor %r（前缀 %r）；"
                    "可用 vendor: %s。该绑定将在运行时被静默跳过。",
                    binding.vendor,
                    binding.prefix,
                    sorted(valid),
                )

    # ── 公开执行入口 ──────────────────────────────────────

    def _resolve_effective_tiers(
        self, session_key: str, policy: SessionPolicy | None = None
    ) -> list[VendorTier]:
        """根据 Session Policy 解析生效的 tier 顺序.

        策略指定的 vendor 按其顺序排列在头部，未提及的保持在末尾。
        无策略时返回全局默认顺序。policy 可由调用方预先 resolve 传入，
        与会话级模型覆盖共用同一次解析（避免双重 resolve 的竞态窗口）。
        """
        if policy is None:
            policy = self._policy_resolver.resolve(session_key)
        if not policy or not policy.tiers:
            return self._tiers

        name_to_tier = {t.name: t for t in self._tiers}
        ordered: list[VendorTier] = []
        seen: set[str] = set()
        for name in policy.tiers:
            tier = name_to_tier.get(name)
            if tier and name not in seen:
                ordered.append(tier)
                seen.add(name)
        for tier in self._tiers:
            if tier.name not in seen:
                ordered.append(tier)
                seen.add(tier.name)
        return ordered

    _MODEL_FALLBACK_REASONS = frozenset({"default"})

    @classmethod
    def _log_model_drift(
        cls,
        bound_model: str | None,
        tier: VendorTier,
        model_served: str,
    ) -> None:
        """会话模型覆盖生效但实际 served 模型偏离预期时告警.

        两类触发：served 既非绑定名也非接单 vendor 映射预期（failover 接单 /
        上游换名），或绑定名命中映射 default 回退被改写（缺显式规则）。
        配置内等价映射（如 claude 档位名 → glm 档位名）属预期行为，不告警。
        """
        if not bound_model:
            return
        expected, reason = tier.vendor.map_model_with_reason(bound_model)
        if model_served not in (bound_model, expected):
            logger.warning(
                "Session model drift: bound=%s but tier %s served %s "
                "(its model mapping expected %s — failover takeover or "
                "unexpected upstream substitution)",
                bound_model,
                tier.name,
                model_served,
                expected,
            )
        elif reason in cls._MODEL_FALLBACK_REASONS and model_served != bound_model:
            logger.warning(
                "Session model drift: bound=%s hit model_mapping default fallback "
                "on tier %s and was rewritten to %s — add an identity mapping "
                "rule (pattern=target) for this model",
                bound_model,
                tier.name,
                model_served,
            )

    def _extract_session_title(self, request: CanonicalRequest) -> str:
        """封装模块级标题提取，注入实例持有的豁免前缀名单.

        将 ``self._title_exempt_prefixes`` 透传给模块级 ``_extract_session_title``，
        使 Level 1 能跳过以豁免前缀开头的注入式 Prompt。4 处标题提取调用点统一
        经此方法，避免重复透传（零透传污染）。
        """
        return _extract_session_title(
            request, exempt_prefixes=self._title_exempt_prefixes
        )

    async def _maybe_write_session_title(self, canonical_request) -> None:
        """提取并落库 session 标题（幂等 upsert），仅在真实写入时触发联动策略.

        统一覆盖新 session 首写与延迟补写：行缺失补建（首请求全层级被豁免
        未建行时由后续请求自愈）、空标题更新、已有标题不动。compat TTL 过期
        重判新会话时行已存在（written=False），自动绑定不复活已删的运行时绑定。
        """
        title = self._extract_session_title(canonical_request)
        if not title:
            return
        written = await self._recorder.set_session_title(
            canonical_request.session_key, title
        )
        if written:
            self._apply_title_based_policy(canonical_request.session_key, title)

    def _apply_title_based_policy(self, session_key: str, title: str) -> None:
        """根据 Session 标题前缀自动绑定供应商.

        当标题以预配置的前缀开头时，通过 SessionPolicyResolver.upsert()
        将该 Session 绑定到指定供应商，后续请求无需再走默认路由。

        仅在标题首次落库时调用（新 session 首写或延迟补写真正写入时），
        避免覆盖手动绑定的策略。
        """
        if not title or not self._title_vendor_bindings:
            return
        # 已有运行时绑定（手动 Vendor/Model Bind）时跳过，防自动绑定整条覆盖；
        # compat TTL 过期导致长会话被重新判定为新会话时同样受此保护。
        if self._policy_resolver.get_runtime_binding(session_key) is not None:
            return
        for binding in self._title_vendor_bindings:
            if title.startswith(binding.prefix):
                self._policy_resolver.upsert(session_key, [binding.vendor])
                logger.info(
                    "Session title prefix %r matched → auto-bind to %s (session=%s)",
                    binding.prefix,
                    binding.vendor,
                    session_key[:12],
                )
                return

    def _prepare_body_for_tier(
        self,
        body: dict[str, Any],
        tier: VendorTier,
        source_vendor: str | None = None,
    ) -> dict[str, Any]:
        """为指定 tier 准备请求体，应用源→目标绑定转换通道.

        通过 VENDOR_TRANSITIONS 注册表查表分发，executor 不感知具体供应商逻辑。
        未注册转换的源→目标对或 source_vendor 为 None 时原样返回请求体。
        """
        from ..convert.vendor_channels import get_transition_channel

        if source_vendor is None:
            return body

        channel_fn = get_transition_channel(source_vendor, tier.name)
        if channel_fn is None:
            return body

        prepared, adaptations = channel_fn(body)
        if adaptations:
            logger.debug(
                "Applied transition channel %s → %s: %s",
                source_vendor,
                tier.name,
                ", ".join(adaptations),
            )
        return prepared

    @staticmethod
    def _determine_source_vendor(
        target_name: str,
        failed_tier_name: str | None,
        session_record: Any,
        body: dict[str, Any] | None = None,
    ) -> str | None:
        """确定跨供应商转换的源 vendor.

        Priority 1: failed_tier_name（请求内故障转移，最可靠）。
        Priority 2: session_record.provider_state 中有已注册转换的 vendor（跨请求）。
        Priority 3: 从 body 内容推断（兜底首次请求无会话状态场景）。
        """
        from ..convert.vendor_channels import (
            get_transition_channel,
            infer_source_vendor_from_body,
        )

        # 请求内：刚失败的 tier 就是源（仅当存在已注册的转换通道时）
        # 修复：原逻辑仅检查 failed_tier != target 就无条件返回，
        # 导致无注册通道的 failed_tier（如 copilot→anthropic）阻断降级到
        # Priority 2/3，原始 body 中的 server_tool_use 等非标准块未被清理。
        if (
            failed_tier_name
            and get_transition_channel(failed_tier_name, target_name) is not None
        ):
            return failed_tier_name

        # 跨请求：从会话历史找有注册转换的源
        if session_record is not None and session_record.provider_state:
            for source in session_record.provider_state:
                if source != target_name and get_transition_channel(
                    source, target_name
                ):
                    return source

        # 首次请求兜底：从 body 内容推断（识别 zhipu 产物等）
        if body is not None:
            inferred = infer_source_vendor_from_body(body)
            if (
                inferred
                and inferred != target_name
                and get_transition_channel(inferred, target_name)
            ):
                return inferred

        return None

    async def execute_stream(
        self,
        body: dict[str, Any],
        headers: dict[str, str],
    ) -> AsyncIterator[tuple[bytes, str]]:
        """路由流式请求，按优先级尝试各层级."""
        last_exc: Exception | None = None
        failed_tier_name: str | None = None
        request_caps = build_request_capabilities(body)
        canonical_request = build_canonical_request(body, headers)
        session_record, _is_new_session = await self._session_mgr.get_or_create_record(
            canonical_request.session_key,
            canonical_request.trace_id,
        )
        await self._maybe_write_session_title(canonical_request)
        incompatible_reasons: list[str] = []
        session_policy = self._policy_resolver.resolve(canonical_request.session_key)
        client_model = str(body.get("model", "unknown"))
        bound_model = session_policy.model if session_policy else None
        if bound_model and bound_model != client_model:
            logger.info(
                "Session model override active: session=%s client_model=%s → bound_model=%s",
                canonical_request.session_key[:12],
                client_model,
                bound_model,
            )
            body = {**body, "model": bound_model}
        effective_tiers = self._resolve_effective_tiers(
            canonical_request.session_key, policy=session_policy
        )
        last_idx = len(effective_tiers) - 1

        for i, tier in enumerate(effective_tiers):
            is_last = i == last_idx

            gate = await self._try_gate_tier(
                tier,
                is_last,
                request_caps,
                canonical_request,
                session_record,
                incompatible_reasons,
            )
            if gate == "skip":
                continue

            start = time.monotonic()
            usage: dict[str, Any] = {}

            try:
                source_vendor = _RouteExecutor._determine_source_vendor(
                    tier.name, failed_tier_name, session_record, body
                )
                body_for_tier = self._prepare_body_for_tier(body, tier, source_vendor)
                _mapped_model = tier.vendor.map_model(body.get("model", ""))
                async with tier.vendor.track_in_flight(_mapped_model):
                    async for chunk in tier.vendor.send_message_stream(
                        body_for_tier, headers
                    ):
                        parse_usage_from_chunk(
                            chunk,
                            usage,
                            vendor_label=_VENDOR_PROTOCOL_LABEL_MAP.get(tier.name),
                        )
                        yield chunk, tier.name

                info = self._recorder.build_usage_info(usage)
                if has_missing_input_usage_signals(info):
                    logger.warning(
                        "Stream completed with missing input usage signals: output_tokens=%d, "
                        "cache_creation_tokens=%d, cache_read_tokens=%d, tier=%s, usage_data=%r",
                        info.output_tokens,
                        info.cache_creation_tokens,
                        info.cache_read_tokens,
                        tier.name,
                        usage,
                    )
                tier.record_success(
                    info.input_tokens
                    + info.output_tokens
                    + info.cache_creation_tokens
                    + info.cache_read_tokens
                )
                duration = int((time.monotonic() - start) * 1000)
                model = body.get("model", "unknown")
                model_served = usage.get("model_served") or tier.vendor.map_model(model)
                self._log_model_drift(bound_model, tier, model_served)
                if failed_tier_name is not None:
                    logger.info(
                        "Tier %s stream succeeded (took over from failed tier: %s)",
                        tier.name,
                        failed_tier_name,
                    )
                self._recorder.log_model_call(
                    vendor=tier.name,
                    model_requested=model,
                    model_served=model_served,
                    duration_ms=duration,
                    usage=info,
                )
                await self._session_mgr.persist_session(
                    tier.vendor.get_compat_trace(), session_record
                )
                await self._recorder.record(
                    tier.name,
                    model,
                    model_served,
                    info,
                    duration,
                    True,
                    failed_tier_name is not None,
                    failed_tier_name,
                    evidence_records=build_usage_evidence_records(
                        usage,
                        vendor=tier.name,
                        model_served=model_served,
                        request_id=info.request_id,
                    ),
                    session_key=canonical_request.session_key,
                )
                self._router._active_vendor_name = tier.name  # 更新活跃供应商
                return

            except TokenAcquireError as exc:
                failed_tier_name, last_exc = await self._handle_token_error(
                    tier, exc, is_last, failed_tier_name
                )
                if is_last and last_exc is exc:
                    raise

            except (
                httpx.HTTPStatusError,
                httpx.TimeoutException,
                httpx.ConnectError,
                httpx.ReadError,
            ) as exc:
                _log_http_error_detail(tier.name, exc, is_stream=True, tier=tier)
                (
                    should_continue,
                    failed_tier_name,
                    last_exc,
                ) = await self._handle_http_error(
                    tier,
                    exc,
                    is_last,
                    failed_tier_name,
                    last_exc,
                    is_stream=True,
                    request_body=body,
                )
                if should_continue:
                    self._log_failover_transition(tier, exc, self._tiers, i)
                    continue
                if is_last:
                    raise
                # 结构性验证错误（如 tool_result 角色错位）不应级联到下一层：
                # 同样的畸形请求转发到其他供应商只会重复失败。
                if isinstance(exc, httpx.HTTPStatusError) and exc.response is not None:
                    if is_structural_validation_error(
                        status_code=exc.response.status_code,
                        error_message=self._extract_error_message_from_http_status(exc),
                    ):
                        logger.info(
                            "Tier %s structural validation error, stopping failover",
                            tier.name,
                        )
                        raise
            except Exception as exc:
                _log_unexpected("stream", tier.name, exc)
                tier.record_failure()
                failed_tier_name = tier.name
                if not is_last:
                    continue
                raise

        if last_exc:
            raise last_exc
        raise NoCompatibleVendorError(
            "当前请求包含仅客户端/MCP 可安全承接的能力，未找到兼容供应商",
            reasons=incompatible_reasons,
        )

    async def execute_message(
        self,
        body: dict[str, Any],
        headers: dict[str, str],
    ) -> VendorResponse:
        """路由非流式请求，按优先级尝试各层级."""
        start = time.monotonic()
        failed_tier_name: str | None = None
        request_caps = build_request_capabilities(body)
        canonical_request = build_canonical_request(body, headers)
        session_record, _is_new_session = await self._session_mgr.get_or_create_record(
            canonical_request.session_key,
            canonical_request.trace_id,
        )
        await self._maybe_write_session_title(canonical_request)
        incompatible_reasons: list[str] = []
        session_policy = self._policy_resolver.resolve(canonical_request.session_key)
        client_model = str(body.get("model", "unknown"))
        bound_model = session_policy.model if session_policy else None
        if bound_model and bound_model != client_model:
            logger.info(
                "Session model override active: session=%s client_model=%s → bound_model=%s",
                canonical_request.session_key[:12],
                client_model,
                bound_model,
            )
            body = {**body, "model": bound_model}
        effective_tiers = self._resolve_effective_tiers(
            canonical_request.session_key, policy=session_policy
        )
        last_idx = len(effective_tiers) - 1

        for i, tier in enumerate(effective_tiers):
            is_last = i == last_idx

            gate = await self._try_gate_tier(
                tier,
                is_last,
                request_caps,
                canonical_request,
                session_record,
                incompatible_reasons,
            )
            if gate == "skip":
                continue

            try:
                source_vendor = _RouteExecutor._determine_source_vendor(
                    tier.name, failed_tier_name, session_record, body
                )
                body_for_tier = self._prepare_body_for_tier(body, tier, source_vendor)
                _mapped_model = tier.vendor.map_model(body.get("model", ""))
                async with tier.vendor.track_in_flight(_mapped_model):
                    resp = await tier.vendor.send_message(body_for_tier, headers)

                if resp.status_code < 400:
                    duration = int((time.monotonic() - start) * 1000)
                    model = body.get("model", "unknown")
                    model_served = resp.model_served or tier.vendor.map_model(model)
                    self._log_model_drift(bound_model, tier, model_served)
                    if failed_tier_name is not None:
                        logger.info(
                            "Tier %s message succeeded (took over from failed tier: %s)",
                            tier.name,
                            failed_tier_name,
                        )
                    self._recorder.log_model_call(
                        vendor=tier.name,
                        model_requested=model,
                        model_served=model_served,
                        duration_ms=duration,
                        usage=resp.usage,
                    )
                    await self._session_mgr.persist_session(
                        tier.vendor.get_compat_trace(), session_record
                    )
                    await self._recorder.record(
                        tier.name,
                        model,
                        model_served,
                        resp.usage,
                        duration,
                        True,
                        failed_tier_name is not None,
                        failed_tier_name,
                        evidence_records=self._recorder.build_nonstream_evidence_records(
                            vendor=tier.name,
                            model_served=model_served,
                            usage=resp.usage,
                        ),
                        session_key=canonical_request.session_key,
                    )
                    self._router._active_vendor_name = tier.name  # 更新活跃供应商
                    return resp

                # 非流式的 semantic rejection 和 failover 判断（从响应对象而非异常中提取）
                is_semantic = is_semantic_rejection(
                    status_code=resp.status_code,
                    error_type=resp.error_type,
                    error_message=resp.error_message,
                )
                # 补充检测：400 + 有 tool_result + 无结构化错误体 → 格式不兼容
                # （覆盖 Copilot 等返回纯文本 "Bad Request" 的场景）
                if not is_semantic and _is_likely_request_format_error(
                    status_code=resp.status_code,
                    error_body_text=(resp.error_message or "")[:500],
                    body=body,
                ):
                    is_semantic = True
                    logger.warning(
                        "Tier %s likely format incompatibility (400 + tool_results), "
                        "trying next tier without recording failure",
                        tier.name,
                    )

                if not is_last and is_semantic:
                    diagnostic = _build_semantic_rejection_diagnostic(body)
                    # zhipu 等供应商的错误体含字段级诊断（如 [1210] 错误码 + request_id），
                    # 500 字符足以覆盖完整错误体，避免截断丢失关键细节
                    err_msg = (resp.error_message or "N/A")[:500]
                    logger.warning(
                        "Tier %s semantic rejection (type=%s, msg=%s)%s, "
                        "trying next tier without recording failure",
                        tier.name,
                        resp.error_type or resp.status_code,
                        err_msg,
                        diagnostic,
                    )
                    failed_tier_name = tier.name
                    continue

                if tier.vendor.should_trigger_failover(
                    resp.status_code,
                    {"error": {"type": resp.error_type, "message": resp.error_message}},
                ):
                    rl_info = parse_rate_limit_headers(
                        resp.response_headers, resp.status_code, resp.error_message
                    )
                    tier.record_failure(
                        is_cap_error=self._is_cap_error(resp) or rl_info.is_cap_error,
                        retry_after_seconds=compute_effective_retry_seconds(rl_info),
                        rate_limit_deadline=compute_rate_limit_deadline(rl_info),
                    )
                    if not is_last:
                        next_tier = (
                            self._tiers[i + 1] if i + 1 < len(self._tiers) else None
                        )
                        next_info = f" → next: {next_tier.name}" if next_tier else ""
                        logger.warning(
                            "Tier %s error %d, failing over%s",
                            tier.name,
                            resp.status_code,
                            next_info,
                        )
                        failed_tier_name = tier.name
                        continue

                # 最后一层或不可 failover 的错误：记录并返回原始响应
                _log_vendor_response_error(tier.name, resp, body, is_stream=False)
                duration = int((time.monotonic() - start) * 1000)
                model = body.get("model", "unknown")
                model_served = resp.model_served or tier.vendor.map_model(model)
                self._recorder.log_model_call(
                    vendor=tier.name,
                    model_requested=model,
                    model_served=model_served,
                    duration_ms=duration,
                    usage=resp.usage,
                )
                await self._recorder.record(
                    tier.name,
                    model,
                    model_served,
                    resp.usage,
                    duration,
                    resp.status_code < 400,
                    failed_tier_name is not None,
                    failed_tier_name,
                    evidence_records=self._recorder.build_nonstream_evidence_records(
                        vendor=tier.name, model_served=model_served, usage=resp.usage
                    ),
                    session_key=canonical_request.session_key,
                )
                return resp

            except TokenAcquireError as exc:
                failed_tier_name, last_exc = await self._handle_token_error(
                    tier, exc, is_last, failed_tier_name
                )
                if is_last:
                    raise
                continue

            except (httpx.TimeoutException, httpx.ConnectError, httpx.ReadError) as exc:
                _log_http_error_detail(tier.name, exc, is_stream=False, tier=tier)
                tier.record_failure()
                failed_tier_name = tier.name
                if is_last:
                    raise
                continue
            except Exception as exc:
                _log_unexpected("message", tier.name, exc)
                tier.record_failure()
                failed_tier_name = tier.name
                if not is_last:
                    continue
                raise

        if incompatible_reasons:
            raise NoCompatibleVendorError(
                "当前请求包含仅客户端/MCP 可安全承接的能力，未找到兼容供应商",
                reasons=incompatible_reasons,
            )
        raise RuntimeError("无可用供应商层级")

    # ── 门控与错误处理 ──────────────────────────────────────

    async def _try_gate_tier(
        self,
        tier: VendorTier,
        is_last: bool,
        request_caps: RequestCapabilities,
        canonical_request: Any,
        session_record: Any,
        incompatible_reasons: list[str],
    ) -> str:
        """对单个 tier 执行能力门控和兼容性检查.

        Returns:
            "eligible" — 通过所有门控，可执行请求
            "skip" — 未通过门控，跳过此 tier
        """
        supported, reasons = tier.vendor.supports_request(request_caps)
        if not supported:
            reason_text = ",".join(sorted({r.value for r in reasons}))
            incompatible_reasons.append(f"{tier.name}:{reason_text}")
            logger.info(
                "Tier %s skipped due to incompatible capabilities: %s",
                tier.name,
                reason_text,
            )
            return "skip"

        decision = tier.vendor.make_compatibility_decision(canonical_request)
        if decision.status is CompatibilityStatus.UNSAFE:
            reason_text = ",".join(sorted(decision.unsupported_semantics))
            incompatible_reasons.append(f"{tier.name}:{reason_text}")
            logger.info(
                "Tier %s skipped due to compatibility decision: %s",
                tier.name,
                reason_text,
            )
            return "skip"

        self._session_mgr.apply_compat_context(
            tier=tier,
            canonical_request=canonical_request,
            decision=decision,
            session_record=session_record,
        )

        # 非终端层使用健康检查门控；终端层仅检查 can_execute
        if not is_last:
            if not await tier.can_execute_with_health_check():
                return "skip"
        elif not tier.can_execute():
            return "skip"

        return "eligible"

    async def _handle_token_error(
        self,
        tier: VendorTier,
        exc: TokenAcquireError,
        is_last: bool,
        failed_tier_name: str | None,
    ) -> tuple[str | None, Exception]:
        """处理 TokenAcquireError 的共享逻辑.

        特殊处理：
        - ``INSUFFICIENT_SCOPE`` / ``INVALID_CREDENTIALS`` 属于永久性凭证问题，
          重试无意义，因此**不记录熔断器失败**，避免级联 OPEN 阻塞恢复。
        - 其他临时性错误（网络超时等）正常计入熔断器。
        """
        logger.warning("Tier %s credential error: %s", tier.name, exc)
        is_permanent = exc.kind in (
            TokenErrorKind.INSUFFICIENT_SCOPE,
            TokenErrorKind.INVALID_CREDENTIALS,
        )
        if not is_permanent:
            tier.record_failure()
        else:
            logger.info(
                "Tier %s permanent credential issue (%s), "
                "skipping circuit breaker failure recording",
                tier.name,
                exc.kind.value,
            )
        if exc.needs_reauth and self._reauth_coordinator:
            provider = self._tier_provider_map.get(tier.name)
            if provider:
                await self._reauth_coordinator.request_reauth(provider)
        return tier.name, exc

    async def _handle_http_error(
        self,
        tier: VendorTier,
        exc: Exception,
        is_last: bool,
        failed_tier_name: str | None,
        last_exc: Exception | None,
        *,
        is_stream: bool = False,
        request_body: dict[str, Any] | None = None,
    ) -> tuple[bool, str | None, Exception | None]:
        """处理 HTTP 错误的共享逻辑（流式路径）.

        Returns:
            (should_continue, failed_tier_name, last_exc)
        """
        semantic_rejection = False
        if isinstance(exc, httpx.HTTPStatusError) and exc.response is not None:
            payload = extract_error_payload_from_http_status(exc)
            error = payload.get("error", {}) if isinstance(payload, dict) else {}
            semantic_rejection = is_semantic_rejection(
                status_code=exc.response.status_code,
                error_type=error.get("type") if isinstance(error, dict) else None,
                error_message=error.get("message") if isinstance(error, dict) else None,
            )

            # 补充检测：400 + 有 tool_result + 非结构化错误体 → 格式不兼容
            # （如 Copilot 返回纯文本 "Bad Request\n"）
            # 此类错误不应计入熔断器，因为重试同一请求必然再次失败。
            if (
                not semantic_rejection
                and request_body is not None
                and _is_likely_request_format_error(
                    status_code=exc.response.status_code,
                    error_body_text=exc.response.text[:500]
                    if exc.response.text
                    else None,
                    body=request_body,
                )
            ):
                semantic_rejection = True
                logger.warning(
                    "Tier %s likely format incompatibility (400 + tool_results), "
                    "trying next tier without recording failure",
                    tier.name,
                )

            if semantic_rejection and not is_last:
                if request_body is not None:
                    diagnostic = _build_semantic_rejection_diagnostic(request_body)
                    stream_err_msg = (
                        error.get("message") if isinstance(error, dict) else "N/A"
                    )
                    # 扩展至 500 字符以保留完整字段级诊断信息
                    logger.warning(
                        "Tier %s stream semantic rejection (type=%s, msg=%s)%s, "
                        "trying next tier without recording failure",
                        tier.name,
                        error.get("type") if isinstance(error, dict) else None,
                        stream_err_msg[:500],
                        diagnostic,
                    )
                return True, tier.name, exc

            rl_info = parse_rate_limit_headers(
                exc.response.headers,
                exc.response.status_code,
                exc.response.text[:500] if exc.response.text else None,
            )
            tier.record_failure(
                is_cap_error=rl_info.is_cap_error,
                retry_after_seconds=compute_effective_retry_seconds(rl_info),
                rate_limit_deadline=compute_rate_limit_deadline(rl_info),
            )
        else:
            tier.record_failure()

        return False, tier.name, exc

    @staticmethod
    def _log_failover_transition(
        current_tier: VendorTier,
        exc: Exception,
        tiers: list[VendorTier],
        current_index: int,
    ) -> None:
        """记录 vendor 轮转摘要日志（谁 → 谁，原因）."""
        next_tier = tiers[current_index + 1] if current_index + 1 < len(tiers) else None
        if next_tier is None:
            return

        # 提取错误摘要
        reason = type(exc).__name__
        if isinstance(exc, httpx.HTTPStatusError) and exc.response is not None:
            reason = f"HTTP {exc.response.status_code}"

        logger.info(
            "Failover: %s → %s (reason: %s)",
            current_tier.name,
            next_tier.name,
            reason,
        )

    @staticmethod
    def _is_cap_error(resp: VendorResponse) -> bool:
        """判断是否为订阅用量上限错误."""
        if resp.status_code not in (429, 403):
            return False
        msg = (resp.error_message or "").lower()
        return any(p in msg for p in ("usage cap", "quota", "limit exceeded"))

    @staticmethod
    def _extract_error_message_from_http_status(
        exc: httpx.HTTPStatusError,
    ) -> str | None:
        """从 HTTPStatusError 中提取错误消息文本."""
        if exc.response is None or not exc.response.content:
            return None
        try:
            payload = exc.response.json()
        except Exception:
            return None
        if not isinstance(payload, dict):
            return None
        error = payload.get("error", {})
        if isinstance(error, dict):
            return error.get("message")
        return None
