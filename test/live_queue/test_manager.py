"""live_queue 队列内核的单测：数据模型 + 多命名队列调度器。

覆盖 ``live_queue/core`` 的全部公开行为，全部为纯内存测试，
不依赖 mofox 框架、不碰 IO，因此可以快速反复跑。
"""

from __future__ import annotations

from typing import Any

import pytest

from live_queue.core.manager import QueueManager
from live_queue.core.models import (
    DuplicatePolicy,
    JoinStatus,
    QueueEntry,
    QueueState,
    format_duration,
)


@pytest.fixture
def manager(clock: Any) -> QueueManager:
    """提供一个默认配置的管理器。

    Args:
        clock: ``conftest`` 提供的假时钟。

    Returns:
        使用假时钟的管理器。
    """

    return QueueManager(auto_create_queue=True, clock=clock)


def _join(
    manager: QueueManager,
    user_id: str,
    queue: str = "默认",
    nickname: str = "",
    guild: str = "",
) -> object:
    """便捷入队封装，减少测试噪音。

    Args:
        manager: 待操作的管理器。
        user_id: 用户 ID。
        queue: 队名。
        nickname: 昵称。
        guild: 归属标识（直播间号）。

    Returns:
        :class:`JoinResult`。
    """

    return manager.join(
        queue,
        user_id=user_id,
        nickname=nickname or user_id,
        platform="bilibili_live",
        guild=guild,
    )


# ── 数据模型 ──────────────────────────────────────────


@pytest.mark.parametrize(
    ("seconds", "expected"),
    [
        (0, "0秒"),
        (45, "45秒"),
        (60, "1分"),
        (95, "1分35秒"),
        (3600, "1小时"),
        (3660, "1小时1分"),
        (7385, "2小时3分"),
        (-10, "0秒"),
    ],
)
def test_format_duration(seconds: float, expected: str) -> None:
    """时长格式化应覆盖秒/分/时与负数边界。"""

    assert format_duration(seconds) == expected


def test_queue_entry_display_name_falls_back_to_user_id() -> None:
    """昵称为空时用用户 ID 前 6 位兜底，避免叠加页出现空白。"""

    entry = QueueEntry(user_id="abcdef123456", nickname="", joined_at=0.0)
    assert entry.display_name == "用户abcdef"

    anonymous = QueueEntry(user_id="", nickname="", joined_at=0.0)
    assert anonymous.display_name == "用户未知"


def test_queue_entry_round_trip() -> None:
    """条目序列化后应能原样还原。"""

    entry = QueueEntry(
        user_id="u1",
        nickname="阿狸",
        joined_at=123.5,
        platform="bilibili_live",
        ticket=7,
        extra={"guard_level": 1},
    )
    restored = QueueEntry.from_dict(entry.to_dict())
    assert restored == entry


def test_queue_entry_from_dict_tolerates_broken_payload() -> None:
    """残缺的落盘数据不应让还原过程抛异常。"""

    restored = QueueEntry.from_dict({"user_id": "u1", "extra": "not-a-dict"})
    assert restored.user_id == "u1"
    assert restored.nickname == ""
    assert restored.joined_at == 0.0
    assert restored.extra == {}


def test_queue_state_round_trip_keeps_order() -> None:
    """队列序列化必须保留条目顺序与 current。"""

    first = QueueEntry(user_id="u1", nickname="A", joined_at=1.0, ticket=1)
    second = QueueEntry(user_id="u2", nickname="B", joined_at=2.0, ticket=2)
    state = QueueState(
        name="排位",
        display_name="排位赛",
        entries=[first, second],
        current=QueueEntry(user_id="u0", nickname="当前", joined_at=0.5, ticket=0),
        paused=True,
        capacity=5,
        next_ticket=3,
        served_count=9,
        created_at=1.0,
        updated_at=2.0,
    )

    restored = QueueState.from_dict(state.to_dict())

    assert restored.title == "排位赛"
    assert [entry.user_id for entry in restored.entries] == ["u1", "u2"]
    assert restored.current is not None
    assert restored.current.nickname == "当前"
    assert restored.paused is True
    assert restored.capacity == 5
    assert restored.next_ticket == 3
    assert restored.served_count == 9
    assert restored.size == 2


