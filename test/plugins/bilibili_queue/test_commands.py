"""bilibili_queue 聊天平台命令组件的单测。

直接调用 ``@cmd_route`` 装饰的方法（命令路由本身由框架负责），验证每个子命令的行为、
回复文案与空状态处理。队列操作会落到真实的 live_queue hub 上。
"""

from __future__ import annotations

from typing import Any

import pytest

from plugins.bilibili_queue.components import queue_admin_command as admin_module
from plugins.bilibili_queue.components import queue_command as command_module
from plugins.bilibili_queue.components.queue_admin_command import QueueAdminCommand
from plugins.bilibili_queue.components.queue_command import QueueCommand
from plugins.bilibili_queue.core.runtime import QueueRuntime, clear_queue_runtime
from src.core.models import Message


class _DummyPlugin:
    """命令构造只需要一个持有它的对象。"""

    plugin_name = "bilibili_queue"
    config = None


def _message(
    *,
    user_id: str = "u1",
    nickname: str = "阿狸",
    platform: str = "bilibili_live",
    group_id: str = "12345",
    guard_level: int = 0,
) -> Message:
    """构造一条命令来源消息。"""

    return Message(
        message_id="m1",
        sender_id=user_id,
        sender_name=nickname,
        platform=platform,
        stream_id="stream-1",
        group_id=group_id,
        raw_data={"guard_level": guard_level} if guard_level else None,
    )


