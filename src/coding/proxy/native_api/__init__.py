"""Native API 透传子包.

提供 OpenAI / Gemini / Anthropic 原生 LLM API 的全量 catch-all 透传能力，
与既有 ``/v1/messages`` Claude Code 链路正交共存。

核心模块：

- :mod:`.config`         — ``NativeApiConfig`` / ``NativeProviderConfig`` 配置模型
- :mod:`.operation`      — ``OperationClassifier`` 路径→规范化操作名分类器
- :mod:`.usage_registry` — ``NativeUsageExtractor`` 注册表 + 通用兜底扫描
- :mod:`.extractors`     — 三家 provider 各自的 usage 抽取器
- :mod:`.handler`        — ``NativeProxyHandler`` httpx 透传核心
- :mod:`.routes`         — ``register_native_api_routes`` FastAPI 集成

.. note::
    本模块顶层仅导入**纯配置**（不触发 httpx / routing 依赖），以避免与
    ``config/schema.py`` 的循环导入。``NativeProxyHandler`` / ``register_native_api_routes``
    等需通过各自子模块按需引入。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from .config import NativeApiConfig, NativeProviderConfig

if TYPE_CHECKING:  # 仅类型标注使用，运行时不触发循环导入
    from .handler import NativeProxyHandler
    from .operation import OperationClassifier
    from .routes import register_native_api_routes
    from .usage_registry import (
        ExtractionResult,
        StreamingUsageAccumulator,
        extract_usage,
        register_extractor,
    )


def __getattr__(name: str):  # noqa: D401 - pep 562 lazy export
    """PEP 562 lazy re-export — 运行时按需加载重模块.

    首次访问 ``native_api.NativeProxyHandler`` 才触发 handler 模块初始化，
    避免 ``config/schema.py`` 加载时连锁引入 httpx/routing 依赖导致循环。
    """
    if name == "NativeProxyHandler":
        from .handler import NativeProxyHandler as _cls

        return _cls
    if name == "OperationClassifier":
        from .operation import OperationClassifier as _cls

        return _cls
    if name == "register_native_api_routes":
        from .routes import register_native_api_routes as _fn

        return _fn
    if name in {
        "ExtractionResult",
        "StreamingUsageAccumulator",
        "extract_usage",
        "register_extractor",
    }:
        import importlib

        mod = importlib.import_module(".usage_registry", __name__)
        # 首次通过 public API 获取 usage 注册表相关符号时，同步触发三家
        # provider 的抽取器模块加载（side-effect import 触发
        # @register_extractor 装饰器），幂等且无循环依赖。
        importlib.import_module(".extractors", __name__)
        return getattr(mod, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = [
    "NativeApiConfig",
    "NativeProviderConfig",
    "NativeProxyHandler",
    "OperationClassifier",
    "register_native_api_routes",
    "ExtractionResult",
    "StreamingUsageAccumulator",
    "extract_usage",
    "register_extractor",
]