def test_queue_state_title_falls_back_to_name() -> None:
    """展示名留空时回退到队名。"""

    assert QueueState(name="默认").title == "默认"


def test_queue_state_from_dict_ignores_garbage_entries() -> None:
    """落盘结构异常时的条目应被丢弃而不是崩溃。"""

    restored = QueueState.from_dict({"name": "x", "entries": ["oops", 3, {"user_id": "u"}]})
    assert [entry.user_id for entry in restored.entries] == ["u"]


def test_position_of_returns_zero_for_outsider() -> None:
    """不在队列中的用户位置为 0。"""

    state = QueueState(name="x", entries=[QueueEntry(user_id="u1", nickname="A", joined_at=0.0)])
    assert state.position_of("u1") == 1
    assert state.position_of("u9") == 0


# ── 入队 ──────────────────────────────────────────────


def test_join_appends_and_assigns_increasing_tickets(manager: QueueManager) -> None:
    """入队应追加到队尾并按 1、2、3 递增发号。"""

    first = _join(manager, "u1")
    second = _join(manager, "u2")

    assert first.status is JoinStatus.JOINED
    assert first.position == 1
    assert first.entry is not None and first.entry.ticket == 1
    assert second.position == 2
    assert second.entry is not None and second.entry.ticket == 2

    queue = manager.get_queue("默认")
    assert queue is not None
    assert [entry.user_id for entry in queue.entries] == ["u1", "u2"]


def test_join_records_platform_and_extra(manager: QueueManager) -> None:
    """平台标识与附加信息应被完整记录，供叠加页和排查使用。"""

    result = manager.join(
        "默认",
        user_id="u1",
        nickname="阿狸",
        platform="bilibili_live",
        extra={"guard_level": 2},
    )
    assert result.entry is not None
    assert result.entry.platform == "bilibili_live"
    assert result.entry.extra == {"guard_level": 2}


def test_join_without_name_uses_default_queue(manager: QueueManager) -> None:
    """空队名解析成默认队。"""

    assert manager.resolve_name("") == "默认"
    assert manager.resolve_name("   ") == "默认"
    assert manager.resolve_name(None) == "默认"


def test_resolve_name_applies_alias() -> None:
    """别名应映射到正式队名。"""

    manager = QueueManager(auto_create_queue=True, aliases={"排位赛": "排位"})
    assert manager.resolve_name("排位赛") == "排位"
    assert manager.resolve_name("其他") == "其他"


@pytest.mark.parametrize("raw", ["带 空格", "a" * 17])
def test_resolve_name_rejects_invalid(raw: str) -> None:
    """含空白或超长的队名不合法，解析成空串。"""

    manager = QueueManager(auto_create_queue=True)
    assert manager.resolve_name(raw) == ""


def test_join_reports_invalid_name(manager: QueueManager) -> None:
    """非法队名直接返回 INVALID_NAME，不建队。"""

    result = _join(manager, "u1", queue="带 空格")
    assert result.status is JoinStatus.INVALID_NAME
    assert manager.queues() == []


def test_join_is_scoped_per_queue(manager: QueueManager) -> None:
    """多命名队列互不干扰。"""

    _join(manager, "u1", queue="排位")
    _join(manager, "u2", queue="娱乐")

    assert [queue.name for queue in manager.queues()] == ["排位", "娱乐"]
    assert manager.get_queue("排位").position_of("u1") == 1  # type: ignore[union-attr]
    assert manager.get_queue("排位").position_of("u2") == 0  # type: ignore[union-attr]


def test_duplicate_join_rejected_by_default(manager: QueueManager) -> None:
    """默认策略下同一用户重复入队被拒绝且位置不变。"""

    _join(manager, "u1")
    _join(manager, "u2")
    again = _join(manager, "u1")

    assert again.status is JoinStatus.ALREADY_IN
    assert again.position == 1
    assert manager.get_queue("默认").size == 2  # type: ignore[union-attr]


