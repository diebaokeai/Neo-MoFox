"""按 QQ 分库（user_scope）的单测。"""

from __future__ import annotations

import json
from pathlib import Path

from plugins.cyrene_neochatter import user_scope


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def test_scope_dir_and_key(tmp_path: Path) -> None:
    """目录按人分开，空值落到 _shared，路径分隔符被清洗。"""

    base = tmp_path / "mem"
    assert Path(user_scope.scope_dir(str(base), "3308747519")) == base / "users" / "3308747519"
    assert Path(user_scope.scope_dir(str(base), "")) == base / "users" / user_scope.SHARED_SCOPE
    assert user_scope.scope_key(None) == user_scope.SHARED_SCOPE
    assert user_scope.scope_key("   ") == user_scope.SHARED_SCOPE
    assert user_scope.scope_key("1792826358") == "1792826358"
    assert user_scope.scope_key("a/b\\c") == "a_b_c"


def test_migrate_legacy_files_moves_and_is_idempotent(tmp_path: Path) -> None:
    """旧版单人文件搬进 owner 目录；再跑一次什么都不做。"""

    base = tmp_path / "strat"
    _write(base / "memory.json", json.dumps({"l0": {"nickname": "离"}}, ensure_ascii=False))
    _write(base / "proactive_state.json", "{}")

    moved = user_scope.migrate_legacy_files(
        str(base), owner_id="3308747519", names=("memory.json", "proactive_state.json")
    )
    assert set(moved) == {"memory.json", "proactive_state.json"}
    assert not (base / "memory.json").exists()
    assert not (base / "proactive_state.json").exists()
    data = json.loads((base / "users" / "3308747519" / "memory.json").read_text(encoding="utf-8"))
    assert data["l0"]["nickname"] == "离"
    assert user_scope.migrate_legacy_files(str(base), owner_id="3308747519", names=("memory.json",)) == {}


def test_migrate_keeps_both_when_target_exists(tmp_path: Path) -> None:
    """目标已存在时不覆盖、不删源文件（留给人工确认）。"""

    base = tmp_path / "strat"
    _write(base / "memory.json", "{}")
    _write(base / "users" / "3308747519" / "memory.json", json.dumps({"l0": {}}, ensure_ascii=False))

    assert user_scope.migrate_legacy_files(str(base), owner_id="3308747519", names=("memory.json",)) == {}
    assert (base / "memory.json").is_file()
    assert (base / "users" / "3308747519" / "memory.json").is_file()


def test_list_user_dirs_and_label(tmp_path: Path) -> None:
    """列出用户目录（跳过 _shared），标签取 l0 昵称。"""

    base = tmp_path / "strat"
    _write(base / "users" / "3308747519" / "memory.json", json.dumps({"l0": {"nickname": "离喜欢叫我"}}, ensure_ascii=False))
    _write(base / "users" / "_shared" / "memory.json", "{}")
    _write(base / "users" / "1792826358" / "memory.json", "{}")

    assert user_scope.list_user_dirs(str(base)) == ["1792826358", "3308747519"]
    assert user_scope.user_label(str(base), "3308747519") == "离喜欢叫我"
    assert user_scope.user_label(str(base), "1792826358") == ""
    assert user_scope.list_user_dirs(str(tmp_path / "nope")) == []


def test_legacy_owner_prefers_explicit_then_whitelist() -> None:
    """归属人：显式配置优先，其次名单第一个。"""

    class General:
        user_ids = ["3308747519", "1792826358"]

    class Memory:
        legacy_owner_id = ""

    class Cfg:
        general = General()
        memory = Memory()

    assert user_scope.legacy_owner(Cfg()) == "3308747519"
    Cfg.memory.legacy_owner_id = "1792826358"
    assert user_scope.legacy_owner(Cfg()) == "1792826358"
    Cfg.general.user_ids = []
    Cfg.memory.legacy_owner_id = ""
    assert user_scope.legacy_owner(Cfg()) == ""


def test_cached_user_id_roundtrip() -> None:
    """同步读取用缓存；清空后回到空串（会落到 _shared）。"""

    user_scope.clear_cache()
    assert user_scope.cached_user_id("s1") == ""
    user_scope._user_id_cache["s1"] = "123"  # 单测直接塞缓存
    assert user_scope.cached_user_id("s1") == "123"
    user_scope.clear_cache()
    assert user_scope.cached_user_id("s1") == ""
