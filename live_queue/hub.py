"""live_queue 的线程安全服务层。

**这个应用里所有队列访问都必须经过 :class:`QueueHub`。** 原因是它有两条并发路径：

1. **窗口线程**（Qt 主线程）：主播点「叫号 / 清空」按钮；
2. **HTTP 线程**（``ThreadingHTTPServer`` 每个请求一个线程）：机器人把弹幕观众加进
   队列、OBS 叠加页拉状态、控制面板操作。

:class:`~live_queue.core.manager.QueueManager` 自己不加锁，锁集中在 hub 里，临界区
边界一眼可见。

变更通知：每次队列真的变了，hub 会递增 ``revision``、标脏（等待落盘）并调用所有
监听者。监听者是在**持有锁的线程里**被同步调用的，所以必须是非阻塞的——窗口那边
的做法是发一个 Qt 信号（跨线程队列投递，不会阻塞）。
"""

from __future__ import annotations

import threading
import time
import uuid
from collections import OrderedDict
from collections.abc import Callable
from typing import Any

from . import __version__
from .config import AppConfig
from .core.manager import QueueManager
from .core.models import DuplicatePolicy, JoinResult, JoinStatus, QueueEntry, QueueState
from .messages import called_message, finished_message, join_message, requeued_message
from .store import QueueStore, StoreError

_TICK_SECONDS = 0.5
"""维护线程的检查节拍（秒）。"""