def test_duplicate_join_moves_to_end_when_configured() -> None:
    """move_to_end 策略应把重复用户挪到队尾并重新发号。"""

    manager = QueueManager(auto_create_queue=True, duplicate_policy=DuplicatePolicy.MOVE_TO_END)
    _join(manager, "u1")
    _join(manager, "u2")
    moved = _join(manager, "u1")

    assert moved.status is JoinStatus.MOVED
    assert moved.position == 2
    assert moved.entry is not None and moved.entry.ticket == 3
    queue = manager.get_queue("默认")
    assert queue is not None
    assert [entry.user_id for entry in queue.entries] == ["u2", "u1"]


def test_duplicate_join_allowed_when_configured() -> None:
    """allow 策略下同一用户可以占多个位置。"""

    manager = QueueManager(auto_create_queue=True, duplicate_policy=DuplicatePolicy.ALLOW)
    _join(manager, "u1")
    again = _join(manager, "u1")

    assert again.status is JoinStatus.JOINED
    assert manager.get_queue("默认").size == 2  # type: ignore[union-attr]


def test_duplicate_policy_accepts_plain_string() -> None:
    """配置里读到的是字符串，构造器应能自行转换。"""

    manager = QueueManager(auto_create_queue=True, duplicate_policy="move_to_end")
    _join(manager, "u1")
    _join(manager, "u2")
    assert _join(manager, "u1").status is JoinStatus.MOVED


def test_join_respects_capacity() -> None:
    """人数达到上限后拒绝新入队，但已在队中的人仍可重复尝试。"""

    manager = QueueManager(auto_create_queue=True, default_capacity=2)
    _join(manager, "u1")
    _join(manager, "u2")
    full = _join(manager, "u3")

    assert full.status is JoinStatus.QUEUE_FULL
    assert full.queue is not None and full.queue.size == 2


def test_capacity_is_per_queue() -> None:
    """不同队列的人数上限互相独立。"""

    manager = QueueManager(auto_create_queue=True)
    manager.create_queue("小桌", capacity=1)
    _join(manager, "u1", queue="小桌")
    assert _join(manager, "u2", queue="小桌").status is JoinStatus.QUEUE_FULL
    assert _join(manager, "u2", queue="大厅").status is JoinStatus.JOINED


def test_join_rejected_when_paused(manager: QueueManager) -> None:
    """暂停中的队列不接受新排队。"""

    _join(manager, "u1")
    assert manager.set_paused("默认", True) is True

    result = _join(manager, "u2")
    assert result.status is JoinStatus.QUEUE_PAUSED
    assert result.queue is not None and result.queue.size == 1


def test_join_without_auto_create_reports_not_found() -> None:
    """关闭自动建队后，排不存在的队应明确报 QUEUE_NOT_FOUND。"""

    manager = QueueManager(auto_create_queue=False)
    assert _join(manager, "u1", queue="幽灵").status is JoinStatus.QUEUE_NOT_FOUND
    assert manager.queues() == []


def test_join_reports_too_many_queues() -> None:
    """超过队列数量上限时返回 TOO_MANY_QUEUES。"""

    manager = QueueManager(auto_create_queue=True, max_queues=1)
    assert _join(manager, "u1", queue="一队").status is JoinStatus.JOINED
    assert _join(manager, "u2", queue="二队").status is JoinStatus.TOO_MANY_QUEUES


def test_ensure_queue_returns_existing(manager: QueueManager) -> None:
    """ensure_queue 对已存在的队不重复创建。"""

    created = manager.create_queue("排位", display_name="排位赛")
    assert manager.ensure_queue("排位") is created
    assert len(manager.queues()) == 1


def test_create_queue_rejects_invalid_and_duplicate_keeps_original(manager: QueueManager) -> None:
    """非法队名返回 None；已存在的队名直接返回原对象。"""

    assert manager.create_queue("带 空格") is None
    first = manager.create_queue("排位")
    assert manager.create_queue("排位") is first


def test_create_queue_uses_default_capacity() -> None:
    """未指定上限时使用 default_capacity。"""

    manager = QueueManager(auto_create_queue=True, default_capacity=3)
    queue = manager.create_queue("排位")
    assert queue is not None and queue.capacity == 3


# ── 出队与叫号 ────────────────────────────────────────


