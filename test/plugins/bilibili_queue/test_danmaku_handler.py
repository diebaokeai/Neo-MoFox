"""bilibili_queue 弹幕事件处理器的单测。

队列操作会真的落到一个 live_queue hub 上（通过假客户端转发），所以这里断言的是
**真实队列语义**：命中关键词后队列里到底有没有人、叫号叫到了谁。

重点验证「该拦的拦住、不该拦的放行」，以及「排队姬不可用时不把弹幕丢给 LLM」。
"""

from __future__ import annotations

from itertools import count
from types import SimpleNamespace
from typing import Any

import pytest

from plugins.bilibili_queue.components import danmaku_handler as handler_module
from plugins.bilibili_queue.components.danmaku_handler import DanmakuQueueHandler
from plugins.bilibili_queue.core.runtime import (
    QueueRuntime,
    clear_queue_runtime,
)
from src.kernel.event import EventDecision


class _DummyPlugin:
    """处理器构造只需要一个持有它的对象。"""

    plugin_name = "bilibili_queue"
    config = None


_MSG_SEQ = count(1)
"""弹幕序号：真实平台上每条弹幕都有自己的 msg_id（用于幂等去重）。"""


def _envelope(
    text: str = "排队",
    *,
    platform: str = "bilibili_live",
    guard_level: int = 0,
    user_id: str = "open-1",
    nickname: str = "阿狸",
    group_id: str = "12345",
    message_id: str = "",
) -> dict[str, Any]:
    """构造一封 B 站弹幕信封。

    默认每条弹幕给一个唯一 msg_id，与真实平台一致；想测幂等时显式传同一个
    message_id 即可。
    """

    return {
        "direction": "incoming",
        "message_segment": {"type": "text", "data": text},
        "message_info": {
            "platform": platform,
            "message_id": message_id or f"msg-{next(_MSG_SEQ)}",
            "user_info": {
                "user_id": user_id,
                "user_nickname": nickname,
                "role": SimpleNamespace(value="operator" if guard_level else "member"),
            },
            "group_info": {"group_id": group_id, "group_name": "某主播"},
            "additional_config": {"guard_level": guard_level},
        },
    }


def _params(envelope: dict[str, Any]) -> dict[str, Any]:
    """构造 ``BEFORE_MESSAGE_RECEIVED`` 的事件参数。"""

    return {"envelope": envelope, "adapter_signature": "bilibili_live_adapter:adapter:x"}


