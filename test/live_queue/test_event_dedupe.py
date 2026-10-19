"""幂等去重：同一条平台事件（弹幕 msg_id）只处理一次。

这是「bot 与独立桥同时在线也不会重复排队」的应用级保证；平台侧还有单会话锁。
"""

from __future__ import annotations

from pathlib import Path

from live_queue.config import AppConfig
from live_queue.hub import QueueHub, _EventDedupe
from live_queue.store import QueueStore


def _hub(tmp_path: Path) -> QueueHub:
    """产品默认配置的 hub。"""

    return QueueHub(config=AppConfig(), store=QueueStore(tmp_path / "queues.json"))


def _waiting(hub: QueueHub, queue: str) -> int:
    """取某队当前排队人数。"""

    return int(hub.snapshot(queue=queue)["queues"][0]["waiting"])


def test_same_event_id_joins_once(tmp_path: Path) -> None:
    """同一条弹幕重复投递 -> 第二次是 duplicate_event，队列里只有 1 条。"""

    hub = _hub(tmp_path)
    hub.create_queue("排位")

    first = hub.join(
        user_id="u1", nickname="阿狸", platform="bilibili_live",
        queue="排位", queue_explicit=True, event_id="msg-1",
    )
    assert first["status"] == "joined"

    second = hub.join(
        user_id="u1", nickname="阿狸", platform="bilibili_live",
        queue="排位", queue_explicit=True, event_id="msg-1",
    )
    assert second["ok"] is False
    assert second["status"] == "duplicate_event"
    assert _waiting(hub, "排位") == 1


def test_dedupe_is_keyed_by_event_not_user(tmp_path: Path) -> None:
    """即使两个消费者的 user_id 不同，同一 event_id 也只处理一次。"""

    hub = _hub(tmp_path)
    hub.create_queue("排位")

    a = hub.join(user_id="open_A", platform="bilibili_live", queue="排位", event_id="msg-9")
    b = hub.join(user_id="open_B", platform="bilibili_live", queue="排位", event_id="msg-9")
    assert a["status"] == "joined"
    assert b["status"] == "duplicate_event"
    assert _waiting(hub, "排位") == 1


def test_distinct_event_ids_both_apply(tmp_path: Path) -> None:
    """不同事件互不影响。"""

    hub = _hub(tmp_path)
    hub.create_queue("排位")
    assert hub.join(user_id="u1", platform="bilibili_live", queue="排位", event_id="m1")["status"] == "joined"
    assert hub.join(user_id="u2", platform="bilibili_live", queue="排位", event_id="m2")["status"] == "joined"
    assert _waiting(hub, "排位") == 2


def test_same_user_same_queue_is_already_in_without_event_id(tmp_path: Path) -> None:
    """没有 event_id 时的第二道保险：同一用户同一队列因 duplicate_policy 被拒。"""

    hub = _hub(tmp_path)
    hub.create_queue("排位")
    hub.join(user_id="u1", platform="bilibili_live", queue="排位")
    again = hub.join(user_id="u1", platform="bilibili_live", queue="排位")
    assert again["status"] == "already_in"
    assert _waiting(hub, "排位") == 1


def test_leave_is_deduplicated_too(tmp_path: Path) -> None:
    """退队同样幂等。"""

    hub = _hub(tmp_path)
    hub.create_queue("排位")
    hub.join(user_id="u1", platform="bilibili_live", queue="排位", event_id="j1")

    first = hub.leave(user_id="u1", queue="排位", event_id="l1")
    assert first["status"] == "left"
    second = hub.leave(user_id="u1", queue="排位", event_id="l1")
    assert second["status"] == "duplicate_event"


def test_dedupe_expires_after_ttl() -> None:
    """超过 TTL 之后同一事件可以再次处理（缓存不会永久占着）。"""

    cache = _EventDedupe(ttl=60.0, capacity=100)
    assert cache.seen("join:bilibili_live:m1", now=1000.0) is False
    assert cache.seen("join:bilibili_live:m1", now=1030.0) is True
    assert cache.seen("join:bilibili_live:m1", now=1200.0) is False


def test_dedupe_capacity_is_bounded() -> None:
    """容量上限生效：老记录被淘汰，不会无限增长。"""

    cache = _EventDedupe(ttl=3600.0, capacity=16)
    for index in range(40):
        cache.seen(f"e{index}", now=1000.0 + index)
    assert len(cache) <= 16
