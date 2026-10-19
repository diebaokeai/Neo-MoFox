"""live_queue 服务层（QueueHub）的单测。

重点覆盖三件事：队列语义、持久化、以及**线程安全**（窗口线程与 HTTP 线程会并发访问）。
"""

from __future__ import annotations

import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from live_queue.config import AppConfig
from live_queue.hub import QueueHub, _aliases_of, _policy_of
from live_queue.store import QueueStore


def _join(hub: QueueHub, user_id: str, queue: str = "", nickname: str = "") -> dict:
    """便捷入队。

    Args:
        hub: 服务层。
        user_id: 用户 ID。
        queue: 队名。
        nickname: 昵称。

    Returns:
        结果 dict。
    """

    return hub.join(
        user_id=user_id,
        nickname=nickname or user_id,
        platform="bilibili_live",
        queue=queue,
        queue_explicit=bool(queue),
    )


# ── 基础操作 ──────────────────────────────────────────


def test_join_and_call_next(hub: QueueHub) -> None:
    """入队 → 叫号 → 结束。"""

    first = _join(hub, "u1", "排位", "阿狸")
    second = _join(hub, "u2", "排位", "小满")

    assert first["ok"] is True
    assert first["status"] == "joined"
    assert first["position"] == 1
    assert first["ticket"] == 1
    assert second["position"] == 2

    called = hub.call_next("排位")
    assert called["ok"] is True
    assert called["entry"]["nickname"] == "阿狸"
    assert "阿狸" in called["message"]

    finished = hub.finish_current("排位")
    assert finished["ok"] is True

    snapshot = hub.snapshot(queue="排位")
    assert snapshot["queues"][0]["waiting"] == 1
    assert snapshot["queues"][0]["current"] is None


def test_duplicate_join_reports_already_in(hub: QueueHub) -> None:
    """默认策略下重复入队被拒。"""

    _join(hub, "u1", "排位")
    again = _join(hub, "u1", "排位")

    assert again["ok"] is True
    assert again["status"] == "already_in"
    assert hub.snapshot(queue="排位")["queues"][0]["waiting"] == 1


def test_duplicate_policy_from_config(config: AppConfig, store: QueueStore) -> None:
    """配置里的重复策略要真的生效。"""

    config.queue.duplicate_policy = "move_to_end"
    hub = QueueHub(config=config, store=store)
    hub.start()
    try:
        _join(hub, "u1", "排位")
        _join(hub, "u2", "排位")
        moved = _join(hub, "u1", "排位")

        assert moved["status"] == "moved"
        assert moved["position"] == 2
    finally:
        hub.stop()


def test_invalid_duplicate_policy_degrades(config: AppConfig, store: QueueStore) -> None:
    """策略写错时退化成 reject（不能因为一个错配置就放开重复排队）。"""

    config.queue.duplicate_policy = "随便写的"
    hub = QueueHub(config=config, store=store)
    hub.start()
    try:
        _join(hub, "u1", "排位")
        assert _join(hub, "u1", "排位")["status"] == "already_in"
    finally:
        hub.stop()


def test_leave_specific_and_all(hub: QueueHub) -> None:
    """退队：指定队名 / 退出全部。"""

    _join(hub, "u1", "排位")
    _join(hub, "u1", "娱乐")

    one = hub.leave(user_id="u1", queue="排位", queue_explicit=True)
    assert one["ok"] is True
    assert one["queues"] == ["排位"]
    assert hub.queues_of("u1") == ["娱乐"]

    rest = hub.leave(user_id="u1")
    assert rest["queues"] == ["娱乐"]
    assert hub.queues_of("u1") == []


def test_leave_when_not_queued(hub: QueueHub) -> None:
    """没排队时退队给出 not_queued，不算失败。"""

    result = hub.leave(user_id="u404")
    assert result["ok"] is False
    assert result["status"] == "not_queued"
    assert "不在任何队列" in result["message"]


