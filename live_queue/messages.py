"""live_queue 的提示文案（应用侧）。

窗口、控制面板和 HTTP 返回里的 ``message`` 字段都用这里的措辞。
机器人那边的弹幕回复文案是另一套（见 mofox 插件里的 ``messages.py``），因为
两个受众不同：这里给主播看，那边给观众看。
"""

from __future__ import annotations

from .core.models import JoinResult, JoinStatus, QueueEntry, QueueState, format_duration


def _entry_name(entry: QueueEntry | None) -> str:
    """安全取昵称。

    Args:
        entry: 排队条目或 ``None``。

    Returns:
        展示用昵称。
    """

    return entry.display_name if entry is not None else "这位观众"


def join_message(result: JoinResult) -> str:
    """把入队结果翻译成一句提示。

    Args:
        result: 入队结果。

    Returns:
        提示文案。
    """

    title = result.queue.title if result.queue is not None else "队列"

    if result.status is JoinStatus.JOINED:
        ahead = max(0, result.position - 1)
        suffix = "，你是第 1 位" if ahead == 0 else f"，前面还有 {ahead} 位"
        return f"已加入「{title}」{suffix}"
    if result.status is JoinStatus.MOVED:
        return f"已移到「{title}」队尾，现在第 {result.position} 位"
    if result.status is JoinStatus.ALREADY_IN:
        return f"已经在「{title}」里了（第 {result.position} 位）"
    if result.status is JoinStatus.QUEUE_FULL:
        return f"「{title}」已排满"
    if result.status is JoinStatus.QUEUE_PAUSED:
        return f"「{title}」暂停排队中"
    if result.status is JoinStatus.DUPLICATE_EVENT:
        return "这条消息已经处理过了（重复事件已忽略）"
    if result.status is JoinStatus.QUEUE_REQUIRED:
        return "请写明要排哪个队列，例如：排队 排位"
    if result.status is JoinStatus.QUEUE_NOT_FOUND:
        return "没有这个队列（不会自动新建）"
    if result.status is JoinStatus.TOO_MANY_QUEUES:
        return "队列数量已达上限"
    return "队名不合法（1-16 字，不能含空白）"


def called_message(entry: QueueEntry, queue: QueueState) -> str:
    """叫号提示。

    Args:
        entry: 被叫到的条目。
        queue: 所属队列。

    Returns:
        提示文案。
    """

    return f"叫到 {entry.display_name}（{queue.title} #{entry.ticket}）"


def finished_message(entry: QueueEntry, queue: QueueState) -> str:
    """结束当前服务对象的提示。

    Args:
        entry: 被结束的条目。
        queue: 所属队列。

    Returns:
        提示文案。
    """

    return f"{entry.display_name}（{queue.title} #{entry.ticket}）已完成"


def requeued_message(entry: QueueEntry, queue: QueueState) -> str:
    """放回队首的提示。

    Args:
        entry: 被放回的条目。
        queue: 所属队列。

    Returns:
        提示文案。
    """

    return f"已把 {entry.display_name} 放回「{queue.title}」队首"


def waiting_text(seconds: float) -> str:
    """把等待时长渲染成紧凑文本。

    Args:
        seconds: 等待秒数。

    Returns:
        形如 ``1分30秒`` 的文本。
    """

    return format_duration(seconds)


__all__ = [
    "called_message",
    "finished_message",
    "join_message",
    "requeued_message",
    "waiting_text",
]
