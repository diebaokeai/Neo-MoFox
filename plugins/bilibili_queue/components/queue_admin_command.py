"""bilibili_queue 管理侧命令。

独立成一个更高权限的组件（``PermissionLevel.OPERATOR``），避免观众命令被整体提权。
权限由框架的权限管理器判定（``owner_list`` / 权限组），因此适用于 QQ 等支持权限
体系的平台；**B 站直播间请用弹幕命令**（见
:mod:`plugins.bilibili_queue.components.danmaku_handler`，那里按大航海等级授权）。

    /排队管理 下一个 [队名]     叫号出队
    /排队管理 结束 [队名]       结束当前
    /排队管理 放回 [队名]       把当前放回队首
    /排队管理 清空 [队名]       清空队列
    /排队管理 暂停 [队名]       暂停排队
    /排队管理 恢复 [队名]       恢复排队
    /排队管理 队列              查看所有队列
    /排队管理 重置 [delete]     清空全部队列（带 delete 连队列一起删）
    /排队管理 建队 <队名> [展示名] [上限]
    /排队管理 删队 <队名>
    /排队管理 移除 <用户ID> [队名]

队列操作全部转发给独立应用（排队姬），本组件不持有任何队列状态。
"""

from __future__ import annotations

from src.app.plugin_system.api import log_api
from src.app.plugin_system.api.send_api import send_text
from src.app.plugin_system.base import BaseCommand, cmd_route
from src.app.plugin_system.types import PermissionLevel

from ..core import messages
from ..core.runtime import QueueRuntime, get_queue_runtime

logger = log_api.get_logger("bilibili_queue.admin_command")