@pytest.fixture
def sent(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """拦截 ``send_text``，记录发出的文案。"""

    messages: list[str] = []

    async def fake_send_text(content: str, stream_id: str, **kwargs: Any) -> bool:
        messages.append(content)
        return True

    monkeypatch.setattr(handler_module, "send_text", fake_send_text)
    return messages


@pytest.fixture
def handler() -> DanmakuQueueHandler:
    """待测处理器。"""

    return DanmakuQueueHandler(plugin=_DummyPlugin())  # type: ignore[arg-type]


# ── 放行路径 ──────────────────────────────────────────


async def test_pass_when_runtime_missing(handler: DanmakuQueueHandler) -> None:
    """插件未加载时放行，不影响正常聊天。"""

    clear_queue_runtime()
    decision, params = await handler.execute("before_message_received", _params(_envelope()))
    assert decision is EventDecision.PASS
    assert set(params) == {"envelope", "adapter_signature"}


async def test_pass_when_disabled(handler: DanmakuQueueHandler, runtime: QueueRuntime) -> None:
    """配置禁用时不处理弹幕。"""

    runtime.settings.enabled = False
    decision, _ = await handler.execute("before_message_received", _params(_envelope()))
    assert decision is EventDecision.PASS


async def test_pass_for_unsupported_platform(
    handler: DanmakuQueueHandler, runtime: QueueRuntime
) -> None:
    """白名单外的平台不处理。"""

    decision, _ = await handler.execute(
        "before_message_received", _params(_envelope(platform="onebot"))
    )
    assert decision is EventDecision.PASS


async def test_pass_for_irrelevant_chatter(
    handler: DanmakuQueueHandler, runtime: QueueRuntime, hub: Any
) -> None:
    """与排队无关的弹幕照常进入对话流程。"""

    decision, _ = await handler.execute(
        "before_message_received", _params(_envelope("主播今天好可爱"))
    )
    assert decision is EventDecision.PASS
    assert hub.manager.queues() == []


async def test_pass_for_empty_text(handler: DanmakuQueueHandler, runtime: QueueRuntime) -> None:
    """空弹幕放行。"""

    decision, _ = await handler.execute("before_message_received", _params(_envelope("")))
    assert decision is EventDecision.PASS


async def test_pass_for_malformed_params(
    handler: DanmakuQueueHandler, runtime: QueueRuntime
) -> None:
    """信封结构完全不对时也不能崩。"""

    decision, params = await handler.execute("before_message_received", {"envelope": None})
    assert decision is EventDecision.PASS
    assert set(params) == {"envelope"}


async def test_pass_when_internal_error(
    handler: DanmakuQueueHandler, runtime: QueueRuntime, monkeypatch: pytest.MonkeyPatch
) -> None:
    """内部异常必须被吞掉并放行——抛出去会被框架当成 PASS，等于静默丢消息。"""

    def boom(_text: str) -> None:
        raise RuntimeError("解析炸了")

    monkeypatch.setattr(runtime, "parse", boom)
    decision, _ = await handler.execute("before_message_received", _params(_envelope()))
    assert decision is EventDecision.PASS


# ── 入队 ──────────────────────────────────────────────


async def test_join_blocks_message_and_enqueues(
    handler: DanmakuQueueHandler, runtime: QueueRuntime, hub: Any
) -> None:
    """命中入队关键词时入队并拦截弹幕。"""

    params = _params(_envelope("排队 排位", guard_level=3))
    decision, returned = await handler.execute("before_message_received", params)

    assert decision is EventDecision.STOP
    assert returned is params  # key 集合必须与入参一致

    queue = hub.manager.get_queue("排位")
    assert queue is not None
    assert [entry.nickname for entry in queue.entries] == ["阿狸"]
    assert queue.entries[0].platform == "bilibili_live"
    assert queue.entries[0].extra["guard_level"] == 3


async def test_join_sends_queue_explicit_flag(
    handler: DanmakuQueueHandler, runtime: QueueRuntime, hub: Any, app_client: Any
) -> None:
    """弹幕里「排位」是明确写的，「一下」不是——这个区别要传给 app。"""

    await handler.execute("before_message_received", _params(_envelope("排队 排位")))
    await handler.execute(
        "before_message_received", _params(_envelope("排队一下", user_id="open-2"))
    )

    join_calls = [kwargs for name, kwargs in app_client.calls if name == "join"]
    assert join_calls[0]["queue_explicit"] is True
    assert join_calls[1]["queue_explicit"] is False

    # 「排队一下」落到 app 的默认队，不会建出「一下」队
    assert hub.manager.get_queue("默认") is not None


async def test_same_msg_id_is_processed_once(
    handler: DanmakuQueueHandler, runtime: QueueRuntime, hub: Any
) -> None:
    """同一个 msg_id 的弹幕重复投递只入队一次。

    bot 的 bilibili_live_adapter 与独立弹幕桥同时在线时，两边可能拿到同一条弹幕；
    带着同一个 msg_id 的那次会被 app 判为 duplicate_event，队列里不会出现两个人。
    """

    envelope = _envelope("排队 排位", message_id="dup-1")
    await handler.execute("before_message_received", _params(envelope))
    await handler.execute("before_message_received", _params(envelope))

    queue = hub.manager.get_queue("排位")
    assert queue is not None
    assert queue.size == 1
    assert hub.manager.get_queue("一下") is None


async def test_join_uses_default_queue_for_plain_keyword(
    handler: DanmakuQueueHandler, runtime: QueueRuntime, hub: Any
) -> None:
    """只发关键词时进默认队。"""

    await handler.execute("before_message_received", _params(_envelope("排队")))
    assert hub.manager.get_queue("默认") is not None


async def test_duplicate_join_is_blocked_but_queue_unchanged(
    handler: DanmakuQueueHandler, runtime: QueueRuntime, hub: Any
) -> None:
    """重复排队仍然拦截弹幕，但队列人数不变。"""

    await handler.execute("before_message_received", _params(_envelope("排队 排位")))
    decision, _ = await handler.execute(
        "before_message_received", _params(_envelope("排队 排位"))
    )

    assert decision is EventDecision.STOP
    assert hub.manager.get_queue("排位").size == 1  # type: ignore[union-attr]


async def test_success_instead_of_stop_when_blocking_disabled(
    handler: DanmakuQueueHandler, runtime: QueueRuntime, hub: Any
) -> None:
    """关掉「拦截弹幕」后仍然操作队列，但让弹幕继续进对话流程。"""

    runtime.settings.block_message_from_llm = False
    decision, _ = await handler.execute("before_message_received", _params(_envelope("排队")))

    assert decision is EventDecision.SUCCESS
    assert hub.manager.get_queue("默认") is not None


async def test_join_reply_sent_when_reply_enabled(
    handler: DanmakuQueueHandler, runtime: QueueRuntime, sent: list[str]
) -> None:
    """开启出站反馈后要把结果发到原聊天流。"""

    runtime.settings.reply_in_stream = True
    await handler.execute("before_message_received", _params(_envelope("排队 排位")))

    assert sent and "排位" in sent[0]


async def test_join_reply_logged_when_reply_disabled(
    handler: DanmakuQueueHandler, runtime: QueueRuntime, sent: list[str]
) -> None:
    """默认不出站时不应调用 send_text（B 站也发不出去）。"""

    await handler.execute("before_message_received", _params(_envelope("排队")))
    assert sent == []


async def test_reply_throttled_by_cooldown(
    handler: DanmakuQueueHandler, runtime: QueueRuntime, sent: list[str]
) -> None:
    """同一聊天流内的连续回复要被冷却节流。"""

    runtime.settings.reply_in_stream = True
    runtime.settings.reply_cooldown = 60.0

    await handler.execute("before_message_received", _params(_envelope("排队", user_id="u1")))
    await handler.execute("before_message_received", _params(_envelope("排队", user_id="u2")))

    assert len(sent) == 1


# ── 排队姬不可用 ──────────────────────────────────────


async def test_offline_app_still_blocks_queue_keyword(
    handler: DanmakuQueueHandler, offline_runtime: QueueRuntime
) -> None:
    """排队姬不可用时，队列关键词仍然拦截——「排队」是命令，丢给 LLM 只会得到莫名其妙的回复。"""

    decision, _ = await handler.execute("before_message_received", _params(_envelope("排队")))
    assert decision is EventDecision.STOP


async def test_offline_app_reply_mentions_unavailable(
    handler: DanmakuQueueHandler, offline_runtime: QueueRuntime, sent: list[str]
) -> None:
    """支持出站的平台上要告诉观众服务暂不可用。"""

    offline_runtime.settings.reply_in_stream = True
    await handler.execute("before_message_received", _params(_envelope("排队")))

    assert sent and "不可用" in sent[0]


async def test_offline_app_does_not_break_status_query(
    handler: DanmakuQueueHandler, offline_runtime: QueueRuntime
) -> None:
    """状态查询在离线时也不能抛异常。"""

    decision, _ = await handler.execute(
        "before_message_received", _params(_envelope("排队情况"))
    )
    assert decision is EventDecision.STOP


# ── 退队与查询 ────────────────────────────────────────


async def test_leave_removes_from_queues(
    handler: DanmakuQueueHandler, runtime: QueueRuntime, hub: Any
) -> None:
    """退队关键词要能退出所在队列。"""

    await handler.execute("before_message_received", _params(_envelope("排队 排位")))
    decision, _ = await handler.execute("before_message_received", _params(_envelope("退队")))

    assert decision is EventDecision.STOP
    assert hub.manager.get_queue("排位").size == 0  # type: ignore[union-attr]


async def test_leave_when_not_queued(handler: DanmakuQueueHandler, runtime: QueueRuntime) -> None:
    """没排队时退队也不报错。"""

    decision, _ = await handler.execute("before_message_received", _params(_envelope("退队")))
    assert decision is EventDecision.STOP


async def test_status_query_is_handled(
    handler: DanmakuQueueHandler, runtime: QueueRuntime
) -> None:
    """状态查询被识别为排队相关，拦截不进 LLM。"""

    await handler.execute("before_message_received", _params(_envelope("排队 排位")))
    decision, _ = await handler.execute(
        "before_message_received", _params(_envelope("排队情况"))
    )
    assert decision is EventDecision.STOP


async def test_status_query_disabled_falls_back_to_pass(
    handler: DanmakuQueueHandler, runtime: QueueRuntime
) -> None:
    """关掉状态查询后该关键词不再被拦截。"""

    runtime.settings.queue_status_command = False
    decision, _ = await handler.execute(
        "before_message_received", _params(_envelope("排队情况"))
    )
    assert decision is EventDecision.PASS


# ── 舰长弹幕命令 ──────────────────────────────────────


async def test_admin_command_from_guard_is_executed(
    handler: DanmakuQueueHandler, runtime: QueueRuntime, hub: Any
) -> None:
    """舰长弹幕命令可以叫号。"""

    await handler.execute(
        "before_message_received", _params(_envelope("排队 排位", user_id="u1", nickname="阿狸"))
    )
    decision, _ = await handler.execute(
        "before_message_received", _params(_envelope("下一个 排位", guard_level=3, user_id="u2"))
    )

    assert decision is EventDecision.STOP
    queue = hub.manager.get_queue("排位")
    assert queue is not None and queue.current is not None
    assert queue.current.user_id == "u1"


async def test_admin_command_from_normal_viewer_is_ignored(
    handler: DanmakuQueueHandler, runtime: QueueRuntime, hub: Any
) -> None:
    """普通观众发「下一个」不能被当命令吞掉——这是常见口语，必须放行给 LLM。"""

    await handler.execute("before_message_received", _params(_envelope("排队 排位")))
    decision, _ = await handler.execute(
        "before_message_received", _params(_envelope("下一个", guard_level=0))
    )

    assert decision is EventDecision.PASS
    assert hub.manager.get_queue("排位").current is None  # type: ignore[union-attr]


async def test_admin_commands_disabled(
    handler: DanmakuQueueHandler, runtime: QueueRuntime
) -> None:
    """关掉弹幕管理命令后，即使是舰长也放行。"""

    runtime.settings.allow_danmaku_commands = False
    decision, _ = await handler.execute(
        "before_message_received", _params(_envelope("下一个", guard_level=3))
    )
    assert decision is EventDecision.PASS


@pytest.mark.parametrize(
    ("text", "check"),
    [
        ("清空 排位", lambda hub: hub.manager.get_queue("排位").size == 0),
        ("暂停 排位", lambda hub: hub.manager.get_queue("排位").paused is True),
        ("恢复 排位", lambda hub: hub.manager.get_queue("排位").paused is False),
        ("结束当前 排位", lambda hub: hub.manager.get_queue("排位").current is None),
        ("放回 排位", lambda hub: hub.manager.get_queue("排位").current is None),
        ("查看队列", lambda hub: True),
        ("开队 新队", lambda hub: hub.manager.get_queue("新队") is not None),
        ("删队 排位", lambda hub: hub.manager.get_queue("排位") is None),
        ("重置队列", lambda hub: hub.manager.get_queue("排位").size == 0),
    ],
)
async def test_all_admin_commands_are_handled(
    handler: DanmakuQueueHandler, runtime: QueueRuntime, hub: Any, text: str, check: Any
) -> None:
    """所有内置房管命令都要被识别并执行，不抛异常。

    命令里都带上了队名——不带队名时会落到默认队，这是刻意的语义。
    """

    # 注意：live_queue 的 QueueHub 是同步的（异步的是插件侧的 HTTP 客户端）
    hub.join(user_id="u1", nickname="阿狸", queue="排位")
    hub.join(user_id="u2", nickname="小满", queue="排位")
    hub.call_next("排位")
    hub.set_paused("排位", paused=True)

    decision, _ = await handler.execute(
        "before_message_received", _params(_envelope(text, guard_level=1, user_id="admin"))
    )

    assert decision is EventDecision.STOP
    assert check(hub)


async def test_admin_create_without_name_replies_usage(
    handler: DanmakuQueueHandler, runtime: QueueRuntime, sent: list[str]
) -> None:
    """「开队」不写队名时给用法提示。"""

    runtime.settings.reply_in_stream = True
    decision, _ = await handler.execute(
        "before_message_received", _params(_envelope("开队", guard_level=3))
    )

    assert decision is EventDecision.STOP
    assert sent and "开队 排位" in sent[0]


async def test_admin_delete_without_name_replies_usage(
    handler: DanmakuQueueHandler, runtime: QueueRuntime, sent: list[str]
) -> None:
    """「删队」不写队名时给用法提示（且不能误删默认队）。"""

    runtime.settings.reply_in_stream = True
    await handler.execute("before_message_received", _params(_envelope("删队", guard_level=3)))
    assert sent and "队名" in sent[-1]


async def test_admin_next_on_empty_queue_replies_gracefully(
    handler: DanmakuQueueHandler, runtime: QueueRuntime, sent: list[str]
) -> None:
    """空队列（甚至还没有队列）叫号要给可读提示，不抛异常。"""

    runtime.settings.reply_in_stream = True
    decision, _ = await handler.execute(
        "before_message_received", _params(_envelope("下一个", guard_level=3))
    )

    assert decision is EventDecision.STOP
    assert sent
    assert ("空" in sent[0]) or ("找不到" in sent[0])


async def test_admin_next_on_paused_empty_queue(
    handler: DanmakuQueueHandler, runtime: QueueRuntime, hub: Any, sent: list[str]
) -> None:
    """队列存在但没人时叫号提示「空的」。"""

    hub.create_queue("排位")
    runtime.settings.reply_in_stream = True

    await handler.execute(
        "before_message_received", _params(_envelope("下一个 排位", guard_level=3))
    )
    assert sent and "空" in sent[0]
