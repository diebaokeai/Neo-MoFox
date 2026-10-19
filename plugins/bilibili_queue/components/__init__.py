"""bilibili_queue 的组件层。

包含四种组件：

- :mod:`~plugins.bilibili_queue.components.queue_service` — 对外服务接口；
- :mod:`~plugins.bilibili_queue.components.danmaku_handler` — 弹幕事件处理器；
- :mod:`~plugins.bilibili_queue.components.queue_command` — 观众命令；
- :mod:`~plugins.bilibili_queue.components.queue_admin_command` — 管理命令。

队列状态与 HTTP 服务/页面都在独立应用 ``live_queue`` 那边，插件不再注册 Router 组件。
"""

from .danmaku_handler import DanmakuQueueHandler
from .queue_admin_command import QueueAdminCommand
from .queue_command import QueueCommand
from .queue_service import QueueService

__all__ = [
    "DanmakuQueueHandler",
    "QueueAdminCommand",
    "QueueCommand",
    "QueueService",
]
