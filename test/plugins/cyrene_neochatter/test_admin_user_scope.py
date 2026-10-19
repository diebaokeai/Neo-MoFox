"""面板的按 QQ 分库：?user= 选中谁就看谁的库。"""

from __future__ import annotations

import json
from pathlib import Path

from fastapi.testclient import TestClient

from plugins.cyrene_neochatter.config import CyreneNeoConfig
from plugins.cyrene_neochatter.router.admin_router import NeoChatterMemoryAdminRouter


class _DummyPlugin:
    """只需要一个持有 config 的对象。"""

    plugin_name = "cyrene_neochatter"

    def __init__(self, config: CyreneNeoConfig) -> None:
        self.config = config


def _config(strat_root: Path, dfc_root: Path) -> CyreneNeoConfig:
    cfg = CyreneNeoConfig()
    cfg.memory.data_dir = str(strat_root)
    cfg.memory_store.data_dir = str(dfc_root)
    return cfg


def _client(cfg: CyreneNeoConfig) -> TestClient:
    router = NeoChatterMemoryAdminRouter(_DummyPlugin(cfg))
    router.register_endpoints()
    return TestClient(router.app)


def _seed(strat_root: Path, dfc_root: Path, user_id: str, l1: str) -> None:
    """给某个 QQ 造一份分层记忆 + DFC 记忆。"""

    strat = strat_root / "users" / user_id
    strat.mkdir(parents=True, exist_ok=True)
    (strat / "memory.json").write_text(
        json.dumps(
            {"l0": {"nickname": user_id}, "l1": l1, "l2": [], "relationship": [], "entries": []},
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    dfc = dfc_root / "users" / user_id
    dfc.mkdir(parents=True, exist_ok=True)
    (dfc / "memory.json").write_text(
        json.dumps(
            {"entries": [], "profile": {}, "relationship": [], "worldbook": []},
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )


def test_users_endpoint_lists_both(tmp_path: Path) -> None:
    """用户列表来自两个记忆库的并集。"""

    strat = tmp_path / "strat"
    dfc = tmp_path / "dfc"
    _seed(strat, dfc, "3308747519", "离的备忘")
    _seed(strat, dfc, "1792826358", "新人的备忘")

    with _client(_config(strat, dfc)) as client:
        data = client.get("/api/users").json()

    ids = [item["user_id"] for item in data["users"]]
    assert ids == ["1792826358", "3308747519"]
    assert data["default"] == "1792826358"
    assert all(item["has_strat"] and item["has_dfc"] for item in data["users"])


def test_profile_is_scoped_per_user(tmp_path: Path) -> None:
    """同一次请求带不同的 ?user= 看到不同人的画像。"""

    strat = tmp_path / "strat"
    dfc = tmp_path / "dfc"
    _seed(strat, dfc, "3308747519", "离的备忘")
    _seed(strat, dfc, "1792826358", "新人的备忘")

    with _client(_config(strat, dfc)) as client:
        first = client.get("/api/profile?user=3308747519").json()
        second = client.get("/api/profile?user=1792826358").json()
        default = client.get("/api/profile").json()

    assert first["l1"] == "离的备忘"
    assert second["l1"] == "新人的备忘"
    assert default["l1"] in {"离的备忘", "新人的备忘"}
    assert first["l0"]["nickname"] == "3308747519"


def test_unknown_user_gets_empty_store(tmp_path: Path) -> None:
    """查一个还没数据的人：得到空库，不会串到别人的记忆。"""

    strat = tmp_path / "strat"
    dfc = tmp_path / "dfc"
    _seed(strat, dfc, "3308747519", "离的备忘")

    with _client(_config(strat, dfc)) as client:
        fresh = client.get("/api/profile?user=1792826358").json()
        dfc_overview = client.get("/api/dfc/overview?user=1792826358").json()

    assert fresh["l1"] == ""
    assert fresh["l0"] == {}
    assert dfc_overview["entries"] == 0
    assert dfc_overview["relationship"] == 0