def test_leave_unknown_queue_does_not_touch_default(hub: QueueHub) -> None:
    """指定了不存在的队名时，不能顺手把人从默认队里删掉。"""

    _join(hub, "u1")
    result = hub.leave(user_id="u1", queue="幽灵队", queue_explicit=True)
    assert result["ok"] is False
    assert hub.queues_of("u1") == ["默认"]


def test_requeue_current(hub: QueueHub) -> None:
    """放回队首。"""

    _join(hub, "u1", "排位")
    _join(hub, "u2", "排位")
    hub.call_next("排位")

    back = hub.requeue_current("排位")
    assert back["ok"] is True
    assert hub.snapshot(queue="排位")["queues"][0]["entries"][0]["nickname"] == "u1"


def test_clear_and_pause_and_capacity(hub: QueueHub) -> None:
    """清空、暂停 / 恢复、改上限。"""

    _join(hub, "u1", "排位")
    _join(hub, "u2", "排位")

    assert hub.set_paused("排位", paused=True)["ok"] is True
    assert hub.snapshot(queue="排位")["queues"][0]["paused"] is True
    assert _join(hub, "u3", "排位")["status"] == "queue_paused"

    assert hub.set_paused("排位", paused=False)["ok"] is True
    assert hub.set_capacity("排位", 7)["ok"] is True
    assert hub.snapshot(queue="排位")["queues"][0]["capacity"] == 7

    cleared = hub.clear("排位")
    assert cleared["ok"] is True
    assert cleared["removed"] == 2


def test_create_and_delete_queue(hub: QueueHub) -> None:
    """建队与删队。"""

    created = hub.create_queue("排位", display_name="排位赛", capacity=5)
    assert created["ok"] is True
    assert created["status"] == "created"

    again = hub.create_queue("排位")
    assert again["status"] == "exists"

    assert hub.create_queue("带 空格")["status"] == "invalid_name"
    assert hub.delete_queue("排位")["ok"] is True
    assert hub.delete_queue("排位")["ok"] is False


def test_create_queue_respects_max_queues(config: AppConfig, store: QueueStore) -> None:
    """队列数量上限生效。"""

    config.queue.max_queues = 1
    hub = QueueHub(config=config, store=store)
    hub.start()
    try:
        assert hub.create_queue("一队")["ok"] is True
        assert hub.create_queue("二队")["status"] == "too_many_queues"
    finally:
        hub.stop()


def test_remove_and_move_front(hub: QueueHub) -> None:
    """移除与置顶。"""

    for index in range(3):
        _join(hub, f"u{index}", "排位")

    assert hub.move_front("u2", "排位")["ok"] is True
    assert hub.snapshot(queue="排位")["queues"][0]["entries"][0]["user_id"] == "u2"

    assert hub.remove_user("u0", "排位")["ok"] is True
    assert hub.remove_user("u404", "排位")["ok"] is False
    assert hub.move_front("u404", "排位")["ok"] is False


def test_add_manual_generates_unique_id(hub: QueueHub) -> None:
    """手动加人用占位 ID，且互不相同。"""

    first = hub.add_manual("手动一号", "排位")
    second = hub.add_manual("手动二号", "排位")

    assert first["ok"] and second["ok"]
    ids = {
        entry["user_id"] for entry in hub.snapshot(queue="排位")["queues"][0]["entries"]
    }
    assert len(ids) == 2
    assert all(user_id.startswith("manual:") for user_id in ids)


def test_operations_on_unknown_queue(hub: QueueHub) -> None:
    """对不存在的队列操作一律返回 ok=False，不抛异常。"""

    for result in (
        hub.call_next("幽灵"),
        hub.finish_current("幽灵"),
        hub.requeue_current("幽灵"),
        hub.clear("幽灵"),
        hub.set_paused("幽灵", paused=True),
        hub.set_capacity("幽灵", 3),
        hub.remove_user("u1", "幽灵"),
        hub.move_front("u1", "幽灵"),
    ):
        assert result["ok"] is False
        assert result["message"]


# ── 队名解析 ──────────────────────────────────────────


def test_resolve_queue_with_alias(hub: QueueHub) -> None:
    """别名映射到正式队名。"""

    assert hub.resolve_queue("排位赛", explicit=True) == "排位"
    assert hub.resolve_alias("上分") == "排位"