def test_call_next_pops_head_and_marks_current(manager: QueueManager) -> None:
    """叫号把队首取出并置为 current，同时累计已叫号次数。"""

    _join(manager, "u1")
    _join(manager, "u2")

    called = manager.call_next("默认")
    assert called is not None and called.user_id == "u1"

    queue = manager.get_queue("默认")
    assert queue is not None
    assert queue.current is called
    assert [entry.user_id for entry in queue.entries] == ["u2"]
    assert queue.served_count == 1


def test_call_next_on_empty_returns_none(manager: QueueManager) -> None:
    """空队列叫号返回 None，且不会产生 current。"""

    manager.create_queue("默认")
    assert manager.call_next("默认") is None
    queue = manager.get_queue("默认")
    assert queue is not None and queue.current is None


def test_call_next_on_unknown_queue_returns_none(manager: QueueManager) -> None:
    """对不存在的队列叫号返回 None，且不会顺手建队。"""

    assert manager.call_next("幽灵队") is None
    assert manager.get_queue("幽灵队") is None


def test_call_next_replaces_previous_current(manager: QueueManager) -> None:
    """连续叫号时上一次的 current 被视为已服务完毕。"""

    _join(manager, "u1")
    _join(manager, "u2")
    manager.call_next("默认")
    second = manager.call_next("默认")

    assert second is not None and second.user_id == "u2"
    queue = manager.get_queue("默认")
    assert queue is not None and queue.current is second and queue.served_count == 2


def test_requeue_current_restores_head_and_ticket(manager: QueueManager) -> None:
    """放回队首应把 current 还原到 index 0，并回退计数。"""

    _join(manager, "u1")
    _join(manager, "u2")
    called = manager.call_next("默认")
    restored = manager.requeue_current("默认")

    assert restored is called
    queue = manager.get_queue("默认")
    assert queue is not None
    assert queue.current is None
    assert [entry.user_id for entry in queue.entries] == ["u1", "u2"]
    assert queue.served_count == 0


def test_requeue_current_without_current_returns_none(manager: QueueManager) -> None:
    """没有 current 时放回操作返回 None。"""

    assert manager.requeue_current("默认") is None


def test_finish_current_clears_current(manager: QueueManager) -> None:
    """结束当前后队列进入空档状态，已叫号次数保留。"""

    _join(manager, "u1")
    manager.call_next("默认")
    finished = manager.finish_current("默认")

    assert finished is not None and finished.user_id == "u1"
    queue = manager.get_queue("默认")
    assert queue is not None
    assert queue.current is None
    assert queue.served_count == 1


def test_finish_current_without_current_returns_none(manager: QueueManager) -> None:
    """没有 current 时结束操作返回 None。"""

    assert manager.finish_current("默认") is None


def test_leave_removes_waiting_entry(manager: QueueManager) -> None:
    """退队应把条目从等待列表中删除。"""

    _join(manager, "u1")
    _join(manager, "u2")

    left = manager.leave("默认", "u1")
    assert left is not None and left.user_id == "u1"
    assert [entry.user_id for entry in manager.get_queue("默认").entries] == ["u2"]  # type: ignore[union-attr]


def test_leave_also_clears_current(manager: QueueManager) -> None:
    """退队也能撤掉正在服务中的自己。"""

    _join(manager, "u1")
    manager.call_next("默认")
    assert manager.leave("默认", "u1") is not None
    assert manager.get_queue("默认").current is None  # type: ignore[union-attr]


def test_leave_unknown_user_or_queue_returns_none(manager: QueueManager) -> None:
    """退不存在的人或队都返回 None，不抛异常。"""

    _join(manager, "u1")
    assert manager.leave("默认", "u404") is None
    assert manager.leave("不存在", "u1") is None


def test_clear_returns_total_removed(manager: QueueManager) -> None:
    """清空应统计等待人数与 current。"""

    _join(manager, "u1")
    _join(manager, "u2")
    manager.call_next("默认")

    assert manager.clear("默认") == 2
    queue = manager.get_queue("默认")
    assert queue is not None and queue.size == 0 and queue.current is None


def test_clear_unknown_queue_returns_zero(manager: QueueManager) -> None:
    """清空不存在的队返回 0。"""

    assert manager.clear("不存在") == 0


