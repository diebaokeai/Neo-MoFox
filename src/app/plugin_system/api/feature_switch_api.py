"""跨插件功能开关（简单持久化的小型共享状态）。

用于把「某个插件的能力是否开启」暴露给其它插件 / WebUI 控制，
例如 ASR 监听开关。状态保存在 data/feature_switches.json，重启后保留。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

_STORE_PATH = Path("data/feature_switches.json")
_cache: dict[str, Any] | None = None


def _load() -> dict[str, Any]:
    global _cache
    if _cache is None:
        try:
            raw = _STORE_PATH.read_text(encoding="utf-8") if _STORE_PATH.is_file() else "{}"
            data = json.loads(raw)
        except Exception:
            data = {}
        _cache = data if isinstance(data, dict) else {}
    return _cache


def _save() -> None:
    try:
        _STORE_PATH.parent.mkdir(parents=True, exist_ok=True)
        _STORE_PATH.write_text(
            json.dumps(_cache or {}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except Exception:
        pass


def get_switch(name: str, default: bool = False) -> bool:
    """读取开关值（不存在时返回 default）。"""
    return bool(_load().get(str(name), default))


def set_switch(name: str, value: bool) -> bool:
    """写入开关值并落盘，返回新值。"""
    _load()[str(name)] = bool(value)
    _save()
    return bool(value)


def all_switches() -> dict[str, bool]:
    """返回全部开关快照。"""
    return {str(k): bool(v) for k, v in _load().items()}