def test_resolve_queue_explicit_creates_new(hub: QueueHub) -> None:
    """明确写了队名时允许用新队。"""

    assert hub.resolve_queue("新队", explicit=True) == "新队"


def test_resolve_queue_inexplicit_unknown_falls_back(hub: QueueHub) -> None:
    """没写分隔符、队也不存在时回到默认队（「排队一下」不该建出「一下」队）。"""

    assert hub.resolve_queue("一下", explicit=False) == "默认"
    assert hub.snapshot()["queues"] == []


def test_resolve_queue_inexplicit_existing_is_accepted(hub: QueueHub) -> None:
    """没写分隔符但正好是已有队名时仍然认（支持「排队排位」）。"""

    hub.create_queue("排位")
    assert hub.resolve_queue("排位", explicit=False) == "排位"


def test_resolve_queue_empty_uses_default(hub: QueueHub) -> None:
    """空队名落到默认队。"""

    assert hub.resolve_queue("", explicit=False) == "默认"
    assert hub.resolve_queue("   ", explicit=True) == "默认"


def test_resolve_queue_invalid_name(hub: QueueHub) -> None:
    """非法队名返回空串。"""

    assert hub.resolve_queue("带 空格", explicit=True) == ""
    assert hub.join(user_id="u1", queue="带 空格")["status"] == "invalid_name"


def test_aliases_of_parsing() -> None:
    """别名解析支持多种分隔符并跳过残缺项。"""

    assert _aliases_of(["a=b", " c : d ", "坏掉的", "=x", "y="]) == {
        "a": "b",
        "c": "d",
    }


def test_policy_of_fallback() -> None:
    """策略转换的兜底。"""

    assert _policy_of("allow").value == "allow"
    assert _policy_of("不认识").value == "reject"


# ── 快照与健康 ────────────────────────────────────────


def test_snapshot_shape(hub: QueueHub) -> None:
    """快照包含前端需要的全部字段。"""

    _join(hub, "u1", "排位", "阿狸")
    _join(hub, "u2", "排位", "小满")
    hub.call_next("排位")

    snapshot = hub.snapshot()
    assert snapshot["ok"] is True
    assert snapshot["total_waiting"] == 1
    assert isinstance(snapshot["view"], dict)
    assert snapshot["view"]["max_rows"] == 8

    queue = snapshot["queues"][0]
    assert queue["name"] == "排位"
    assert queue["current"]["nickname"] == "阿狸"
    assert queue["entries"][0]["position"] == 1
    assert queue["entries"][0]["waiting_seconds"] >= 0


def test_snapshot_limit_and_filter(hub: QueueHub) -> None:
    """截断与过滤。"""

    for index in range(5):
        _join(hub, f"u{index}", "排位")
    _join(hub, "x", "娱乐")

    limited = hub.snapshot(queue="排位", limit=2)
    assert [item["name"] for item in limited["queues"]] == ["排位"]
    assert len(limited["queues"][0]["entries"]) == 2
    assert limited["total_waiting"] == 6  # 过滤只影响返回内容


def test_snapshot_orders_by_overlay_config(config: AppConfig, store: QueueStore) -> None:
    """按配置里的队列顺序输出。"""

    config.overlay.queues = ["娱乐", "排位"]
    hub = QueueHub(config=config, store=store)
    hub.start()
    try:
        hub.create_queue("排位")
        hub.create_queue("娱乐")
        assert [item["name"] for item in hub.snapshot()["queues"]] == ["娱乐", "排位"]
    finally:
        hub.stop()


def test_overlay_config(hub: QueueHub) -> None:
    """叠加页配置接口。"""

    config = hub.overlay_config()
    assert config["overlay"]["max_rows"] == 8
    assert config["default_queue"] == "默认"
    assert config["queues"] == []


def test_health(hub: QueueHub) -> None:
    """健康检查字段齐全（机器人靠它判断就绪）。"""

    health = hub.health()
    assert health["ok"] is True
    assert health["app"] == "live_queue"
    assert health["queue_count"] == 0
    assert health["uptime"] >= 0