def test_move_to_front(manager: QueueManager) -> None:
    """插队应把目标用户移到 index 0。"""

    _join(manager, "u1")
    _join(manager, "u2")
    _join(manager, "u3")

    assert manager.move_to_front("默认", "u3") is True
    assert [entry.user_id for entry in manager.get_queue("默认").entries] == ["u3", "u1", "u2"]  # type: ignore[union-attr]
    assert manager.move_to_front("默认", "u404") is False
    assert manager.move_to_front("不存在", "u1") is False


# ── 队列设置与生命周期 ────────────────────────────────


def test_set_paused_noop_when_unchanged_skips_notify(manager: QueueManager) -> None:
    """状态没变化时不应触发多余的持久化。"""

    manager.create_queue("默认")
    calls: list[int] = []
    manager.on_change = lambda _manager: calls.append(1)

    assert manager.set_paused("默认", False) is True
    assert calls == []
    assert manager.set_paused("默认", True) is True
    assert calls == [1]


def test_set_paused_and_capacity_on_unknown_queue(manager: QueueManager) -> None:
    """对不存在的队列设置参数返回 False。"""

    assert manager.set_paused("不存在", True) is False
    assert manager.set_capacity("不存在", 3) is False


def test_set_capacity_clamps_negative(manager: QueueManager) -> None:
    """负数上限按不限处理。"""

    manager.create_queue("排位")
    assert manager.set_capacity("排位", -5) is True
    assert manager.get_queue("排位").capacity == 0  # type: ignore[union-attr]


def test_delete_queue(manager: QueueManager) -> None:
    """删除队列后不应再能查到，也不能重复删除。"""

    _join(manager, "u1", queue="排位")
    manager.create_queue("娱乐")

    assert manager.delete_queue("排位") is True
    assert manager.get_queue("排位") is None
    assert manager.delete_queue("排位") is False
    assert [queue.name for queue in manager.queues()] == ["娱乐"]


def test_find_queues_of_user(manager: QueueManager) -> None:
    """查询用户所在队列应同时覆盖等待中与服务中。"""

    _join(manager, "u1", queue="排位")
    _join(manager, "u1", queue="娱乐")
    _join(manager, "u2", queue="娱乐")

    assert manager.find_queues_of("u1") == ["排位", "娱乐"]
    assert manager.find_queues_of("u2") == ["娱乐"]
    assert manager.find_queues_of("u404") == []

    manager.clear("娱乐")
    assert manager.find_queues_of("u2") == []


def test_find_queues_of_includes_current(manager: QueueManager) -> None:
    """被叫号的人仍应被算作「在这个队里」。"""

    _join(manager, "u1")
    manager.call_next("默认")
    assert manager.find_queues_of("u1") == ["默认"]


def test_total_waiting_excludes_current(manager: QueueManager) -> None:
    """总等待人数不计入正在服务的人。"""

    _join(manager, "u1")
    _join(manager, "u2")
    manager.call_next("默认")
    assert manager.total_waiting() == 1


# ── 超时清理 ──────────────────────────────────────────


def test_purge_expired_removes_old_entries(manager: QueueManager, clock: Any) -> None:
    """超过超时阈值的条目应被清理，未超时的不动。"""

    _join(manager, "u1")
    clock.advance(100)
    _join(manager, "u2")
    clock.advance(50)

    removed = manager.purge_expired(120)

    assert [entry.user_id for entry in removed] == ["u1"]
    queue = manager.get_queue("默认")
    assert queue is not None and [entry.user_id for entry in queue.entries] == ["u2"]


def test_purge_expired_disabled_by_zero(manager: QueueManager, clock: Any) -> None:
    """timeout <= 0 表示不清理。"""

    _join(manager, "u1")
    clock.advance(10_000)
    assert manager.purge_expired(0) == []
    assert manager.get_queue("默认").size == 1  # type: ignore[union-attr]


def test_purge_expired_on_empty_manager(manager: QueueManager) -> None:
    """没有任何队列时清理是安全的空操作。"""

    assert manager.purge_expired(60) == []