@pytest.fixture
def sent(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """拦截两个命令模块里的 ``send_text``。"""

    messages: list[str] = []

    async def fake_send_text(content: str, stream_id: str, **kwargs: Any) -> bool:
        messages.append(content)
        return True

    monkeypatch.setattr(command_module, "send_text", fake_send_text)
    monkeypatch.setattr(admin_module, "send_text", fake_send_text)
    return messages


@pytest.fixture
def command(runtime: QueueRuntime) -> QueueCommand:
    """观众命令实例（发送者 u1 / 阿狸）。"""

    return QueueCommand(  # type: ignore[arg-type]
        plugin=_DummyPlugin(), stream_id="stream-1", message_id="m1", message=_message()
    )


@pytest.fixture
def admin_command(runtime: QueueRuntime) -> QueueAdminCommand:
    """管理命令实例。"""

    return QueueAdminCommand(  # type: ignore[arg-type]
        plugin=_DummyPlugin(), stream_id="stream-1", message_id="m1", message=_message()
    )


def _seed(hub: Any) -> None:
    """往「排位」里放两个人并叫号一次。

    注意：live_queue 的 ``QueueHub`` 方法是**同步**的（异步的是插件侧的 HTTP 客户端）。
    """

    hub.join(user_id="u1", nickname="阿狸", queue="排位")
    hub.join(user_id="u2", nickname="小满", queue="排位")
    hub.call_next("排位")


# ── 匹配与元数据 ──────────────────────────────────────


@pytest.mark.parametrize(
    ("parts", "expected"),
    [
        (["排队"], 1),
        (["queue"], 1),
        (["排队", "加入", "排位"], 1),
        (["排队管理"], 0),
        (["其他"], 0),
        ([], 0),
    ],
)
def test_queue_command_match(parts: list[str], expected: int) -> None:
    """观众命令的触发词。"""

    assert QueueCommand.match(parts) == expected


@pytest.mark.parametrize(
    ("parts", "expected"),
    [
        (["排队管理"], 1),
        (["queueadmin"], 1),
        (["排队管理", "下一个"], 1),
        (["排队"], 0),
        ([], 0),
    ],
)
def test_admin_command_match(parts: list[str], expected: int) -> None:
    """管理命令的触发词与观众命令不能撞车。"""

    assert QueueAdminCommand.match(parts) == expected


def test_command_permission_levels() -> None:
    """管理命令的权限必须高于观众命令。"""

    from src.app.plugin_system.types import PermissionLevel

    assert QueueCommand.permission_level is PermissionLevel.USER
    assert QueueAdminCommand.permission_level is PermissionLevel.OPERATOR


def test_component_names_match_manifest() -> None:
    """组件名要和 manifest 里的 component_name 一致，否则注册会被静默跳过。"""

    assert QueueCommand.name == "排队"
    assert QueueAdminCommand.name == "排队管理"


# ── 观众命令 ──────────────────────────────────────────


async def test_join_default_queue(command: QueueCommand, hub: Any, sent: list[str]) -> None:
    """``/排队`` 把发送者加进默认队并回复。"""

    ok, code = await command.handle_join_default()

    assert ok is True
    assert code == "joined"
    queue = hub.manager.get_queue("默认")
    assert queue is not None and [entry.nickname for entry in queue.entries] == ["阿狸"]
    assert sent and "已加入" in sent[0]


async def test_join_named_queue(command: QueueCommand, hub: Any, sent: list[str]) -> None:
    """``/排队 加入 <队名>`` 走 app 侧的别名解析。"""

    ok, code = await command.handle_join("排位赛")

    assert ok is True
    assert code == "joined"
    assert hub.manager.get_queue("排位") is not None
    assert "排位" in sent[0]


async def test_join_duplicate_reports_already_in(
    command: QueueCommand, hub: Any, sent: list[str]
) -> None:
    """重复入队时回复「已经在队列里」。"""

    await command.handle_join("排位")
    ok, code = await command.handle_join("排位")

    assert ok is True
    assert code == "already_in"
    assert "已经在" in sent[-1]


async def test_leave_removes_from_all_queues(command: QueueCommand, sent: list[str]) -> None:
    """``/排队 退出`` 不带队名时退出全部队列。"""

    await command.handle_join("排位")
    await command.handle_join("娱乐")

    ok, code = await command.handle_leave()

    assert ok is True
    assert code == "left"
    assert "排位" in sent[-1] and "娱乐" in sent[-1]


async def test_leave_specific_queue(command: QueueCommand, sent: list[str]) -> None:
    """``/排队 退出 <队名>`` 只退指定队。"""

    await command.handle_join("排位")
    await command.handle_join("娱乐")

    await command.handle_leave("排位")
    assert "排位" in sent[-1] and "娱乐" not in sent[-1]


async def test_list_shows_all_queues(command: QueueCommand, sent: list[str]) -> None:
    """``/排队 列表`` 列出队列概况。"""

    await command.handle_join("排位")
    ok, code = await command.handle_list()

    assert ok is True
    assert code == "listed"
    assert "【排位】" in sent[-1]


async def test_mine_reports_position(command: QueueCommand, sent: list[str]) -> None:
    """``/排队 我的`` 报出自己的位置。"""

    await command.handle_join("排位")
    ok, code = await command.handle_mine()

    assert ok is True
    assert code == "mined"
    assert "第 1 位" in sent[-1]


async def test_mine_reports_current(command: QueueCommand, hub: Any, sent: list[str]) -> None:
    """被叫号时显示「正在服务中」。"""

    await command.handle_join("排位")
    hub.call_next("排位")

    await command.handle_mine()
    assert "正在服务中" in sent[-1]


async def test_mine_when_not_queued(command: QueueCommand, sent: list[str]) -> None:
    """没排队时给出引导。"""

    ok, code = await command.handle_mine()

    assert ok is True
    assert code == "empty"
    assert "还没有排队" in sent[-1]


async def test_commands_report_when_runtime_missing(sent: list[str]) -> None:
    """插件未加载时命令要给出可读提示，而不是抛异常。"""

    clear_queue_runtime()
    viewer = QueueCommand(  # type: ignore[arg-type]
        plugin=_DummyPlugin(), stream_id="s", message_id="m", message=_message()
    )
    admin = QueueAdminCommand(  # type: ignore[arg-type]
        plugin=_DummyPlugin(), stream_id="s", message_id="m", message=_message()
    )

    assert (await viewer.handle_join_default())[0] is False
    assert (await viewer.handle_list())[0] is False
    assert (await viewer.handle_leave())[0] is False
    assert (await viewer.handle_mine())[0] is False
    assert (await admin.handle_next())[0] is False
    assert any("未启用" in text for text in sent)


async def test_commands_report_when_app_offline(
    offline_runtime: QueueRuntime, sent: list[str]
) -> None:
    """排队姬不可用时命令返回失败并说明原因。"""

    viewer = QueueCommand(  # type: ignore[arg-type]
        plugin=_DummyPlugin(), stream_id="s", message_id="m", message=_message()
    )
    ok, code = await viewer.handle_join_default()

    assert ok is False
    assert code == "app_unavailable"
    assert sent and "不可用" in sent[0]


# ── 管理命令 ──────────────────────────────────────────


async def test_admin_next(admin_command: QueueAdminCommand, hub: Any, sent: list[str]) -> None:
    """``下一个`` 叫号。"""

    _seed(hub)
    hub.requeue_current("排位")  # 让 u1 回到队首

    ok, code = await admin_command.handle_next("排位")
    assert ok is True and code == "called"
    assert "阿狸" in sent[-1]


async def test_admin_next_on_empty_queue(
    admin_command: QueueAdminCommand, hub: Any, sent: list[str]
) -> None:
    """空队列叫号给出提示（没人可叫，所以 ok=False）。"""

    hub.create_queue("排位")
    ok, code = await admin_command.handle_next("排位")
    assert ok is False and code == "empty"
    assert "空" in sent[-1]


async def test_admin_finish(admin_command: QueueAdminCommand, hub: Any) -> None:
    """``结束`` 结束当前。"""

    _seed(hub)
    ok, code = await admin_command.handle_finish("排位")

    assert ok is True and code == "finished"
    assert hub.manager.get_queue("排位").current is None  # type: ignore[union-attr]


async def test_admin_finish_without_current(
    admin_command: QueueAdminCommand, hub: Any, sent: list[str]
) -> None:
    """没有当前对象时结束命令给出提示（没东西可结束，所以 ok=False）。"""

    hub.create_queue("排位")
    ok, code = await admin_command.handle_finish("排位")
    assert ok is False and code == "empty"
    assert "没有正在服务" in sent[-1]


async def test_admin_requeue(admin_command: QueueAdminCommand, hub: Any) -> None:
    """``放回`` 把当前放回队首。"""

    _seed(hub)
    ok, code = await admin_command.handle_requeue("排位")

    assert ok is True and code == "requeued"
    queue = hub.manager.get_queue("排位")
    assert queue is not None and queue.entries[0].user_id == "u1"


async def test_admin_clear(admin_command: QueueAdminCommand, hub: Any) -> None:
    """``清空`` 清空队列。"""

    _seed(hub)
    ok, code = await admin_command.handle_clear("排位")

    assert ok is True and code == "cleared"
    assert hub.manager.get_queue("排位").size == 0  # type: ignore[union-attr]


async def test_admin_pause_and_resume(admin_command: QueueAdminCommand, hub: Any) -> None:
    """``暂停`` / ``恢复``。"""

    _seed(hub)

    assert (await admin_command.handle_pause("排位"))[1] == "paused"
    assert hub.manager.get_queue("排位").paused is True  # type: ignore[union-attr]

    assert (await admin_command.handle_resume("排位"))[1] == "resumed"
    assert hub.manager.get_queue("排位").paused is False  # type: ignore[union-attr]


async def test_admin_unknown_queue_actions(
    admin_command: QueueAdminCommand, sent: list[str]
) -> None:
    """对不存在的队列操作返回 queue_not_found。"""

    assert (await admin_command.handle_clear("幽灵"))[1] == "queue_not_found"
    assert (await admin_command.handle_pause("幽灵"))[1] == "queue_not_found"
    assert (await admin_command.handle_resume("幽灵"))[1] == "queue_not_found"
    assert any("找不到" in text for text in sent)


async def test_admin_queues_listing(
    admin_command: QueueAdminCommand, hub: Any, sent: list[str]
) -> None:
    """``队列`` 列出全部队列。"""

    _seed(hub)
    ok, code = await admin_command.handle_queues()

    assert ok is True and code == "listed"
    assert "【排位】" in sent[-1]


async def test_admin_reset(admin_command: QueueAdminCommand, hub: Any) -> None:
    """``重置`` 清空全部队列；带 delete 时连队列一起删。"""

    _seed(hub)
    hub.create_queue("娱乐")

    ok, code = await admin_command.handle_reset()
    assert ok is True and code == "reset"
    assert hub.manager.get_queue("排位").size == 0  # type: ignore[union-attr]
    assert hub.manager.get_queue("娱乐") is not None

    _seed(hub)
    assert (await admin_command.handle_reset("delete"))[0] is True
    assert hub.manager.get_queue("排位") is None


async def test_admin_create_queue(
    admin_command: QueueAdminCommand, hub: Any, sent: list[str]
) -> None:
    """``建队 <队名> [展示名] [上限]``。"""

    ok, code = await admin_command.handle_create("排位", "排位赛", 5)

    assert ok is True and code == "created"
    queue = hub.manager.get_queue("排位")
    assert queue is not None and queue.title == "排位赛" and queue.capacity == 5
    assert "排位赛" in sent[-1]


async def test_admin_create_requires_name(
    admin_command: QueueAdminCommand, sent: list[str]
) -> None:
    """建队必须给队名。"""

    ok, code = await admin_command.handle_create()
    assert ok is False and code == "missing_arg"
    assert "请指定队名" in sent[-1]


async def test_admin_create_invalid_name(
    admin_command: QueueAdminCommand, sent: list[str]
) -> None:
    """非法队名给出明确失败。"""

    ok, code = await admin_command.handle_create("带 空格")
    assert ok is False and code == "invalid_name"
    assert "不合法" in sent[-1]


async def test_admin_delete_queue(
    admin_command: QueueAdminCommand, hub: Any, sent: list[str]
) -> None:
    """``删队``。"""

    _seed(hub)
    assert (await admin_command.handle_delete("排位"))[1] == "deleted"
    assert hub.manager.get_queue("排位") is None


async def test_admin_delete_requires_name(
    admin_command: QueueAdminCommand, sent: list[str]
) -> None:
    """``删队`` 必须给队名，不能默认删默认队。"""

    assert (await admin_command.handle_delete())[1] == "missing_arg"
    assert "请指定" in sent[-1]


async def test_admin_delete_unknown_queue(
    admin_command: QueueAdminCommand, sent: list[str]
) -> None:
    """删不存在的队返回 queue_not_found。"""

    assert (await admin_command.handle_delete("幽灵"))[1] == "queue_not_found"


async def test_admin_remove_user(
    admin_command: QueueAdminCommand, hub: Any
) -> None:
    """``移除 <用户ID> [队名]``。"""

    _seed(hub)
    ok, code = await admin_command.handle_remove("u2", "排位")

    assert ok is True and code == "removed"
    assert hub.manager.get_queue("排位").position_of("u2") == 0  # type: ignore[union-attr]


async def test_admin_remove_missing_or_unknown(
    admin_command: QueueAdminCommand, hub: Any
) -> None:
    """缺少 ID 与用户不在队中都要有可读结果。"""

    _seed(hub)
    assert (await admin_command.handle_remove())[1] == "missing_arg"
    assert (await admin_command.handle_remove("u404", "排位"))[1] == "not_found"


async def test_admin_help(admin_command: QueueAdminCommand, sent: list[str]) -> None:
    """``帮助`` 列出命令表。"""

    ok, code = await admin_command.handle_help()
    assert ok is True and code == "help"
    assert "下一个" in sent[-1]
