"""严格排队语义：必须写明一个已存在的队名，而且不会自动建队。

这是产品默认（AppConfig 的默认值 / live_queue/config.json）。其它用例用的
legacy_config 夹具是旧的宽松语义；严格行为专门在这里验证。
"""

from __future__ import annotations

from pathlib import Path

from live_queue.config import AppConfig
from live_queue.hub import QueueHub
from live_queue.store import QueueStore


def _strict_hub(tmp_path: Path) -> QueueHub:
    """用产品默认配置建一个 hub。"""

    return QueueHub(config=AppConfig(), store=QueueStore(tmp_path / "queues.json"))


def _names(hub: QueueHub) -> list[str]:
    """当前队列名（排序后）。"""

    return sorted(str(item["name"]) for item in hub.snapshot()["queues"])


def test_defaults_are_strict() -> None:
    """产品默认：不自动建队、必须写明队名。"""

    rules = AppConfig().queue
    assert rules.auto_create_queue is False
    assert rules.require_explicit_queue is True


def test_join_without_queue_name_is_rejected(tmp_path: Path) -> None:
    """只发关键词、没写队名 -> queue_required（不回落默认队）。"""

    hub = _strict_hub(tmp_path)
    hub.create_queue("排位")

    result = hub.join(user_id="u1", nickname="阿狸", queue="", queue_explicit=False)
    assert result["ok"] is False
    assert result["status"] == "queue_required"
    assert "请写明" in result["message"]
    assert hub.queues_of("u1") == []


def test_join_empty_queue_even_if_explicit_flag(tmp_path: Path) -> None:
    """空队名即使带 explicit 标记也不行（HTTP 默认就是 true）。"""

    hub = _strict_hub(tmp_path)
    hub.create_queue("排位")

    result = hub.join(user_id="u1", queue="", queue_explicit=True)
    assert result["status"] == "queue_required"


def test_join_unknown_queue_is_not_created(tmp_path: Path) -> None:
    """明确写了不存在的队名 -> queue_not_found，且不会建出这个队。"""

    hub = _strict_hub(tmp_path)
    hub.create_queue("排位")

    result = hub.join(user_id="u1", queue="新队", queue_explicit=True)
    assert result["status"] == "queue_not_found"
    assert _names(hub) == ["排位"]


def test_join_inexplicit_unknown_is_rejected(tmp_path: Path) -> None:
    """没写分隔符、队也不存在 -> queue_required（「排队一下」不当队名）。"""

    hub = _strict_hub(tmp_path)
    hub.create_queue("排位")

    result = hub.join(user_id="u1", queue="一下", queue_explicit=False)
    assert result["status"] == "queue_required"
    assert _names(hub) == ["排位"]


def test_join_existing_queue_works(tmp_path: Path) -> None:
    """写明已有队名都能进：显式、无分隔符、别名。"""

    hub = _strict_hub(tmp_path)
    hub.create_queue("排位")

    first = hub.join(user_id="u1", nickname="阿狸", queue="排位", queue_explicit=True)
    assert first["ok"] is True and first["status"] == "joined"

    second = hub.join(user_id="u2", queue="排位", queue_explicit=False)
    assert second["status"] == "joined"

    alias = hub.join(user_id="u3", queue="排位赛", queue_explicit=False)
    assert alias["status"] == "joined"


def test_manual_add_never_creates_default_queue(tmp_path: Path) -> None:
    """控制台手动加人留空 -> 走默认队；默认队不存在就报错，绝不自动建队。"""

    hub = _strict_hub(tmp_path)
    hub.create_queue("排位")

    result = hub.add_manual("小明")
    assert result["status"] == "queue_not_found"
    assert _names(hub) == ["排位"]

    hub.create_queue("默认")
    ok = hub.add_manual("小红")
    assert ok["status"] == "joined"
    assert ok["queue"] == "默认"


def test_create_queue_is_the_only_way_to_add_a_queue(tmp_path: Path) -> None:
    """新建队列只能靠显式创建（面板 / 窗口 / 开队命令）。"""

    hub = _strict_hub(tmp_path)
    hub.create_queue("娱乐")
    assert _names(hub) == ["娱乐"]