def test_purge_expired_keeps_current(manager: QueueManager, clock: Any) -> None:
    """正在服务中的人不受超时清理影响。"""

    _join(manager, "u1")
    manager.call_next("默认")
    clock.advance(10_000)

    assert manager.purge_expired(60) == []
    assert manager.get_queue("默认").current is not None  # type: ignore[union-attr]


def test_purge_expired_triggers_notify(manager: QueueManager, clock: Any) -> None:
    """有实际清理动作时才通知持久化。"""

    manager.create_queue("默认")
    _join(manager, "u1")
    calls: list[int] = []
    manager.on_change = lambda _manager: calls.append(1)

    clock.advance(10_000)
    manager.purge_expired(60)
    assert calls == [1]

    calls.clear()
    manager.purge_expired(60)
    assert calls == []


# ── 持久化 ────────────────────────────────────────────


def test_to_dict_and_load_dict_round_trip(manager: QueueManager, clock: Any) -> None:
    """落盘再读回应完整还原全部队列。"""

    manager.create_queue("排位", display_name="排位赛", capacity=3)
    manager.create_queue("默认")
    _join(manager, "u1", queue="排位")
    _join(manager, "u2", queue="排位", nickname="阿狸")
    manager.call_next("排位")
    _join(manager, "u3")
    manager.set_paused("默认", True)

    payload = manager.to_dict()

    revived = QueueManager(auto_create_queue=True, clock=clock)
    revived.load_dict(payload)

    assert [queue.name for queue in revived.queues()] == ["排位", "默认"]
    paiwei = revived.get_queue("排位")
    assert paiwei is not None
    assert paiwei.display_name == "排位赛"
    assert paiwei.capacity == 3
    assert paiwei.current is not None and paiwei.current.user_id == "u1"
    assert paiwei.served_count == 1
    assert revived.get_queue("默认").paused is True  # type: ignore[union-attr]
    assert revived.get_queue("默认").size == 1  # type: ignore[union-attr]


def test_load_dict_tolerates_none_and_garbage(manager: QueueManager) -> None:
    """空/异常落盘数据应让管理器保持为空而不是崩溃。"""

    manager.load_dict(None)
    assert manager.queues() == []

    manager.load_dict({"queues": "not-a-list"})
    assert manager.queues() == []

    manager.load_dict({"queues": [1, {"name": ""}, {"name": "有效"}]})
    assert [queue.name for queue in manager.queues()] == ["有效"]


def test_load_dict_restores_guild_mapping(manager: QueueManager) -> None:
    """队名到直播间号的归属映射也应被还原。"""

    _join(manager, "u1", queue="排位", guild="12345")
    payload = manager.to_dict()

    revived = QueueManager(auto_create_queue=True)
    revived.load_dict(payload)
    assert revived.guild_of == {"排位": "12345"}


def test_guild_recorded_only_on_create(manager: QueueManager) -> None:
    """已存在的队列不会被后续入队的 guild 覆盖。"""

    _join(manager, "u1", queue="排位", guild="111")
    _join(manager, "u2", queue="排位", guild="222")
    assert manager.guild_of == {"排位": "111"}


def test_reset_clears_state(manager: QueueManager) -> None:
    """reset 应清空内存状态。"""

    _join(manager, "u1")
    manager.reset()
    assert manager.queues() == []
    assert manager.guild_of == {}


def test_on_change_fires_on_mutations(manager: QueueManager) -> None:
    """所有会改变队列的操作都应触发持久化回调。"""

    calls: list[int] = []
    manager.on_change = lambda _manager: calls.append(1)

    _join(manager, "u1")
    manager.call_next("默认")
    manager.requeue_current("默认")
    manager.finish_current("默认")
    manager.clear("默认")
    manager.delete_queue("默认")

    assert len(calls) == 6


def test_on_change_exception_does_not_break_mutation(manager: QueueManager) -> None:
    """持久化回调抛异常不应打断队列操作本身。"""

    def boom(_manager: QueueManager) -> None:
        raise RuntimeError("存储炸了")

    manager.on_change = boom
    with pytest.raises(RuntimeError):
        _join(manager, "u1")

    # 状态本身已经写入，只是回调失败
    assert manager.get_queue("默认") is not None