def test_queue_names_and_queues_of(hub: QueueHub) -> None:
    """队列名列表与用户所在队列。"""

    _join(hub, "u1", "排位")
    _join(hub, "u1", "娱乐")

    assert hub.queue_names() == ["排位", "娱乐"]
    assert hub.queues_of("u1") == ["排位", "娱乐"]
    assert hub.queues_of("u404") == []


# ── 变更通知与持久化 ──────────────────────────────────


def test_revision_increments_and_listeners_fire(hub: QueueHub) -> None:
    """每次变更推进版本号并通知监听者；注销后不再收到通知。"""

    calls: list[int] = []

    def listener() -> None:
        calls.append(1)

    hub.add_listener(listener)
    start = hub.revision

    _join(hub, "u1", "排位")
    assert hub.revision > start
    assert calls

    hub.remove_listener(listener)
    before = len(calls)
    _join(hub, "u2", "排位")
    assert len(calls) == before


def test_remove_unknown_listener_is_safe(hub: QueueHub) -> None:
    """注销一个没注册过的回调不应该出错。"""

    hub.remove_listener(lambda: None)
    assert _join(hub, "u1", "排位")["ok"] is True


def test_listener_exception_does_not_break_operation(hub: QueueHub) -> None:
    """监听者抛异常不能影响队列操作本身。"""

    def boom() -> None:
        raise RuntimeError("监听器炸了")

    hub.add_listener(boom)
    result = _join(hub, "u1", "排位")
    assert result["ok"] is True


def test_dirty_flag_and_flush(hub: QueueHub) -> None:
    """落盘：没有改动不写，有改动才写。"""

    assert hub.has_pending_changes is False
    assert hub.flush() is False

    _join(hub, "u1", "排位")
    assert hub.has_pending_changes is True
    assert hub.flush() is True
    assert hub.has_pending_changes is False
    assert hub.store.path.exists()


def test_force_flush_writes_even_without_changes(hub: QueueHub) -> None:
    """强制落盘。"""

    assert hub.flush(force=True) is True
    payload = json.loads(hub.store.path.read_text(encoding="utf-8"))
    assert "queues" in payload


def test_flush_reports_failure_and_keeps_dirty(
    config: AppConfig, tmp_path: Path, logger: object
) -> None:
    """落盘失败要返回 False 并且保持脏标记，方便下一轮重试。"""

    blocker = tmp_path / "blocker"
    blocker.write_text("我是文件", encoding="utf-8")
    hub = QueueHub(config=config, store=QueueStore(blocker / "queues.json"), logger=logger)
    _join(hub, "u1", "排位")

    assert hub.flush() is False
    assert hub.has_pending_changes is True


def test_persistence_round_trip(tmp_path: Path, legacy_config: AppConfig) -> None:
    """重启（新建 hub）后能恢复状态。"""

    store = QueueStore(tmp_path / "queues.json")
    config = legacy_config

    first = QueueHub(config=config, store=store)
    first.start()
    _join(first, "u1", "排位", "阿狸")
    _join(first, "u2", "排位", "小满")
    first.call_next("排位")
    first.stop()

    second = QueueHub(config=config, store=store)
    second.start()
    try:
        snapshot = second.snapshot(queue="排位")
        queue = snapshot["queues"][0]
        assert queue["current"]["nickname"] == "阿狸"
        assert [item["nickname"] for item in queue["entries"]] == ["小满"]
    finally:
        second.stop()


def test_start_survives_corrupt_state(tmp_path: Path, legacy_config: AppConfig) -> None:
    """状态文件损坏时要能起来（按空队列开始），而不是拒绝启动。"""

    path = tmp_path / "queues.json"
    path.write_text("{ 坏掉的", encoding="utf-8")

    hub = QueueHub(config=legacy_config, store=QueueStore(path))
    hub.start()
    try:
        assert hub.snapshot()["queues"] == []
    finally:
        hub.stop()


