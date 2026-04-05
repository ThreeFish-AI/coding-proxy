"""coding-proxy: Claude Code 多后端智能代理."""

from __future__ import annotations


def __get_version() -> str:
    """从 pyproject.toml 或包元数据动态读取版本号（SSOT）.

    优先级:
    1. ``importlib.metadata`` — pip / wheel 安装后的标准元数据源
    2. ``tomllib`` 解析 ``pyproject.toml`` — 开发模式 (uv run) 回退
    """
    try:
        from importlib.metadata import version as _meta_version
        return _meta_version("coding-proxy")
    except Exception:
        pass

    import tomllib
    from pathlib import Path as _Path

    _toml_path = _Path(__file__).resolve().parents[2] / "pyproject.toml"
    with open(_toml_path, "rb") as _f:
        _data = tomllib.load(_f)
    return _data["project"]["version"]


__version__: str = __get_version()
