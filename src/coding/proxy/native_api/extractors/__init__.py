"""三家 provider 的 usage 抽取器入口（导入即触发 ``@register_extractor`` 注册）.

模块导入顺序无关；只要在 ``NativeProxyHandler`` 构造前完成导入即可让注册表就绪。
"""

from __future__ import annotations

from . import anthropic as _anthropic  # noqa: F401  — 触发注册
from . import gemini as _gemini  # noqa: F401
from . import openai as _openai  # noqa: F401

__all__: list[str] = []