def test_stop_forces_final_flush(config: AppConfig, store: QueueStore) -> None:
    """退出前必须落盘。"""

    hub = QueueHub(config=config, store=store)
    hub.start()
    _join(hub, "u1", "排位")
    hub.stop()

    payload = json.loads(store.path.read_text(encoding="utf-8"))
    assert payload["queues"][0]["entries"][0]["user_id"] == "u1"


# ── 维护线程 ──────────────────────────────────────────


def test_maintenance_purges_expired_and_flushes(config: AppConfig, store: QueueStore) -> None:
    """维护线程要清理超时条目，并把脏数据落盘。

    真实 sleep ~1.5 秒：间隔下限是 1 秒（防止配置写小了把磁盘刷爆）。
    """

    config.queue.entry_timeout = 0.05
    hub = QueueHub(config=config, store=store)
    hub.start()
    try:
        _join(hub, "u1", "排位")
        assert hub.snapshot(queue="排位")["queues"][0]["waiting"] == 1

        deadline = time.time() + 4.0
        while time.time() < deadline:
            if hub.snapshot(queue="排位")["queues"][0]["waiting"] == 0:
                break
            time.sleep(0.2)

        assert hub.snapshot(queue="排位")["queues"][0]["waiting"] == 0
        assert store.path.exists()
    finally:
        hub.stop()


def test_no_purge_when_timeout_disabled(config: AppConfig, store: QueueStore) -> None:
    """entry_timeout=0 表示永不超时。"""

    config.queue.entry_timeout = 0.0
    hub = QueueHub(config=config, store=store)
    hub.start()
    try:
        _join(hub, "u1", "排位")
        time.sleep(1.2)
        assert hub.snapshot(queue="排位")["queues"][0]["waiting"] == 1
    finally:
        hub.stop()


# ── 线程安全 ──────────────────────────────────────────


def test_concurrent_joins_are_not_lost(hub: QueueHub) -> None:
    """8 个线程各加 25 人，一个人都不能丢。"""

    def worker(index: int) -> None:
        for item in range(25):
            _join(hub, f"u{index}-{item}", "并发队")

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(worker, range(8)))

    queue = hub.snapshot(queue="并发队")["queues"][0]
    assert queue["waiting"] == 200
    # 序号不能重复（重复说明发号时有竞态）
    tickets = [entry["ticket"] for entry in queue["entries"]]
    assert len(set(tickets)) == 200


def test_concurrent_reads_during_writes(hub: QueueHub) -> None:
    """读写混跑不应炸（快照在锁内构建，不会看到半截状态）。"""

    stop = threading.Event()
    errors: list[Exception] = []

    def reader() -> None:
        while not stop.is_set():
            try:
                snapshot = hub.snapshot()
                assert isinstance(snapshot["queues"], list)
            except Exception as exc:  # noqa: BLE001 - 测试里就是要把异常收集起来
                errors.append(exc)
                return

    def writer() -> None:
        for index in range(200):
            _join(hub, f"w{index}", "混合队")
            if index % 20 == 0:
                hub.call_next("混合队")

    readers = [threading.Thread(target=reader, daemon=True) for _ in range(3)]
    for thread in readers:
        thread.start()
    writer()
    stop.set()
    for thread in readers:
        thread.join(timeout=2)

    assert errors == []


def test_concurrent_call_next_never_double_calls(hub: QueueHub) -> None:
    """并发叫号不能把同一个人叫两次。"""

    for index in range(10):
        _join(hub, f"u{index}", "抢号队")

    with ThreadPoolExecutor(max_workers=10) as pool:
        results = list(pool.map(lambda _i: hub.call_next("抢号队"), range(10)))

    called_ids = [item["entry"]["user_id"] for item in results if item["ok"]]
    assert len(called_ids) == len(set(called_ids)) == 10


def test_health_is_thread_safe(hub: QueueHub) -> None:
    """健康检查在并发下也能拿到一致的数字。"""

    for index in range(50):
        _join(hub, f"u{index}", "统计队")

    with ThreadPoolExecutor(max_workers=4) as pool:
        infos = list(pool.map(lambda _i: hub.health(), range(20)))

    assert {info["total_waiting"] for info in infos} == {50}
