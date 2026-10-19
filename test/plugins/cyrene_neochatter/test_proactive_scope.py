"""主动状态的按人分库：未知流不落 _shared，异步版才全解析。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from plugins.cyrene_neochatter import user_scope
from plugins.cyrene_neochatter.config import CyreneNeoConfig
from plugins.cyrene_neochatter.plugin import CyreneNeoPlugin


def _plugin(tmp_path: Path) -> tuple[CyreneNeoPlugin, CyreneNeoConfig]:
    """用临时目录构造插件实例。"""

    cfg = CyreneNeoConfig()
    cfg.memory.data_dir = str(tmp_path / "strat")
    return CyreneNeoPlugin(cfg), cfg


def test_sync_save_skips_unknown_streams(tmp_path: Path) -> None:
    """缓存里有映射的按人写；没有映射的跳过，绝不写进 _shared。"""

    instance, cfg = _plugin(tmp_path)
    instance._cnc_proactive = {"s_known": {"epoch": 1}, "s_cold": {"epoch": 2}}
    user_scope.clear_cache()
    user_scope._user_id_cache["s_known"] = "3308747519"

    instance._save_proactive_state(cfg)

    root = Path(cfg.memory.data_dir) / "users"
    assert json.loads((root / "3308747519" / "proactive_state.json").read_text(encoding="utf-8")) == {
        "s_known": {"epoch": 1}
    }
    assert not (root / user_scope.SHARED_SCOPE).exists()


async def test_async_save_partitions_by_resolved_user(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """异步版：解析得到的按人写，真的解析不到才落 _shared。"""

    instance, cfg = _plugin(tmp_path)
    instance._cnc_proactive = {"s_a": {"epoch": 1}, "s_unknown": {"epoch": 2}}

    async def fake_resolve(stream_id: str) -> str:
        return "1792826358" if stream_id == "s_a" else ""

    monkeypatch.setattr(user_scope, "resolve_user_id", fake_resolve)
    await instance._save_proactive_state_async(cfg)

    root = Path(cfg.memory.data_dir) / "users"
    assert json.loads((root / "1792826358" / "proactive_state.json").read_text(encoding="utf-8")) == {
        "s_a": {"epoch": 1}
    }
    assert json.loads(
        (root / user_scope.SHARED_SCOPE / "proactive_state.json").read_text(encoding="utf-8")
    ) == {"s_unknown": {"epoch": 2}}


async def test_load_merges_all_user_files_and_warms_cache(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """加载时合并所有人（含 _shared）的文件，并预热 stream -> QQ 缓存。"""

    instance, cfg = _plugin(tmp_path)
    root = Path(cfg.memory.data_dir) / "users"
    for key, payload in (
        ("3308747519", {"s_a": {"epoch": 1}}),
        ("1792826358", {"s_b": {"epoch": 2}}),
        (user_scope.SHARED_SCOPE, {"s_c": {"epoch": 3}}),
    ):
        path = root / key / "proactive_state.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload), encoding="utf-8")

    warmed: list[str] = []

    async def fake_resolve(stream_id: str) -> str:
        warmed.append(stream_id)
        return "x"

    monkeypatch.setattr(user_scope, "resolve_user_id", fake_resolve)
    await instance._load_proactive_state(cfg)

    assert set(instance._cnc_proactive) == {"s_a", "s_b", "s_c"}
    assert set(warmed) == {"s_a", "s_b", "s_c"}
