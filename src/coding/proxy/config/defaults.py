"""随包默认值基线（``config.default.yaml``，已 normalize 为 ``$tmpl`` 节点）."""

from __future__ import annotations

import copy
import functools

import yaml

from .loader import _get_default_config_path
from .normalize import normalize_raw, vendor_map


@functools.lru_cache(maxsize=1)
def _parsed_defaults() -> dict:
    path = _get_default_config_path()
    if path is None:
        return {}
    with open(path, encoding="utf-8") as fh:
        raw = yaml.safe_load(fh) or {}
    return normalize_raw(raw)


def packaged_defaults_raw() -> dict:
    """返回包内默认原始文档的深拷贝（调用方可自由修改）."""
    return copy.deepcopy(_parsed_defaults())


def default_vendor_dict(vendor_type: str) -> dict | None:
    found = vendor_map(_parsed_defaults().get("vendors") or []).get(vendor_type)
    return copy.deepcopy(found) if found is not None else None


__all__ = ["packaged_defaults_raw", "default_vendor_dict"]
