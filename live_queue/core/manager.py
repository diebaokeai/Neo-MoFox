"""live_queue 队列调度核心。

:class:`QueueManager` 是**纯内存**的多命名队列调度器：不碰 IO、不 await、
不依赖第三方库，因此可以被单测直接覆盖。

线程模型：管理器自身**不加锁**，线程安全由上层 :class:`live_queue.hub.QueueHub`
统一负责（窗口线程与 HTTP 线程都会经由 hub 访问队列）。把锁集中在一处，
比散落在每个方法里更容易看清临界区边界。

持久化方式：管理器的每次变更都会触发 ``on_change`` 回调，回调由外层注入
（通常是「标脏 + 定时落盘」）。管理器本身不关心存储长什么样。
"""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any

from .models import (
    DuplicatePolicy,
    JoinResult,
    JoinStatus,
    QueueEntry,
    QueueState,
)

_MAX_NAME_LENGTH = 16
"""队列名最大长度（字符）。"""


class QueueManager:
    """多命名队列调度器。

    所有公开方法都是同步的：内部的增删改查都是几步纯 Python 操作，没有 await
    点，因此在同一个事件循环里被弹幕处理器和 HTTP 端点并发调用也不会撕裂状态。
    """

    def __init__(
        self,
        *,
        default_queue: str = "默认",
        max_queues: int = 8,
        default_capacity: int = 0,
        duplicate_policy: DuplicatePolicy | str = DuplicatePolicy.REJECT,
        auto_create_queue: bool = False,
        require_explicit_queue: bool = True,
        aliases: dict[str, str] | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        """初始化空的管理器。

        Args:
            default_queue: 用户没指定队名时落到哪个队列。
            max_queues: 允许同时存在的队列数量上限；``<=0`` 表示不限。
            default_capacity: 新建队列默认人数上限；``0`` 表示不限。
            duplicate_policy: 同一用户重复入队时的策略。
            auto_create_queue: 用户排一个不存在的队时是否自动建队。
            require_explicit_queue: 入队是否必须写明队名；为真时不回落默认队。
            aliases: 别名 → 正式队名 的映射，例如 ``{"排位赛": "排位"}``。
            clock: 取当前时间戳的函数，默认 :func:`time.time`，测试可注入。
        """

        self._default_queue = self._clean(default_queue) or "默认"
        self._max_queues = int(max_queues)
        self._default_capacity = max(0, int(default_capacity))
        self._duplicate_policy = (
            duplicate_policy
            if isinstance(duplicate_policy, DuplicatePolicy)
            else DuplicatePolicy(str(duplicate_policy))
        )
        self._auto_create_queue = bool(auto_create_queue)
        self._require_explicit_queue = bool(require_explicit_queue)
        self._aliases: dict[str, str] = {
            str(key): str(value) for key, value in (aliases or {}).items()
        }
        self._clock = clock
        self._queues: dict[str, QueueState] = {}
        self._guild: dict[str, str] = {}
        """queue_name -> 归属标识（例如直播间号），用于多直播间隔离。"""

        self.on_change: Callable[[QueueManager], None] | None = None
        """状态变更回调，由外层注入用于持久化。"""

    # ── 基础设施 ──────────────────────────────────────

    @staticmethod
    def _clean(raw: Any) -> str:
        """把任意输入规整成候选队名（去首尾空白）。

        Args:
            raw: 原始输入。

        Returns:
            规整后的字符串；``None`` 等非法输入返回空字符串。
        """

        if raw is None:
            return ""
        return str(raw).strip()

    def is_valid_name(self, name: str) -> bool:
        """判断队名是否合法（非空、未超长、不含空白）。

        Args:
            name: 已规整的候选队名。

        Returns:
            合法返回 ``True``。
        """

        if not name or len(name) > _MAX_NAME_LENGTH:
            return False
        return not any(ch.isspace() for ch in name)

    def resolve_name(self, raw: Any = "") -> str:
        """把用户输入解析成真实队名（处理默认队与别名）。

        Args:
            raw: 用户输入的队名，可为空。

        Returns:
            解析后的队名；输入非法时返回空字符串。
        """

        name = self._clean(raw)
        if not name:
            return self._default_queue
        name = self._aliases.get(name, name)
        return name if self.is_valid_name(name) else ""

    @property
    def default_queue(self) -> str:
        """默认队名。"""

        return self._default_queue

    @property
    def require_explicit_queue(self) -> bool:
        """入队是否必须写明一个已存在的队名。"""

        return self._require_explicit_queue

    def configure(
        self,
        *,
        default_queue: str | None = None,
        max_queues: int | None = None,
        default_capacity: int | None = None,
        duplicate_policy: DuplicatePolicy | str | None = None,
        auto_create_queue: bool | None = None,
        require_explicit_queue: bool | None = None,
        aliases: dict[str, str] | None = None,
    ) -> None:
        """热更新可变策略，供配置重载使用。

        只影响**后续**操作，不会重排已有队列。

        Args:
            default_queue: 新的默认队名；空值表示不修改。
            max_queues: 新的队列数量上限。
            default_capacity: 新建队列的默认人数上限。
            duplicate_policy: 新的重复入队策略。
            auto_create_queue: 是否允许自动建队。
            require_explicit_queue: 是否要求写明队名。
            aliases: 新的别名映射，整体替换。
        """

        if default_queue is not None and self._clean(default_queue):
            self._default_queue = self._clean(default_queue)
        if max_queues is not None:
            self._max_queues = int(max_queues)
        if default_capacity is not None:
            self._default_capacity = max(0, int(default_capacity))
        if duplicate_policy is not None:
            self._duplicate_policy = (
                duplicate_policy
                if isinstance(duplicate_policy, DuplicatePolicy)
                else DuplicatePolicy(str(duplicate_policy))
            )
        if auto_create_queue is not None:
            self._auto_create_queue = bool(auto_create_queue)
        if require_explicit_queue is not None:
            self._require_explicit_queue = bool(require_explicit_queue)
        if aliases is not None:
            self._aliases = {str(key): str(value) for key, value in aliases.items()}

    def _touch(self, queue: QueueState) -> None:
        """刷新队列时间戳并广播变更。

        Args:
            queue: 发生变化的队列。
        """

        queue.updated_at = self._clock()
        self._notify()

    def _notify(self) -> None:
        """触发变更回调；回调异常不应打断业务逻辑。"""

        callback = self.on_change
        if callback is None:
            return
        callback(self)

    # ── 队列生命周期 ──────────────────────────────────

    def create_queue(
        self,
        name: str,
        *,
        display_name: str = "",
        capacity: int | None = None,
        guild: str = "",
    ) -> QueueState | None:
        """显式创建一个队列。

        Args:
            name: 队名（应已通过 :meth:`resolve_name` 解析）。
            display_name: 展示名；留空则与 ``name`` 相同。
            capacity: 人数上限；``None`` 表示用 ``default_capacity``。
            guild: 归属标识（通常传直播间号）。

        Returns:
            创建好的队列；队名非法或已达数量上限时返回 ``None``。
        """

        if not self.is_valid_name(name):
            return None
        existing = self._queues.get(name)
        if existing is not None:
            return existing
        if self._max_queues > 0 and len(self._queues) >= self._max_queues:
            return None

        now = self._clock()
        queue = QueueState(
            name=name,
            display_name=display_name or name,
            capacity=self._default_capacity if capacity is None else max(0, int(capacity)),
            created_at=now,
            updated_at=now,
        )
        self._queues[name] = queue
        if guild:
            self._guild[name] = guild
        self._notify()
        return queue

    def ensure_queue(self, name: str, *, guild: str = "") -> QueueState | None:
        """取队列，不存在则按 ``auto_create_queue`` 决定是否自动建队。

        Args:
            name: 队名。
            guild: 归属标识，仅在自动建队时使用。

        Returns:
            队列对象；不允许建队且不存在时返回 ``None``。
        """

        queue = self._queues.get(name)
        if queue is not None:
            return queue
        if not self._auto_create_queue:
            return None
        return self.create_queue(name, guild=guild)

    def delete_queue(self, name: str) -> bool:
        """删除一个队列及其全部排队条目。

        Args:
            name: 队名。

        Returns:
            确实删除了返回 ``True``。
        """

        if name not in self._queues:
            return False
        del self._queues[name]
        self._guild.pop(name, None)
        self._notify()
        return True

    def get_queue(self, name: str) -> QueueState | None:
        """按队名取队列。

        Args:
            name: 队名（已解析）。

        Returns:
            队列对象或 ``None``。
        """

        return self._queues.get(name)

    def queues(self) -> list[QueueState]:
        """按创建顺序返回所有队列。"""

        return list(self._queues.values())

    @property
    def guild_of(self) -> dict[str, str]:
        """返回 队名 → 归属标识 的只读副本。"""

        return dict(self._guild)

    # ── 排队操作 ──────────────────────────────────────

    def join(
        self,
        name: str,
        *,
        user_id: str,
        nickname: str = "",
        platform: str = "",
        extra: dict[str, Any] | None = None,
        guild: str = "",
    ) -> JoinResult:
        """把一个用户加入指定队列的队尾。

        Args:
            name: 队名（已解析）。
            user_id: 平台用户 ID。
            nickname: 展示用昵称。
            platform: 平台标识。
            extra: 平台附加信息。
            guild: 归属标识，仅在自动建队时使用。

        Returns:
            :class:`JoinResult`，``status`` 说明本次尝试的结果。
        """

        if not self.is_valid_name(name):
            return JoinResult(status=JoinStatus.INVALID_NAME)

        queue = self.ensure_queue(name, guild=guild)
        if queue is None:
            status = (
                JoinStatus.TOO_MANY_QUEUES
                if self._max_queues > 0 and len(self._queues) >= self._max_queues
                else JoinStatus.QUEUE_NOT_FOUND
            )
            return JoinResult(status=status)

        if queue.paused:
            return JoinResult(status=JoinStatus.QUEUE_PAUSED, queue=queue)

        existing_index = -1
        if self._duplicate_policy is not DuplicatePolicy.ALLOW:
            for index, entry in enumerate(queue.entries):
                if entry.user_id == user_id:
                    existing_index = index
                    break

        if existing_index >= 0 and self._duplicate_policy is DuplicatePolicy.REJECT:
            entry = queue.entries[existing_index]
            return JoinResult(
                status=JoinStatus.ALREADY_IN,
                queue=queue,
                entry=entry,
                position=existing_index + 1,
            )

        if queue.capacity > 0 and queue.size >= queue.capacity and existing_index < 0:
            return JoinResult(status=JoinStatus.QUEUE_FULL, queue=queue)

        if existing_index >= 0:
            # move_to_end：把原条目挪到队尾并重新发号。
            entry = queue.entries.pop(existing_index)
            entry.ticket = queue.next_ticket
            queue.next_ticket += 1
            queue.entries.append(entry)
            self._touch(queue)
            return JoinResult(
                status=JoinStatus.MOVED,
                queue=queue,
                entry=entry,
                position=len(queue.entries),
            )

        entry = QueueEntry(
            user_id=user_id,
            nickname=nickname,
            joined_at=self._clock(),
            platform=platform,
            ticket=queue.next_ticket,
            extra=dict(extra or {}),
        )
        queue.next_ticket += 1
        queue.entries.append(entry)
        self._touch(queue)
        return JoinResult(
            status=JoinStatus.JOINED,
            queue=queue,
            entry=entry,
            position=len(queue.entries),
        )

    def leave(self, name: str, user_id: str) -> QueueEntry | None:
        """把一个用户从队列中移除。

        Args:
            name: 队名。
            user_id: 平台用户 ID。

        Returns:
            被移除的条目；用户不在队列或队里返回 ``None``。
        """

        queue = self._queues.get(name)
        if queue is None:
            return None
        for index, entry in enumerate(queue.entries):
            if entry.user_id == user_id:
                queue.entries.pop(index)
                self._touch(queue)
                return entry
        if queue.current is not None and queue.current.user_id == user_id:
            entry = queue.current
            queue.current = None
            self._touch(queue)
            return entry
        return None

    def call_next(self, name: str) -> QueueEntry | None:
        """叫号：把队首取出并置为「当前服务中」。

        上一次的 ``current`` 会被直接丢弃（视为已服务完毕）。

        Args:
            name: 队名。

        Returns:
            被叫到的条目；队列为空返回 ``None``。
        """

        queue = self._queues.get(name)
        if queue is None or not queue.entries:
            return None
        entry = queue.entries.pop(0)
        queue.current = entry
        queue.served_count += 1
        self._touch(queue)
        return entry

    def requeue_current(self, name: str) -> QueueEntry | None:
        """把「当前服务中」的人放回队首（叫错了 / 人还没到）。

        Args:
            name: 队名。

        Returns:
            被放回的条目；没有 ``current`` 时返回 ``None``。
        """

        queue = self._queues.get(name)
        if queue is None or queue.current is None:
            return None
        entry = queue.current
        queue.current = None
        queue.entries.insert(0, entry)
        queue.served_count = max(0, queue.served_count - 1)
        if queue.next_ticket <= entry.ticket:
            queue.next_ticket = entry.ticket + 1
        self._touch(queue)
        return entry

    def finish_current(self, name: str) -> QueueEntry | None:
        """结束「当前服务中」的人，进入空档状态。

        Args:
            name: 队名。

        Returns:
            被结束的条目；本来就没有 ``current`` 时返回 ``None``。
        """

        queue = self._queues.get(name)
        if queue is None or queue.current is None:
            return None
        entry = queue.current
        queue.current = None
        self._touch(queue)
        return entry

    def clear(self, name: str) -> int:
        """清空一个队列的等待列表与当前条目。

        Args:
            name: 队名。

        Returns:
            被清掉的条目总数；队列不存在返回 ``0``。
        """

        queue = self._queues.get(name)
        if queue is None:
            return 0
        removed = len(queue.entries) + (1 if queue.current is not None else 0)
        queue.entries.clear()
        queue.current = None
        self._touch(queue)
        return removed

    def move_to_front(self, name: str, user_id: str) -> bool:
        """把队列中的某人插到队首（房管手动插队）。

        Args:
            name: 队名。
            user_id: 平台用户 ID。

        Returns:
            确实移动了返回 ``True``。
        """

        queue = self._queues.get(name)
        if queue is None:
            return False
        for index, entry in enumerate(queue.entries):
            if entry.user_id == user_id:
                queue.entries.insert(0, queue.entries.pop(index))
                self._touch(queue)
                return True
        return False

    def set_paused(self, name: str, paused: bool) -> bool:
        """设置队列的暂停状态。

        Args:
            name: 队名。
            paused: ``True`` 表示暂停接受排队。

        Returns:
            设置成功返回 ``True``。
        """

        queue = self._queues.get(name)
        if queue is None:
            return False
        if queue.paused != paused:
            queue.paused = paused
            self._touch(queue)
        return True

    def set_capacity(self, name: str, capacity: int) -> bool:
        """设置队列人数上限。

        Args:
            name: 队名。
            capacity: 上限，``0`` 表示不限。

        Returns:
            设置成功返回 ``True``。
        """

        queue = self._queues.get(name)
        if queue is None:
            return False
        queue.capacity = max(0, int(capacity))
        self._touch(queue)
        return True

    # ── 查询与维护 ────────────────────────────────────

    def find_queues_of(self, user_id: str) -> list[str]:
        """找出某个用户当前所在的全部队列名。

        Args:
            user_id: 平台用户 ID。

        Returns:
            队列名列表（按队列创建顺序）。
        """

        return [
            queue.name
            for queue in self._queues.values()
            if queue.position_of(user_id) > 0
            or (queue.current is not None and queue.current.user_id == user_id)
        ]

    def purge_expired(self, timeout: float, *, now: float | None = None) -> list[QueueEntry]:
        """清理排队超时的条目。

        Args:
            timeout: 超时秒数；``<=0`` 表示不清理。
            now: 当前时间戳；``None`` 时取 ``clock()``。

        Returns:
            被清理掉的条目列表。
        """

        if timeout <= 0 or not self._queues:
            return []
        moment = self._clock() if now is None else now
        removed: list[QueueEntry] = []
        for queue in self._queues.values():
            kept: list[QueueEntry] = []
            for entry in queue.entries:
                if moment - entry.joined_at >= timeout:
                    removed.append(entry)
                else:
                    kept.append(entry)
            if len(kept) != len(queue.entries):
                queue.entries = kept
                queue.updated_at = moment
        if removed:
            self._notify()
        return removed

    def total_waiting(self) -> int:
        """返回所有队列的等待人数之和。"""

        return sum(queue.size for queue in self._queues.values())

    # ── 序列化 ────────────────────────────────────────

    def to_dict(self) -> dict[str, Any]:
        """把全部队列状态导出成可 JSON 落盘的 dict。"""

        return {
            "version": 1,
            "default_queue": self._default_queue,
            "guild": dict(self._guild),
            "queues": [queue.to_dict() for queue in self._queues.values()],
        }

    def load_dict(self, data: dict[str, Any] | None) -> None:
        """用落盘数据覆盖当前状态。

        Args:
            data: :meth:`to_dict` 产出的结构；``None`` 或结构异常时保持为空。
        """

        self._queues.clear()
        self._guild.clear()
        if not isinstance(data, dict):
            return

        raw_guild = data.get("guild")
        if isinstance(raw_guild, dict):
            self._guild = {str(key): str(value) for key, value in raw_guild.items()}

        raw_queues = data.get("queues")
        if not isinstance(raw_queues, list):
            return
        for item in raw_queues:
            if not isinstance(item, dict):
                continue
            queue = QueueState.from_dict(item)
            if queue.name:
                self._queues[queue.name] = queue

    def reset(self) -> None:
        """清空全部内存状态（不写入存储）。"""

        self._queues.clear()
        self._guild.clear()


__all__ = ["QueueManager"]