class QueueHub:
    """队列的操作总线：查询、变更、持久化、变更通知。"""

    def __init__(
        self,
        *,
        config: AppConfig,
        store: QueueStore,
        logger: Any = None,
    ) -> None:
        """构造 hub。

        Args:
            config: 应用配置。
            store: 状态存储。
            logger: 可选日志器（标准库 ``logging.Logger`` 或兼容对象）。
        """

        self._config = config
        self._store = store
        self._logger = logger
        self._lock = threading.RLock()
        self._revision = 0
        self._dirty = False
        self._started_at = time.time()
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None

        self._manager = QueueManager(
            default_queue=config.queue.default_queue,
            max_queues=config.queue.max_queues,
            default_capacity=config.queue.capacity,
            duplicate_policy=_policy_of(config.queue.duplicate_policy),
            auto_create_queue=config.queue.auto_create_queue,
            require_explicit_queue=config.queue.require_explicit_queue,
            aliases=_aliases_of(config.queue.aliases),
        )
        self._manager.on_change = self._on_manager_change
        self._listeners: list[Callable[[], None]] = []
        self._event_dedupe = _EventDedupe(
            ttl=config.queue.event_dedupe_seconds,
            capacity=config.queue.event_dedupe_capacity,
        )

    # ── 只读属性 ──────────────────────────────────────

    @property
    def config(self) -> AppConfig:
        """当前配置。"""

        return self._config

    @property
    def store(self) -> QueueStore:
        """状态存储。"""

        return self._store

    @property
    def manager(self) -> QueueManager:
        """底层队列管理器（**调用方需自行注意线程安全**，一般用不到）。"""

        return self._manager

    @property
    def revision(self) -> int:
        """状态版本号，每次变更自增。"""

        with self._lock:
            return self._revision

    @property
    def has_pending_changes(self) -> bool:
        """是否有尚未落盘的改动。"""

        with self._lock:
            return self._dirty

    # ── 生命周期 ──────────────────────────────────────

    def start(self) -> None:
        """加载历史状态并启动维护线程。"""

        with self._lock:
            try:
                payload = self._store.load()
            except StoreError as exc:
                self._log("warning", f"读取状态失败，按空队列启动：{exc}")
                payload = None

            if payload is None:
                self._log("info", "没有历史队列状态，从空队列开始")
            else:
                self._manager.load_dict(payload)
                self._revision = int(payload.get("revision") or 0)
                self._log(
                    "info",
                    f"已恢复 {len(self._manager.queues())} 个队列"
                    f"（{self._manager.total_waiting()} 人等待）",
                )

        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._maintenance_loop, name="live_queue_maintenance", daemon=True
        )
        self._thread.start()

    def stop(self, *, timeout: float = 5.0) -> None:
        """停止维护线程并做最后一次落盘。

        Args:
            timeout: 等待维护线程退出的秒数。
        """

        self._stop_event.set()
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=timeout)
        self._thread = None
        self.flush(force=True)

    def add_listener(self, callback: Callable[[], None]) -> None:
        """注册状态变更回调。

        Args:
            callback: 无参可调用对象；必须是**非阻塞**的（在持有锁的线程里同步调用）。
        """

        with self._lock:
            if callback not in self._listeners:
                self._listeners.append(callback)

    def remove_listener(self, callback: Callable[[], None]) -> None:
        """注销状态变更回调。

        Args:
            callback: 之前注册的回调。
        """

        with self._lock:
            if callback in self._listeners:
                self._listeners.remove(callback)

    # ── 变更与持久化 ──────────────────────────────────

    def _on_manager_change(self, _manager: QueueManager) -> None:
        """队列管理器回调：推进版本号、标脏、通知监听者。

        Args:
            _manager: 触发变更的管理器（未使用）。
        """

        self._revision += 1
        self._dirty = True
        for callback in list(self._listeners):
            try:
                callback()
            except Exception as exc:  # 监听者出错不能影响队列操作
                self._log("error", f"状态变更回调执行失败：{exc}")

    def flush(self, *, force: bool = False) -> bool:
        """把内存状态落盘。

        快照在锁内取，写盘在锁外做——写盘是磁盘 IO，不该把窗口和 HTTP 请求卡住。

        Args:
            force: 没有脏标记也强制写一次（例如退出前）。

        Returns:
            真的写了盘返回 ``True``。
        """

        with self._lock:
            if not force and not self._dirty:
                return False
            payload = self._manager.to_dict()
            payload["revision"] = self._revision
            payload["saved_at"] = time.time()
            self._dirty = False

        try:
            self._store.save(payload)
        except StoreError as exc:
            with self._lock:
                self._dirty = True  # 重标脏，下一轮重试
            self._log("error", f"保存队列状态失败：{exc}")
            return False
        return True

    def _maintenance_loop(self) -> None:
        """后台维护：定时清理超时条目并落盘脏数据。"""

        last_flush = time.time()
        last_purge = time.time()

        while not self._stop_event.wait(_TICK_SECONDS):
            now = time.time()
            try:
                timeout = float(self._config.queue.entry_timeout)
                if timeout > 0 and now - last_purge >= max(1.0, self._config.server.purge_interval):
                    last_purge = now
                    with self._lock:
                        removed = self._manager.purge_expired(timeout, now=now)
                    if removed:
                        self._log(
                            "info",
                            f"排队超时自动出队 {len(removed)} 人："
                            + "、".join(entry.display_name for entry in removed[:5]),
                        )

                if now - last_flush >= max(1.0, self._config.server.flush_interval):
                    last_flush = now
                    self.flush()
            except Exception as exc:  # 维护线程必须活着
                self._log("error", f"维护循环出错（已跳过本轮）：{exc}")

    def _log(self, level: str, message: str) -> None:
        """按级别输出日志（没有日志器时静默）。

        Args:
            level: ``debug`` / ``info`` / ``warning`` / ``error``。
            message: 日志正文。
        """

        if self._logger is None:
            return
        emit = getattr(self._logger, level, None)
        if callable(emit):
            emit(message)

    # ── 队名解析 ──────────────────────────────────────

    def resolve_queue(self, raw: Any, *, explicit: bool) -> str:
        """把外部传来的队名落到真实队名上。

        Args:
            raw: 原始队名（可能是别名或空）。
            explicit: 调用方是否**明确**指定了队名（例如弹幕里写了分隔符）。
                为 ``False`` 时，只有命中已存在的队列才认，否则回到默认队——
                避免把「排队一下」这种闲聊解读成队名「一下」。

        Returns:
            真实队名；队名非法时返回空字符串。
        """

        with self._lock:
            name = self._manager.resolve_name(raw)
            if not name:
                return ""
            if self._manager.get_queue(name) is not None:
                return name
            return name if explicit else self._manager.default_queue

    def resolve_alias(self, raw: Any) -> str:
        """只做别名 / 默认队解析，不检查队列是否存在。

        Args:
            raw: 原始队名。

        Returns:
            解析后的队名；非法时为空字符串。
        """

        with self._lock:
            return self._manager.resolve_name(raw)

    # ── 查询 ──────────────────────────────────────────

    def snapshot(self, *, queue: str = "", limit: int = 0) -> dict[str, Any]:
        """生成完整状态快照。

        Args:
            queue: 只返回这个队列（支持别名）；留空返回全部。
            limit: 每个队列最多返回多少条目；``0`` 表示不截断。

        Returns:
            JSON 可序列化的 dict。
        """

        with self._lock:
            moment = time.time()
            queues = self._manager.queues()
            if queue:
                wanted = self._manager.resolve_name(queue)
                queues = [item for item in queues if item.name == wanted]

            order = [str(name) for name in self._config.overlay.queues]
            if order:
                rank = {name: index for index, name in enumerate(order)}
                queues = sorted(queues, key=lambda item: rank.get(item.name, len(order)))

            payloads = []
            for item in queues:
                payload = self._queue_payload(item, moment=moment)
                if limit > 0:
                    payload["entries"] = payload["entries"][:limit]
                payloads.append(payload)

            return {
                "ok": True,
                "revision": self._revision,
                "updated_at": round(moment, 3),
                "total_waiting": self._manager.total_waiting(),
                "queues": payloads,
                "view": self._overlay_view(),
            }

    def _overlay_view(self) -> dict[str, Any]:
        """返回叠加页 / 窗口的外观配置（调用方需持有锁）。

        Returns:
            外观配置 dict。
        """

        overlay = self._config.overlay
        return {
            "title": overlay.title,
            "show_title": overlay.show_title,
            "queues": list(overlay.queues),
            "max_rows": overlay.max_rows,
            "poll_interval_ms": overlay.poll_interval_ms,
            "show_current": overlay.show_current,
            "show_wait_time": overlay.show_wait_time,
            "accent_color": overlay.accent_color,
            "text_color": overlay.text_color,
            "panel_bg": overlay.panel_bg,
            "panel_border": overlay.panel_border,
            "current_bg": overlay.current_bg,
            "title_size": overlay.title_size,
            "name_size": overlay.name_size,
        }

    def overlay_config(self) -> dict[str, Any]:
        """叠加页需要的展示配置（独立接口用）。

        Returns:
            含 ``overlay`` 与 ``queues`` 的 dict。
        """

        with self._lock:
            return {
                "overlay": self._overlay_view(),
                "queues": [str(name) for name in self._config.overlay.queues],
                "default_queue": self._manager.default_queue,
            }

    def health(self) -> dict[str, Any]:
        """健康检查信息（机器人靠它判断应用是否就绪）。

        Returns:
            含版本、运行时长、队列统计的 dict。
        """

        with self._lock:
            return {
                "ok": True,
                "app": "live_queue",
                "version": __version__,
                "revision": self._revision,
                "uptime": round(time.time() - self._started_at, 1),
                "queue_count": len(self._manager.queues()),
                "total_waiting": self._manager.total_waiting(),
            }

    def queue_names(self) -> list[str]:
        """返回全部队列名。

        Returns:
            队列名列表（按创建顺序）。
        """

        with self._lock:
            return [queue.name for queue in self._manager.queues()]

    def queues_of(self, user_id: str) -> list[str]:
        """查询某个用户在哪些队列里。

        Args:
            user_id: 平台用户 ID。

        Returns:
            队列名列表。
        """

        with self._lock:
            return self._manager.find_queues_of(user_id)

    def _queue_payload(self, queue: QueueState, *, moment: float) -> dict[str, Any]:
        """把一个队列序列化成 JSON（调用方需持有锁）。

        Args:
            queue: 队列状态。
            moment: 当前时间戳。

        Returns:
            JSON 可序列化的 dict。
        """

        return {
            "name": queue.name,
            "title": queue.title,
            "paused": queue.paused,
            "capacity": queue.capacity,
            "waiting": queue.size,
            "served_count": queue.served_count,
            "next_ticket": queue.next_ticket,
            "current": (
                self._entry_payload(queue.current, position=0, moment=moment)
                if queue.current is not None
                else None
            ),
            "entries": [
                self._entry_payload(entry, position=index, moment=moment)
                for index, entry in enumerate(queue.entries, start=1)
            ],
        }

    @staticmethod
    def _entry_payload(entry: QueueEntry, *, position: int, moment: float) -> dict[str, Any]:
        """把一个条目序列化成 JSON。

        Args:
            entry: 排队条目。
            position: 队列内位置（从 1 开始）；0 表示不适用。
            moment: 当前时间戳。

        Returns:
            JSON 可序列化的 dict。
        """

        return {
            "user_id": entry.user_id,
            "nickname": entry.display_name,
            "ticket": entry.ticket,
            "position": position,
            "waiting_seconds": round(max(0.0, moment - entry.joined_at), 1),
            "platform": entry.platform,
        }

    # ── 观众操作 ──────────────────────────────────────

    def join(
        self,
        *,
        user_id: str,
        nickname: str = "",
        platform: str = "",
        queue: str = "",
        queue_explicit: bool = True,
        extra: dict[str, Any] | None = None,
        guild: str = "",
        event_id: str = "",
    ) -> dict[str, Any]:
        """让一个观众入队。

        Args:
            user_id: 平台用户 ID。
            nickname: 昵称。
            platform: 平台标识。
            queue: 队名（原始输入，支持别名）。
            queue_explicit: 队名是否是调用方明确指定的。
            extra: 平台附加信息（如 ``guard_level``）。
            guild: 归属标识（直播间号 / 群号）。
            event_id: 平台事件 ID（B 站弹幕是 msg_id），用于幂等；重复事件直接忽略。

        Returns:
            结果 dict，含 ``ok`` / ``status`` / ``message`` / ``position`` / ``ticket``。
        """

        with self._lock:
            if event_id and self._event_dedupe.seen(f"join:{platform}:{event_id}"):
                # 同一条弹幕已经被别的消费者处理过（例如 bot 与独立桥同时在线）
                return self._join_payload(JoinResult(status=JoinStatus.DUPLICATE_EVENT))
            raw = str(queue or "").strip()
            named = self._manager.resolve_name(raw)
            exists = bool(named) and self._manager.get_queue(named) is not None
            if self._manager.require_explicit_queue and not exists and not (queue_explicit and raw):
                # 新规则：必须写明一个已存在的队名——既不回落默认队，也不自动建队。
                return self._join_payload(JoinResult(status=JoinStatus.QUEUE_REQUIRED))
            name = self.resolve_queue(queue, explicit=queue_explicit)
            if not name:
                return self._join_payload(JoinResult(status=JoinStatus.INVALID_NAME))
            result = self._manager.join(
                name,
                user_id=user_id,
                nickname=nickname,
                platform=platform,
                extra=extra,
                guild=guild,
            )
            return self._join_payload(result)

    def leave(
        self,
        *,
        user_id: str,
        queue: str = "",
        queue_explicit: bool = False,
        event_id: str = "",
    ) -> dict[str, Any]:
        """让一个观众退队。

        Args:
            user_id: 平台用户 ID。
            queue: 指定队名；留空表示退出他所在的全部队列。
            queue_explicit: 队名是否是调用方明确指定的。
            event_id: 平台事件 ID，用于幂等；重复事件直接忽略。

        Returns:
            结果 dict，``queues`` 为实际退出的队列展示名列表。
        """

        with self._lock:
            if event_id and self._event_dedupe.seen(f"leave:{event_id}"):
                return {
                    "ok": False,
                    "status": "duplicate_event",
                    "queues": [],
                    "message": "这条消息已经处理过了（重复事件已忽略）",
                }
            names: list[str] = []
            if queue:
                resolved = self.resolve_queue(queue, explicit=queue_explicit)
                if resolved and self._manager.get_queue(resolved) is not None:
                    names = [resolved]
            else:
                names = self._manager.find_queues_of(user_id)

            titles: list[str] = []
            for name in names:
                target = self._manager.get_queue(name)
                entry = self._manager.leave(name, user_id)
                if entry is not None and target is not None:
                    titles.append(target.title)

            return {
                "ok": bool(titles),
                "status": "left" if titles else "not_queued",
                "queues": titles,
                "message": (
                    f"已退出 {'、'.join(titles)}" if titles else "你现在不在任何队列里"
                ),
            }

    # ── 叫号与维护 ────────────────────────────────────

    def call_next(self, queue: str = "") -> dict[str, Any]:
        """叫号：把队首取出并置为「当前」。

        Args:
            queue: 队名；留空用默认队。

        Returns:
            结果 dict，含被叫到的 ``entry``。
        """

        with self._lock:
            name = self.resolve_queue(queue, explicit=True)
            target = self._manager.get_queue(name) if name else None
            if target is None:
                return {"ok": False, "status": "queue_not_found", "message": "找不到这个队列"}
            entry = self._manager.call_next(name)
            if entry is None:
                return {"ok": False, "status": "empty", "message": "队列是空的，没有人可以叫"}
            return {
                "ok": True,
                "status": "called",
                "message": called_message(entry, target),
                "queue": target.name,
                "entry": self._entry_payload(entry, position=0, moment=time.time()),
            }

    def finish_current(self, queue: str = "") -> dict[str, Any]:
        """结束当前服务对象。

        Args:
            queue: 队名；留空用默认队。

        Returns:
            结果 dict。
        """

        with self._lock:
            name = self.resolve_queue(queue, explicit=True)
            target = self._manager.get_queue(name) if name else None
            if target is None:
                return {"ok": False, "status": "queue_not_found", "message": "找不到这个队列"}
            entry = self._manager.finish_current(name)
            if entry is None:
                return {"ok": False, "status": "empty", "message": "当前没有正在服务的人"}
            return {
                "ok": True,
                "status": "finished",
                "message": finished_message(entry, target),
                "queue": target.name,
            }

    def requeue_current(self, queue: str = "") -> dict[str, Any]:
        """把当前服务对象放回队首。

        Args:
            queue: 队名；留空用默认队。

        Returns:
            结果 dict。
        """

        with self._lock:
            name = self.resolve_queue(queue, explicit=True)
            target = self._manager.get_queue(name) if name else None
            if target is None:
                return {"ok": False, "status": "queue_not_found", "message": "找不到这个队列"}
            entry = self._manager.requeue_current(name)
            if entry is None:
                return {"ok": False, "status": "empty", "message": "当前没有正在服务的人"}
            return {
                "ok": True,
                "status": "requeued",
                "message": requeued_message(entry, target),
                "queue": target.name,
            }

    def clear(self, queue: str = "") -> dict[str, Any]:
        """清空队列。

        Args:
            queue: 队名；留空用默认队。

        Returns:
            结果 dict，``removed`` 为被清掉的人数。
        """

        with self._lock:
            name = self.resolve_queue(queue, explicit=True)
            target = self._manager.get_queue(name) if name else None
            if target is None:
                return {"ok": False, "status": "queue_not_found", "message": "找不到这个队列"}
            removed = self._manager.clear(name)
            return {
                "ok": True,
                "status": "cleared",
                "removed": removed,
                "queue": target.name,
                "message": f"已清空「{target.title}」，移除 {removed} 人",
            }

    def set_paused(self, queue: str = "", *, paused: bool) -> dict[str, Any]:
        """暂停或恢复队列。

        Args:
            queue: 队名；留空用默认队。
            paused: ``True`` 表示暂停。

        Returns:
            结果 dict。
        """

        with self._lock:
            name = self.resolve_queue(queue, explicit=True)
            target = self._manager.get_queue(name) if name else None
            if target is None or not self._manager.set_paused(name, paused):
                return {"ok": False, "status": "queue_not_found", "message": "找不到这个队列"}
            verb = "暂停" if paused else "恢复"
            return {
                "ok": True,
                "status": "paused" if paused else "resumed",
                "queue": target.name,
                "message": f"已{verb}「{target.title}」排队",
            }

    def set_capacity(self, queue: str = "", capacity: int = 0) -> dict[str, Any]:
        """修改队列人数上限。

        Args:
            queue: 队名；留空用默认队。
            capacity: 上限，``0`` 表示不限。

        Returns:
            结果 dict。
        """

        with self._lock:
            name = self.resolve_queue(queue, explicit=True)
            if not name or not self._manager.set_capacity(name, capacity):
                return {"ok": False, "status": "queue_not_found", "message": "找不到这个队列"}
            limit = "不限" if capacity == 0 else f"{capacity} 人"
            return {"ok": True, "status": "capacity", "message": f"人数上限已改为 {limit}"}

    def create_queue(
        self,
        queue: str,
        *,
        display_name: str = "",
        capacity: int | None = None,
        guild: str = "",
    ) -> dict[str, Any]:
        """创建队列。

        Args:
            queue: 队名。
            display_name: 展示名。
            capacity: 人数上限；``None`` 表示用默认值。
            guild: 归属标识。

        Returns:
            结果 dict。
        """

        with self._lock:
            name = self._manager.resolve_name(queue)
            if not name:
                return {
                    "ok": False,
                    "status": "invalid_name",
                    "message": "队名不合法（1-16 字，不能含空白）",
                }
            existing = self._manager.get_queue(name)
            created = self._manager.create_queue(
                name, display_name=display_name, capacity=capacity, guild=guild
            )
            if created is None:
                return {
                    "ok": False,
                    "status": "too_many_queues",
                    "message": "队列数量已达上限",
                }
            if existing is not None:
                return {
                    "ok": True,
                    "status": "exists",
                    "queue": created.name,
                    "message": f"队列「{created.title}」已存在",
                }
            return {
                "ok": True,
                "status": "created",
                "queue": created.name,
                "message": f"已创建队列「{created.title}」",
            }

    def delete_queue(self, queue: str = "") -> dict[str, Any]:
        """删除队列。

        Args:
            queue: 队名。

        Returns:
            结果 dict。
        """

        with self._lock:
            name = self._manager.resolve_name(queue)
            if not name or not self._manager.delete_queue(name):
                return {"ok": False, "status": "queue_not_found", "message": "找不到这个队列"}
            return {"ok": True, "status": "deleted", "message": f"已删除队列「{name}」"}

    def remove_user(self, user_id: str, queue: str = "") -> dict[str, Any]:
        """把指定观众移出队列。

        Args:
            user_id: 平台用户 ID。
            queue: 队名；留空用默认队。

        Returns:
            结果 dict。
        """

        with self._lock:
            name = self.resolve_queue(queue, explicit=True)
            entry = self._manager.leave(name, user_id) if name else None
            if entry is None:
                return {"ok": False, "status": "not_found", "message": "该用户不在这个队列里"}
            return {
                "ok": True,
                "status": "removed",
                "message": f"已移除 {entry.display_name}",
            }

    def move_front(self, user_id: str, queue: str = "") -> dict[str, Any]:
        """把指定观众插到队首。

        Args:
            user_id: 平台用户 ID。
            queue: 队名；留空用默认队。

        Returns:
            结果 dict。
        """

        with self._lock:
            name = self.resolve_queue(queue, explicit=True)
            if not name or not self._manager.move_to_front(name, user_id):
                return {"ok": False, "status": "not_found", "message": "该用户不在这个队列里"}
            return {"ok": True, "status": "moved", "message": "已置顶"}

    def add_manual(self, nickname: str, queue: str = "") -> dict[str, Any]:
        """手动加一个人（控制台用，没有平台用户 ID，生成占位 ID）。

        Args:
            nickname: 昵称。
            queue: 队名；留空用默认队。

        Returns:
            结果 dict。
        """

        target = str(queue or "").strip() or self._manager.default_queue
        return self.join(
            user_id=f"manual:{uuid.uuid4().hex[:12]}",
            nickname=nickname,
            platform="manual",
            queue=target,
            queue_explicit=True,
        )

    def reset(self, *, delete_queues: bool = False) -> dict[str, Any]:
        """重置全部队列（换场次时用）。

        Args:
            delete_queues: 为真时连队列本身一起删掉，否则只清空队列内容。

        Returns:
            结果 dict。
        """

        with self._lock:
            queues = list(self._manager.queues())
            removed = 0
            for queue in queues:
                removed += self._manager.clear(queue.name)
                if delete_queues:
                    self._manager.delete_queue(queue.name)

            action = "删除" if delete_queues else "清空"
            return {
                "ok": True,
                "status": "reset",
                "removed": removed,
                "queues": len(queues),
                "message": f"已{action} {len(queues)} 个队列，移除 {removed} 人",
            }

    # ── 序列化辅助 ────────────────────────────────────

    @staticmethod
    def _join_payload(result: JoinResult) -> dict[str, Any]:
        """把入队结果转成对外 dict。

        Args:
            result: 入队结果。

        Returns:
            JSON 可序列化的 dict。
        """

        return {
            "ok": result.ok,
            "status": result.status.value,
            "message": join_message(result),
            "queue": result.queue.name if result.queue is not None else "",
            "queue_title": result.queue.title if result.queue is not None else "",
            "position": result.position,
            "ticket": result.entry.ticket if result.entry is not None else 0,
            "nickname": result.entry.display_name if result.entry is not None else "",
        }


