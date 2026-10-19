"""bilibili_queue 对外服务组件。

把排队能力暴露成稳定签名 ``bilibili_queue:service:queue``，其他插件通过
``service_api.get_service(...)`` 调用即可，不需要依赖本插件的源码（符合插件规范
§2.5 的导入边界）。

注意两点：

- 框架的 ``get_service()`` **每次都会新建实例**，所以本类不持有任何状态，所有操作
  都转发到模块级单例 :func:`~plugins.bilibili_queue.core.runtime.get_queue_runtime`；
- 队列状态在独立应用（排队姬）那边，所以这里的接口**都是异步的**，返回值就是 app 的
  结果 dict（含 ``ok`` / ``status`` / ``message``），调用方看 ``ok`` 即可。
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from src.app.plugin_system.base import BaseService

from ..core.runtime import QueueRuntime, get_queue_runtime


class QueueService(BaseService):
    """直播排队能力服务（转发给独立排队姬）。"""

    name: str = "queue"
    description: str = "直播弹幕排队：入队、退队、叫号、暂停、查询队列状态（转发给独立排队姬）"

    # ── 内部 ──────────────────────────────────────────

    @staticmethod
    def _runtime() -> QueueRuntime:
        """取出运行时单例。

        Returns:
            排队运行时。

        Raises:
            RuntimeError: 插件尚未加载。
        """

        runtime = get_queue_runtime()
        if runtime is None:
            raise RuntimeError("bilibili_queue 尚未加载，排队服务不可用")
        return runtime

    async def _call(self, method: str, **kwargs: Any) -> dict[str, Any]:
        """确保排队姬可用后调用它。

        Args:
            method: :class:`QueueAppClient` 上的方法名。
            **kwargs: 透传参数。

        Returns:
            app 的结果 dict（不可用时为 ``ok=False`` 的合成结果）。
        """

        return await self._runtime().call(method, **kwargs)

    # ── 状态 ──────────────────────────────────────────

    def is_configured(self) -> bool:
        """插件是否已加载（不代表排队姬在跑）。

        Returns:
            已加载返回 ``True``。
        """

        runtime = get_queue_runtime()
        return runtime is not None and runtime.settings.enabled

    async def is_ready(self) -> bool:
        """排队姬当前是否可用（必要时会尝试拉起它）。

        Returns:
            可用返回 ``True``；插件未加载返回 ``False``。
        """

        runtime = get_queue_runtime()
        if runtime is None:
            return False
        ok, _detail = await runtime.ensure_ready()
        return ok

    async def health(self) -> dict[str, Any]:
        """排队姬的健康信息。

        Returns:
            app 的健康 dict；不可用时为 ``ok=False``。
        """

        return await self._call("health")

    async def snapshot(self, *, queue: str = "", limit: int = 0) -> dict[str, Any]:
        """队列状态快照。

        Args:
            queue: 只取某个队列。
            limit: 每队最多返回多少条目；``0`` 表示不截断。

        Returns:
            app 的状态 dict。
        """

        return await self._call("state", queue=queue, limit=limit)

    async def queue_names(self) -> list[str]:
        """队列名列表。

        Returns:
            队列名列表；不可用时为空列表。
        """

        payload = await self._call("queues")
        names = payload.get("queues")
        return [str(name) for name in names] if isinstance(names, list) else []

    # ── 观众操作 ──────────────────────────────────────

    async def join(
        self,
        *,
        user_id: str,
        nickname: str = "",
        queue: str = "",
        platform: str = "",
        queue_explicit: bool = True,
        extra: dict[str, Any] | None = None,
        guild: str = "",
    ) -> dict[str, Any]:
        """让一个用户入队。

        Args:
            user_id: 平台用户 ID。
            nickname: 昵称。
            queue: 队名（支持别名）；留空用默认队。
            platform: 平台标识。
            queue_explicit: 队名是否明确指定（决定未命中时是否回退默认队）。
            extra: 附加信息。
            guild: 归属标识（直播间号 / 群号）。

        Returns:
            app 的结果 dict，含 ``ok`` / ``status`` / ``position`` / ``ticket``。
        """

        return await self._call(
            "join",
            user_id=user_id,
            nickname=nickname,
            platform=platform,
            queue=queue,
            queue_explicit=queue_explicit,
            extra=extra or {},
            guild=guild,
        )

    async def leave(self, *, user_id: str, queue: str = "") -> dict[str, Any]:
        """让一个用户退队。

        Args:
            user_id: 平台用户 ID。
            queue: 指定队名；留空退出全部队列。

        Returns:
            app 的结果 dict，``queues`` 为实际退出的队列展示名。
        """

        return await self._call(
            "leave", user_id=user_id, queue=queue, queue_explicit=bool(queue)
        )

    async def remove_user(self, *, user_id: str, queue: str = "") -> dict[str, Any]:
        """把指定用户移出队列。

        Args:
            user_id: 平台用户 ID。
            queue: 队名。

        Returns:
            app 的结果 dict。
        """

        return await self._call("remove_user", user_id=user_id, queue=queue)

    # ── 叫号与管理 ────────────────────────────────────

    async def call_next(self, queue: str = "") -> dict[str, Any]:
        """叫号。

        Args:
            queue: 队名；留空用默认队。

        Returns:
            app 的结果 dict，``entry`` 为被叫到的观众。
        """

        return await self._call("call_next", queue=queue)

    async def finish_current(self, queue: str = "") -> dict[str, Any]:
        """结束当前服务对象。

        Args:
            queue: 队名；留空用默认队。

        Returns:
            app 的结果 dict。
        """

        return await self._call("finish", queue=queue)

    async def requeue_current(self, queue: str = "") -> dict[str, Any]:
        """把当前服务对象放回队首。

        Args:
            queue: 队名；留空用默认队。

        Returns:
            app 的结果 dict。
        """

        return await self._call("requeue", queue=queue)

    async def clear(self, queue: str = "") -> dict[str, Any]:
        """清空队列。

        Args:
            queue: 队名；留空用默认队。

        Returns:
            app 的结果 dict，``removed`` 为被清掉的人数。
        """

        return await self._call("clear", queue=queue)

    async def set_paused(self, queue: str = "", *, paused: bool) -> dict[str, Any]:
        """暂停或恢复队列。

        Args:
            queue: 队名。
            paused: ``True`` 表示暂停。

        Returns:
            app 的结果 dict。
        """

        return await self._call("set_paused", queue=queue, paused=paused)

    async def create_queue(
        self, queue: str, *, display_name: str = "", capacity: int = 0
    ) -> dict[str, Any]:
        """创建一个队列。

        Args:
            queue: 队名。
            display_name: 展示名。
            capacity: 人数上限；``0`` 表示不限。

        Returns:
            app 的结果 dict。
        """

        return await self._call(
            "create_queue", queue=queue, display_name=display_name, capacity=capacity
        )

    async def delete_queue(self, queue: str) -> dict[str, Any]:
        """删除一个队列。

        Args:
            queue: 队名。

        Returns:
            app 的结果 dict。
        """

        return await self._call("delete_queue", queue=queue)

    async def reset(self, *, delete_queues: bool = False) -> dict[str, Any]:
        """重置全部队列。

        Args:
            delete_queues: 为真时连队列本身一起删掉。

        Returns:
            app 的结果 dict。
        """

        return await self._call("reset", delete_queues=delete_queues)

    # ── 高级：直接拿到运行时 ──────────────────────────

    def runtime_accessor(self) -> Callable[[], QueueRuntime]:
        """返回一个可以取到运行时的函数（给需要更细粒度控制的插件用）。

        Returns:
            无参函数，返回 :class:`QueueRuntime`。
        """

        return self._runtime


__all__ = ["QueueService"]