class QueueAdminCommand(BaseCommand):
    """队列管理命令（需要 OPERATOR 及以上权限）。"""

    name: str = "排队管理"
    description: str = "叫号、清空、暂停、建队、删队等队列管理操作"
    permission_level: PermissionLevel = PermissionLevel.OPERATOR

    @classmethod
    def match(cls, parts: list[str]) -> int:
        """匹配命令名，支持「排队管理」与 ``queueadmin``。

        Args:
            parts: 命令片段列表。

        Returns:
            吃掉的片段数，不匹配返回 0。
        """

        if not parts:
            return 0
        if parts[0] in ("排队管理", "queueadmin"):
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
            logger.warning(f"排队管理命令回复失败: {exc}")

    @staticmethod
    def _runtime() -> QueueRuntime | None:
        """取出运行时单例。

        Returns:
            排队运行时；插件未加载时为 ``None``。
        """

        return get_queue_runtime()

    async def _guard(self) -> QueueRuntime | None:
        """取运行时；不可用时回复提示。

        Returns:
            运行时实例或 ``None``。
        """

        runtime = self._runtime()
        if runtime is None:
            await self._reply("排队功能未启用。")
        return runtime

    async def _run(
        self, runtime: QueueRuntime, method: str, **kwargs: object
    ) -> tuple[bool, str]:
        """调用 app 的某个方法、回复结果，并返回 ``(是否成功, 状态)``。

        Args:
            runtime: 排队运行时。
            method: :class:`QueueAppClient` 上的方法名。
            **kwargs: 透传参数。

        Returns:
            ``(是否成功, 状态码)``。
        """

        payload = await runtime.call(method, **kwargs)
        text = messages.operation_reply(payload)
        if payload.get("ok") and runtime.settings.log_commands:
            logger.info(f"[排队] 管理命令 {method} -> {text}")
        await self._reply(text)
        return bool(payload.get("ok")), str(payload.get("status") or "unknown")

    # ── 路由 ──────────────────────────────────────────

    @cmd_route("下一个")
    async def handle_next(self, queue_name: str = "") -> tuple[bool, str]:
        """``下一个 [队名]``：叫号。

        Args:
            queue_name: 队名，留空表示默认队。
        """

        runtime = await self._guard()
        if runtime is None:
            return False, "unavailable"
        return await self._run(runtime, "call_next", queue=queue_name)

    @cmd_route("结束")
    async def handle_finish(self, queue_name: str = "") -> tuple[bool, str]:
        """``结束 [队名]``：结束当前服务对象。

        Args:
            queue_name: 队名，留空表示默认队。
        """

        runtime = await self._guard()
        if runtime is None:
            return False, "unavailable"
        return await self._run(runtime, "finish", queue=queue_name)

    @cmd_route("放回")
    async def handle_requeue(self, queue_name: str = "") -> tuple[bool, str]:
        """``放回 [队名]``：把当前叫号对象放回队首。

        Args:
            queue_name: 队名，留空表示默认队。
        """

        runtime = await self._guard()
        if runtime is None:
            return False, "unavailable"
        return await self._run(runtime, "requeue", queue=queue_name)

    @cmd_route("清空")
    async def handle_clear(self, queue_name: str = "") -> tuple[bool, str]:
        """``清空 [队名]``：清空队列。

        Args:
            queue_name: 队名，留空表示默认队。
        """

        runtime = await self._guard()
        if runtime is None:
            return False, "unavailable"
        return await self._run(runtime, "clear", queue=queue_name)

    @cmd_route("暂停")
    async def handle_pause(self, queue_name: str = "") -> tuple[bool, str]:
        """``暂停 [队名]``：暂停接受排队。

        Args:
            queue_name: 队名，留空表示默认队。
        """

        runtime = await self._guard()
        if runtime is None:
            return False, "unavailable"
        return await self._run(runtime, "set_paused", queue=queue_name, paused=True)

    @cmd_route("恢复")
    async def handle_resume(self, queue_name: str = "") -> tuple[bool, str]:
        """``恢复 [队名]``：恢复接受排队。

        Args:
            queue_name: 队名，留空表示默认队。
        """

        runtime = await self._guard()
        if runtime is None:
            return False, "unavailable"
        return await self._run(runtime, "set_paused", queue=queue_name, paused=False)

    @cmd_route("队列")
    async def handle_queues(self) -> tuple[bool, str]:
        """``队列``：查看所有队列概况。"""

        runtime = await self._guard()
        if runtime is None:
            return False, "unavailable"

        payload = await runtime.call("state")
        await self._reply(messages.status_reply(payload))
        return True, "listed"

    @cmd_route("重置")
    async def handle_reset(self, mode: str = "") -> tuple[bool, str]:
        """``重置 [delete]``：清空全部队列；带 ``delete`` 时连队列一起删。

        Args:
            mode: 传 ``delete`` / ``删除`` / ``删`` 时删除队列本身。
        """

        runtime = await self._guard()
        if runtime is None:
            return False, "unavailable"

        delete_queues = mode.strip().lower() in ("delete", "删除", "删")
        return await self._run(runtime, "reset", delete_queues=delete_queues)

    @cmd_route("建队")
    async def handle_create(
        self, queue_name: str = "", display_name: str = "", capacity: int = 0
    ) -> tuple[bool, str]:
        """``建队 <队名> [展示名] [上限]``：新建队列。

        Args:
            queue_name: 队名。
            display_name: 展示名，留空则与队名相同。
            capacity: 人数上限，``0`` 表示不限。
        """

        runtime = await self._guard()
        if runtime is None:
            return False, "unavailable"

        if not queue_name:
            await self._reply("请指定队名，例如「排队管理 建队 排位」。")
            return False, "missing_arg"

        return await self._run(
            runtime,
            "create_queue",
            queue=queue_name,
            display_name=display_name,
            capacity=max(0, capacity),
        )

    @cmd_route("删队")
    async def handle_delete(self, queue_name: str = "") -> tuple[bool, str]:
        """``删队 <队名>``：删除队列。

        Args:
            queue_name: 队名。
        """

        runtime = await self._guard()
        if runtime is None:
            return False, "unavailable"

        if not queue_name:
            await self._reply("请指定要删除的队名。")
            return False, "missing_arg"

        return await self._run(runtime, "delete_queue", queue=queue_name)

    @cmd_route("移除")
    async def handle_remove(
        self, user_id: str = "", queue_name: str = ""
    ) -> tuple[bool, str]:
        """``移除 <用户ID> [队名]``：把指定用户移出队列。

        Args:
            user_id: 平台用户 ID。
            queue_name: 队名，留空表示默认队。
        """

        runtime = await self._guard()
        if runtime is None:
            return False, "unavailable"

        if not user_id:
            await self._reply("请指定要移除的用户 ID。")
            return False, "missing_arg"

        return await self._run(runtime, "remove_user", user_id=user_id, queue=queue_name)

    @cmd_route("帮助")
    async def handle_help(self) -> tuple[bool, str]:
        """``帮助``：查看管理命令列表。"""

        runtime = self._runtime()
        prefix = runtime.settings.command_prefix if runtime is not None else ""
        await self._reply(messages.admin_usage(prefix))
        return True, "help"


__all__ = ["QueueAdminCommand"]
