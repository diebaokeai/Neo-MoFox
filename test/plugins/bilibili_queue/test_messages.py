"""bilibili_queue 文案函数的单测（数据来自独立 app 的 JSON 返回）。"""

from __future__ import annotations

from typing import Any

from plugins.bilibili_queue.core import messages


def _queue(
    *,
    name: str = "排位",
    title: str = "排位赛",
    waiting: int = 2,
    capacity: int = 0,
    paused: bool = False,
    current: dict[str, Any] | None = None,
    entries: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """构造一份 app 风格的队列快照。"""

    return {
        "name": name,
        "title": title,
        "waiting": waiting,
        "capacity": capacity,
        "paused": paused,
        "current": current,
        "served_count": 3,
        "next_ticket": 9,
        "entries": entries
        if entries is not None
        else [
            {"nickname": "小满", "ticket": 8, "position": 1, "user_id": "u2"},
            {"nickname": "青柠", "ticket": 9, "position": 2, "user_id": "u3"},
        ],
    }


# ── format_wait ───────────────────────────────────────


def test_format_wait() -> None:
    """等待时长格式化。"""

    assert messages.format_wait(0) == "0秒"
    assert messages.format_wait(45) == "45秒"
    assert messages.format_wait(60) == "1分"
    assert messages.format_wait(95) == "1分35秒"
    assert messages.format_wait(3600) == "1小时"
    assert messages.format_wait(3900) == "1小时5分"
    assert messages.format_wait(-5) == "0秒"


# ── join_reply ────────────────────────────────────────


def test_join_reply_uses_app_message() -> None:
    """入队成功直接用 app 给的文案。"""

    payload = {"ok": True, "status": "joined", "message": "已加入「排位」，你是第 1 位"}
    assert messages.join_reply(payload) == "已加入「排位」，你是第 1 位"


def test_join_reply_appends_hint_on_failure() -> None:
    """失败时补上「怎么排队」的引导。"""

    payload = {"ok": False, "status": "queue_full", "message": "「排位」已排满"}
    text = messages.join_reply(payload, join_words=["排队", "上车"])

    assert "已排满" in text
    assert "排队" in text


def test_join_reply_without_hint_words() -> None:
    """没有关键词表时不硬拼提示。"""

    payload = {"ok": False, "status": "queue_full", "message": "「排位」已排满"}
    assert messages.join_reply(payload) == "「排位」已排满"


def test_join_reply_for_app_unavailable() -> None:
    """排队姬不可用时的合成结果也要有可读文案。"""

    payload = {"ok": False, "status": "app_unavailable", "message": "排队服务暂时不可用"}
    text = messages.join_reply(payload, join_words=["排队"])
    assert "暂时不可用" in text


def test_join_reply_without_message() -> None:
    """没有 message 时的兜底。"""

    assert messages.join_reply({"ok": True, "status": "joined"}) == "已收到你的排队请求"


# ── leave_reply ───────────────────────────────────────


def test_leave_reply_lists_queues() -> None:
    """退队成功列出队列名与昵称。"""

    payload = {"ok": True, "queues": ["排位", "娱乐"]}
    text = messages.leave_reply(payload, "阿狸")

    assert "阿狸" in text
    assert "「排位」" in text and "「娱乐」" in text


def test_leave_reply_when_not_queued() -> None:
    """不在队列里时给出提示。"""

    assert "不在任何队列" in messages.leave_reply({"ok": False, "queues": []}, "阿狸")


def test_leave_reply_tolerates_broken_payload() -> None:
    """字段缺失/类型不对也不能崩。"""

    assert "不在任何队列" in messages.leave_reply({}, "阿狸")
    assert "不在任何队列" in messages.leave_reply({"queues": "排位"}, "阿狸")


# ── status_reply / queue_line ─────────────────────────


def test_queue_line_with_current_and_entries() -> None:
    """单队列描述包含当前叫号与等待名单。"""

    queue = _queue(current={"nickname": "阿狸", "ticket": 7, "user_id": "u1"})
    line = messages.queue_line(queue)

    assert "【排位赛】2 人排队" in line
    assert "当前：阿狸（#7）" in line
    assert "1. 小满（#8）" in line


def test_queue_line_truncates() -> None:
    """超出上限的部分折叠。"""

    queue = _queue(
        waiting=10,
        entries=[{"nickname": f"观众{i}", "ticket": i, "position": i} for i in range(10)],
    )
    line = messages.queue_line(queue, max_rows=3)

    assert "还有 7 人" in line
    assert "4. " not in line


def test_queue_line_marks_paused_capacity_and_empty() -> None:
    """暂停 / 上限 / 空队列都要有标记。"""

    assert "暂停中" in messages.queue_line(_queue(paused=True))
    assert "上限 5" in messages.queue_line(_queue(capacity=5))
    assert "（空）" in messages.queue_line(_queue(waiting=0, entries=[]))


def test_status_reply_renders_all_queues() -> None:
    """多队列逐条渲染。"""

    snapshot = {"queues": [_queue(name="排位"), _queue(name="娱乐", title="娱乐")]}
    text = messages.status_reply(snapshot)
    assert "【排位赛】" in text and "【娱乐】" in text


def test_status_reply_when_empty() -> None:
    """没有队列时给引导文案。"""

    assert "还没有开任何队列" in messages.status_reply({"queues": []})
    assert "还没有开任何队列" in messages.status_reply({})
    assert "还没有开任何队列" in messages.status_reply({"queues": "坏数据"})


def test_status_reply_ignores_broken_rows() -> None:
    """快照里混进非 dict 的行时跳过而不是崩。"""

    snapshot = {"queues": ["垃圾", _queue()]}
    assert "【排位赛】" in messages.status_reply(snapshot)


# ── operation_reply / admin_usage ─────────────────────


def test_operation_reply() -> None:
    """管理操作直接复用 app 文案，缺省时有兜底。"""

    assert messages.operation_reply({"message": "叫到 阿狸"}) == "叫到 阿狸"
    assert messages.operation_reply({}, fallback="好了") == "好了"
    assert messages.operation_reply({"message": "  "}) == "操作完成"


def test_admin_usage_prefix() -> None:
    """帮助文案要带上前缀。"""

    assert "下一个" in messages.admin_usage()
    prefixed = messages.admin_usage("姬姬")
    assert "姬姬下一个" in prefixed
    assert "姬姬暂停" in prefixed
