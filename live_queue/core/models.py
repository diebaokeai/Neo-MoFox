"""live_queue 队列核心数据模型。

本模块只包含纯数据类与纯函数，不依赖任何第三方库（连 PySide6 都不依赖），
因此可以被单测直接覆盖，也可以被窗口线程与 HTTP 线程共用。

结构关系：

- :class:`QueueState` 是一个**命名队列**（例如「排位」「娱乐」），互不干扰。
- :class:`QueueEntry` 是队列里的一个**排队条目**（一个观众）。
- :class:`JoinStatus` 描述一次入队尝试的结果，供上层翻译成提示文案。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class JoinStatus(str, Enum):
    """入队尝试的结果状态。

    继承 ``str`` 是为了序列化（存盘 / HTTP JSON 返回）时直接可读。
    """

    JOINED = "joined"
    """成功入队，排到了队尾。"""

    MOVED = "moved"
    """该用户本来就在队列里，按 ``move_to_end`` 策略被移到了队尾。"""

    ALREADY_IN = "already_in"
    """该用户已经在队列里，按 ``reject`` 策略拒绝重复入队。"""

    QUEUE_FULL = "queue_full"
    """队列人数已达 ``capacity`` 上限。"""

    QUEUE_PAUSED = "queue_paused"
    """队列处于暂停状态，暂时不接受新排队。"""

    QUEUE_NOT_FOUND = "queue_not_found"
    """指定的队列不存在，且当前不允许自动建队。"""

    QUEUE_REQUIRED = "queue_required"
    """没有写明要排的队列，而当前要求必须指定一个已有的队列。"""

    DUPLICATE_EVENT = "duplicate_event"
    """这条平台事件已经处理过了（幂等去重），本次调用没有改动任何队列。"""

    TOO_MANY_QUEUES = "too_many_queues"
    """队列数量已达到 ``max_queues`` 上限，不能再建新队。"""

    INVALID_NAME = "invalid_name"
    """队列名不合法（空、过长或含空白字符）。"""


class DuplicatePolicy(str, Enum):
    """同一用户重复入队时的处理策略。"""

    REJECT = "reject"
    """直接拒绝，告知「你已经排过队了」。"""

    MOVE_TO_END = "move_to_end"
    """把该用户从原位置移到队尾，并按新顺序发号。"""

    ALLOW = "allow"
    """允许同一用户占多个位置（不做去重）。"""


def format_duration(seconds: float) -> str:
    """把秒数格式化成 ``1分30秒`` / ``2小时5分`` 这类中文可读文本。

    Args:
        seconds: 时长（秒），负数按 0 处理。

    Returns:
        中文可读时长；不足 1 分钟时返回 ``N秒``。
    """

    total = int(max(0.0, seconds))
    if total < 60:
        return f"{total}秒"

    minutes, sec = divmod(total, 60)
    if minutes < 60:
        return f"{minutes}分{sec}秒" if sec else f"{minutes}分"

    hours, minute = divmod(minutes, 60)
    return f"{hours}小时{minute}分" if minute else f"{hours}小时"


@dataclass
class QueueEntry:
    """队列中的一个排队条目。

    Attributes:
        user_id: 平台侧用户唯一 ID（B 站弹幕是 ``open_id``，跨直播间稳定）。
        nickname: 展示用昵称。
        joined_at: 入队时间戳（epoch 秒，UTC）。
        platform: 平台标识，例如 ``bilibili_live``。
        ticket: 该队列内单调递增的排队号码，出队后不回收。
        extra: 平台专属附加信息（如 ``guard_level``、``fans_medal_level``）。
    """

    user_id: str
    nickname: str
    joined_at: float
    platform: str = ""
    ticket: int = 0
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def display_name(self) -> str:
        """返回用于展示的名字，昵称为空时退化成 ``用户<ID 前 6 位>``。"""

        if self.nickname:
            return self.nickname
        tail = self.user_id[:6] if self.user_id else "未知"
        return f"用户{tail}"

    def to_dict(self) -> dict[str, Any]:
        """序列化成可 JSON 落盘的 dict。"""

        return {
            "user_id": self.user_id,
            "nickname": self.nickname,
            "joined_at": float(self.joined_at),
            "platform": self.platform,
            "ticket": int(self.ticket),
            "extra": dict(self.extra),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> QueueEntry:
        """从 :meth:`to_dict` 产出的 dict 还原条目。

        Args:
            data: 落盘的结构；缺失字段使用安全默认值，保证旧存档仍可读取。

        Returns:
            还原后的 :class:`QueueEntry`。
        """

        extra = data.get("extra")
        return cls(
            user_id=str(data.get("user_id") or ""),
            nickname=str(data.get("nickname") or ""),
            joined_at=float(data.get("joined_at") or 0.0),
            platform=str(data.get("platform") or ""),
            ticket=int(data.get("ticket") or 0),
            extra=dict(extra) if isinstance(extra, dict) else {},
        )


@dataclass
class QueueState:
    """一个命名队列的完整状态。

    Attributes:
        name: 队列键名（用户输入的那个名字，例如「排位」）。
        display_name: 叠加页展示用名字，为空时回退到 ``name``。
        entries: 正在排队的条目，**队首在 index 0**。
        current: 已经被叫号、正在服务中的条目；也叫「当前」。
        paused: 是否暂停接受新排队。
        capacity: 人数上限，``0`` 表示不限。
        next_ticket: 下一个待发的排队号码。
        served_count: 历史累计叫号次数，用于叠加页展示。
        created_at: 队列创建时间戳。
        updated_at: 最后一次变更时间戳。
    """

    name: str
    display_name: str = ""
    entries: list[QueueEntry] = field(default_factory=list)
    current: QueueEntry | None = None
    paused: bool = False
    capacity: int = 0
    next_ticket: int = 1
    served_count: int = 0
    created_at: float = 0.0
    updated_at: float = 0.0

    @property
    def title(self) -> str:
        """返回展示用队列名。"""

        return self.display_name or self.name

    @property
    def size(self) -> int:
        """返回当前排队人数（不含已在服务中的 ``current``）。"""

        return len(self.entries)

    def position_of(self, user_id: str) -> int:
        """返回用户在队列中的位置（从 1 开始）；不在队列中返回 0。

        Args:

            user_id: 平台用户 ID。
        """

        for index, entry in enumerate(self.entries):
            if entry.user_id == user_id:
                return index + 1
        return 0

    def to_dict(self) -> dict[str, Any]:
        """序列化成可 JSON 落盘的 dict。"""

        return {
            "name": self.name,
            "display_name": self.display_name,
            "entries": [entry.to_dict() for entry in self.entries],
            "current": self.current.to_dict() if self.current is not None else None,
            "paused": bool(self.paused),
            "capacity": int(self.capacity),
            "next_ticket": int(self.next_ticket),
            "served_count": int(self.served_count),
            "created_at": float(self.created_at),
            "updated_at": float(self.updated_at),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> QueueState:
        """从 :meth:`to_dict` 产出的 dict 还原队列。

        Args:
            data: 落盘的结构。

        Returns:
            还原后的 :class:`QueueState`，条目顺序会被保留。
        """

        raw_entries = data.get("entries")
        entries: list[QueueEntry] = []
        if isinstance(raw_entries, list):
            entries = [
                QueueEntry.from_dict(item) for item in raw_entries if isinstance(item, dict)
            ]

        raw_current = data.get("current")
        current = QueueEntry.from_dict(raw_current) if isinstance(raw_current, dict) else None

        name = str(data.get("name") or "")
        return cls(
            name=name,
            display_name=str(data.get("display_name") or ""),
            entries=entries,
            current=current,
            paused=bool(data.get("paused", False)),
            capacity=int(data.get("capacity") or 0),
            next_ticket=int(data.get("next_ticket") or 1),
            served_count=int(data.get("served_count") or 0),
            created_at=float(data.get("created_at") or 0.0),
            updated_at=float(data.get("updated_at") or 0.0),
        )


@dataclass
class JoinResult:
    """一次入队尝试的结果。

    Attributes:
        status: 结果状态。
        queue: 涉及的队列状态（找不到队列时为 ``None``）。
        entry: 本次入队（或已在队中）的条目。
        position: 该条目在队列中的位置（从 1 开始），未知为 0。
    """

    status: JoinStatus
    queue: QueueState | None = None
    entry: QueueEntry | None = None
    position: int = 0

    @property
    def ok(self) -> bool:
        """是否算作「用户现在确实在队列里」。"""

        return self.status in (JoinStatus.JOINED, JoinStatus.MOVED, JoinStatus.ALREADY_IN)


__all__ = [
    "DuplicatePolicy",
    "JoinResult",
    "JoinStatus",
    "QueueEntry",
    "QueueState",
    "format_duration",
]
