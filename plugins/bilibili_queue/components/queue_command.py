"""bilibili_queue 观众侧命令。

聊天平台（QQ 等）上的排队入口，命令前缀由框架决定（默认 ``/``）：

    /排队                 排进默认队
    /排队 加入 [队名]      排进指定队
    /排队 退出 [队名]      退出队列
    /排队 列表             查看所有队列
    /排队 我的             看自己在哪些队列里
    /queue ...            英文别名

管理类命令在 :mod:`plugins.bilibili_queue.components.queue_admin_command` 里，
单独做成一个更高权限的组件，避免观众命令被整体提权。

所有操作都转发给独立应用（排队姬），本组件不持有任何队列状态。
"""

from __future__ import annotations

from src.app.plugin_system.api import log_api
from src.app.plugin_system.api.send_api import send_text
from src.app.plugin_system.base import BaseCommand, cmd_route
from src.app.plugin_system.types import PermissionLevel

from ..core import messages
from ..core.runtime import QueueRuntime, get_queue_runtime
from .queue_common import sender_info

logger = log_api.get_logger("bilibili_queue.command")


class QueueCommand(BaseCommand):
    """观众排队命令。"""

    name: str = "排队"
    description: str = "排队入队 / 退出 / 查看队列"
    permission_level: PermissionLevel = PermissionLevel.USER

    @classmethod
    def match(cls, parts: list[str]) -> int:
        """匹配命令名，同时支持「排队」与英文别名 ``queue``。

        Args:
            parts: 命令片段列表。

        Returns:
            吃掉的片段数，不匹配返回 0。
        """

        if not parts:
            return 0
        if parts[0] in ("排队", "queue"):
            return 1
        return 0

    # ── 内部 ──────────────────────────────────────────

    async def _reply(self, text: str) -> None:
        """向当前聊天流回复文本。

        Args:
            text: 回复内容。
        """

        if not text:
            return
        try:
            await send_text(text, stream_id=self.stream_id)
        except Exception as exc:
            logger.warning(f"排队命令回复失败: {exc}")

    @staticmethod
    def _runtime() -> QueueRuntime | None:
        """取出运行时单例。

        Returns:
            排队运行时；插件未加载时为 ``None``。
        """

        return get_queue_runtime()

    # ── 路由 ──────────────────────────────────────────

    @cmd_route()
    async def handle_join_default(self) -> tuple[bool, str]:
        """``/排队``：排进默认队。"""

        return await self._join("")

    @cmd_route("加入")
    async def handle_join(self, queue_name: str = "") -> tuple[bool, str]:
        """``/排队 加入 [队名]``：排进指定队。

        Args:
            queue_name: 队名，留空表示默认队。
        """

        return await self._join(queue_name)

    @cmd_route("退出")
    async def handle_leave(self, queue_name: str = "") -> tuple[bool, str]:
        """``/排队 退出 [队名]``：退出队列。

        Args:
            queue_name: 队名，留空表示退出全部队列。
        """

        runtime = self._runtime()
        if runtime is None:
            await self._reply("排队功能未启用。")
            return False, "unavailable"

        sender = sender_info(self._message)
        payload = await runtime.call(
            "leave",
            user_id=sender.user_id,
            queue=queue_name,
            queue_explicit=bool(queue_name),
        )
        await self._reply(messages.leave_reply(payload, sender.display_name))
        return bool(payload.get("ok")), str(payload.get("status") or "left")

    @cmd_route("列表")
    async def handle_list(self) -> tuple[bool, str]:
        """``/排队 列表``：查看所有队列概况。"""

        runtime = self._runtime()
        if runtime is None:
            await self._reply("排队功能未启用。")
            return False, "unavailable"

        payload = await runtime.call("state")
        await self._reply(messages.status_reply(payload))
        return True, "listed"

    @cmd_route("我的")
    async def handle_mine(self) -> tuple[bool, str]:
        """``/排队 我的``：查看自己所在的队列与位置。"""

        runtime = self._runtime()
        if runtime is None:
            await self._reply("排队功能未启用。")
            return False, "unavailable"

        sender = sender_info(self._message)
        payload = await runtime.call("state")

        lines: list[str] = []
        for queue in payload.get("queues") or []:
            if not isinstance(queue, dict):
                continue
            title = str(queue.get("title") or queue.get("name") or "队列")

            current = queue.get("current")
            if isinstance(current, dict) and current.get("user_id") == sender.user_id:
                lines.append(f"· 「{title}」正在服务中（#{current.get('ticket', '?')}）")
                continue

            for entry in queue.get("entries") or []:
                if isinstance(entry, dict) and entry.get("user_id") == sender.user_id:
                    lines.append(
                        f"· 「{title}」第 {entry.get('position', '?')} 位"
                        f" / 共 {queue.get('waiting', '?')} 人"
                    )
                    break

        if not lines:
            await self._reply(f"你还没有排队哦，发送「{self.name}」即可入队。")
            return True, "empty"

        await self._reply("你的排队情况：\n" + "\n".join(lines))
        return True, "mined"

    # ── 共用实现 ──────────────────────────────────────

    async def _join(self, queue_name: str) -> tuple[bool, str]:
        """入队并回复结果。

        Args:
            queue_name: 队名，留空表示默认队。

        Returns:
            ``(是否成功, 简述)``。
        """

        runtime = self._runtime()
        if runtime is None:
            await self._reply("排队功能未启用。")
            return False, "unavailable"

        sender = sender_info(self._message)
        payload = await runtime.call(
            "join",
            user_id=sender.user_id,
            nickname=sender.display_name,
            platform=sender.platform,
            queue=queue_name,
            queue_explicit=bool(queue_name),
            extra={"guard_level": sender.guard_level},
            guild=sender.group_id,
        )

        await self._reply(
            messages.join_reply(payload, join_words=runtime.settings.join_keywords)
        )
        if runtime.settings.log_commands:
            logger.info(
                f"[排队] {sender.display_name} 命令入队 -> {payload.get('status')}"
            )
        return bool(payload.get("ok")), str(payload.get("status") or "unknown")


__all__ = ["QueueCommand"]