class _EventDedupe:
    """短期事件幂等：记住最近处理过的平台事件，重复的直接忽略。

    一条弹幕可能有多个消费者（bot 的 bilibili_live_adapter 与独立的 bilibili_bridge），
    两边都会带着同一个平台 msg_id 调用接口。这里用带 TTL 的有界表把它折叠成
    「只处理一次」，这样即使两边同时在线也不会重复入队。
    """

    def __init__(self, *, ttl: float = 600.0, capacity: int = 5000) -> None:
        """记录窗口与容量。

        Args:
            ttl: 同一事件在多长时间内算重复（秒）。
            capacity: 最多记住多少个事件。
        """

        self._ttl = max(1.0, float(ttl))
        self._capacity = max(16, int(capacity))
        self._items: OrderedDict[str, float] = OrderedDict()

    def seen(self, key: str, *, now: float | None = None) -> bool:
        """登记事件并返回它是否已经出现过。

        Args:
            key: 事件键（调用方负责拼上类型与平台）。
            now: 当前时间戳，测试可注入。

        Returns:
            True 表示这是重复事件（调用方应跳过）。
        """

        stamp = time.time() if now is None else float(now)
        self._purge(stamp)
        if key in self._items:
            self._items.move_to_end(key)
            return True
        self._items[key] = stamp
        while len(self._items) > self._capacity:
            self._items.popitem(last=False)
        return False

    def clear(self) -> None:
        """清空记录（测试或人工重置用）。"""

        self._items.clear()

    def __len__(self) -> int:
        """当前记住的事件数。"""

        return len(self._items)

    def _purge(self, now: float) -> None:
        """删掉超过 TTL 的记录。"""

        if not self._items:
            return
        deadline = now - self._ttl
        expired = [key for key, stamp in self._items.items() if stamp < deadline]
        for key in expired:
            self._items.pop(key, None)


def _policy_of(raw: str) -> DuplicatePolicy:
    """把配置里的策略字符串转成枚举，非法值退化成 ``reject``。

    Args:
        raw: 配置值。

    Returns:
        重复入队策略。
    """

    try:
        return DuplicatePolicy(str(raw))
    except ValueError:
        return DuplicatePolicy.REJECT


def _aliases_of(entries: list[str]) -> dict[str, str]:
    """把 ``["别名=正式名"]`` 解析成映射。

    Args:
        entries: 配置里读到的列表。

    Returns:
        别名 → 正式队名。
    """

    mapping: dict[str, str] = {}
    for raw in entries or []:
        item = str(raw).strip()
        if not item:
            continue
        for separator in ("=", "＝", ":", "：", "->", "→"):
            if separator not in item:
                continue
            alias, _, target = item.partition(separator)
            alias, target = alias.strip(), target.strip()
            if alias and target:
                mapping[alias] = target
            break
    return mapping


__all__ = ["QueueHub"]
