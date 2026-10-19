"""bilibili_queue 的弹幕回复文案。

数据来自独立 app 的 JSON 返回（``{"status": ..., "message": ..., "queue_title": ...}``），
不再是本地的数据类——队列状态已经迁到 ``live_queue`` 那边了。

大部分操作直接复用 app 给的 ``message``（措辞本来就是面向人的），只有两类需要在这里
加工：**状态查询**（app 没有对应接口，得自己拼）和**入队失败时的引导**（要告诉观众
发什么关键词）。
"""

from __future__ import annotations

from typing import Any

_MAX_STATUS_ROWS = 5
"""状态查询默认最多列出多少人。"""

_FAILED_STATUSES = frozenset(
    {"queue_full", "queue_paused", "queue_not_found", "too_many_queues", "invalid_name"}
)
"""需要给观众补一句「怎么排队」的失败状态。"""


def format_wait(seconds: float) -> str:
    """把等待秒数渲染成中文可读文本。

    Args:
        seconds: 等待秒数。

    Returns:
        形如 ``1分30秒`` 的文本。
    """

    total = int(max(0.0, seconds))
    if total < 60:
        return f"{total}秒"

    minutes, sec = divmod(total, 60)
    if minutes < 60:
        return f"{minutes}分{sec}秒" if sec else f"{minutes}分"

    hours, minute = divmod(minutes, 60)
    return f"{hours}小时{minute}分" if minute else f"{hours}小时"


def join_reply(
    payload: dict[str, Any],
    *,
    join_words: list[str] | None = None,
    queue_names: list[str] | None = None,
) -> str:
    """把入队结果翻译成面向观众的文案。

    Args:
        payload: app 的 ``/api/join`` 返回。
        join_words: 生效的入队关键词，用于失败时提示怎么排队。
        queue_names: 当前已有的队列名，用于提示可以排哪些队。

    Returns:
        回复文案。
    """

    status = str(payload.get("status") or "")
    message = str(payload.get("message") or "").strip()
    word = str(join_words[0]) if join_words else "排队"
    names = [name for name in (queue_names or []) if str(name).strip()]

    if status == "duplicate_event":
        # 同一条弹幕已经被另一个消费者（bot 或独立桥）处理过：不回复、不刷屏
        return ""

    if status == "queue_required":
        example = names[0] if names else "排位"
        tail = "，现有队列：" + "、".join(names[:5]) if names else ""
        return f"请写明要排哪个队，例如「{word} {example}」{tail}"

    if status == "queue_not_found":
        tail = "，现有队列：" + "、".join(names[:5]) if names else ""
        return f"没有这个队列{tail}（不会自动新建）"

    if status in _FAILED_STATUSES and join_words:
        hint = f"（发送「{word} 队名」即可排队）"
        return f"{message}{hint}" if message else f"暂时排不了队{hint}"

    if message:
        return message
    return "已收到你的排队请求"


def leave_reply(payload: dict[str, Any], nickname: str) -> str:
    """把退队结果翻译成文案。

    Args:
        payload: app 的 ``/api/leave`` 返回。
        nickname: 观众昵称。

    Returns:
        回复文案。
    """

    queues = payload.get("queues")
    titles = [str(item) for item in queues] if isinstance(queues, list) else []
    if not titles:
        return f"🤔 {nickname}，你现在不在任何队列里。"
    joined = "、".join(f"「{title}」" for title in titles)
    return f"👋 {nickname} 已退出 {joined}。"


def queue_line(queue: dict[str, Any], *, max_rows: int = _MAX_STATUS_ROWS) -> str:
    """把一个队列渲染成状态查询里的一段。

    Args:
        queue: app 快照里的单个队列。
        max_rows: 最多列出多少人。

    Returns:
        多行文本。
    """

    title = str(queue.get("title") or queue.get("name") or "队列")
    waiting = int(queue.get("waiting") or 0)
    capacity = int(queue.get("capacity") or 0)

    head = f"【{title}】{waiting} 人排队"
    if capacity > 0:
        head += f"（上限 {capacity}）"
    if queue.get("paused"):
        head += "［暂停中］"

    lines = [head]
    current = queue.get("current")
    if isinstance(current, dict):
        lines.append(
            f"  ▶ 当前：{current.get('nickname', '?')}（#{current.get('ticket', '?')}）"
        )

    entries = queue.get("entries")
    rows = list(entries) if isinstance(entries, list) else []
    for index, entry in enumerate(rows[:max_rows], start=1):
        if not isinstance(entry, dict):
            continue
        lines.append(
            f"  {index}. {entry.get('nickname', '?')}（#{entry.get('ticket', '?')}）"
        )

    if waiting > len(rows[:max_rows]):
        lines.append(f"  …… 还有 {waiting - len(rows[:max_rows])} 人")

    if not rows and current is None:
        lines.append("  （空）")

    return "\n".join(lines)


def status_reply(snapshot: dict[str, Any], *, max_rows: int = _MAX_STATUS_ROWS) -> str:
    """把整份快照渲染成状态查询文案。

    Args:
        snapshot: app 的 ``/api/state`` 返回。
        max_rows: 每个队列最多列出多少人。

    Returns:
        多行文本。
    """

    queues = snapshot.get("queues")
    rows = [item for item in queues if isinstance(item, dict)] if isinstance(queues, list) else []
    if not rows:
        return "📭 目前还没有开任何队列，请主播先在控制面板或排队姬窗口里新建一个。"
    return "\n".join(queue_line(item, max_rows=max_rows) for item in rows)


def operation_reply(payload: dict[str, Any], *, fallback: str = "操作完成") -> str:
    """取 app 返回里的提示文案（叫号、结束、放回、清空等都用它）。

    Args:
        payload: app 的返回。
        fallback: 没有 message 时的兜底文案。

    Returns:
        回复文案。
    """

    message = str(payload.get("message") or "").strip()
    return message or fallback


def admin_usage(prefix: str = "") -> str:
    """房管弹幕命令帮助。

    Args:
        prefix: 命令前缀。

    Returns:
        多行帮助文本。
    """

    p = prefix or ""
    return "\n".join(
        [
            "🎛️ 房管排队命令：",
            f"  {p}下一个 [队名]   叫号出队",
            f"  {p}结束 [队名]     结束当前",
            f"  {p}放回 [队名]     把当前放回队首",
            f"  {p}清空 [队名]     清空队列",
            f"  {p}暂停 / {p}恢复 [队名]  暂停或恢复排队",
            f"  {p}查看队列        查看所有队列",
            f"  {p}开队 <队名>     新建队列",
        ]
    )


__all__ = [
    "admin_usage",
    "format_wait",
    "join_reply",
    "leave_reply",
    "operation_reply",
    "queue_line",
    "status_reply",
]
