"""bilibili_queue 插件入口。

本插件**不持有队列状态**：队列归独立应用 ``live_queue``（排队姬）所有，插件只做两件事：

1. 把直播弹幕翻译成对排队姬的 HTTP 调用（观众发「排队」就入队）；
2. 保证排队姬在需要时是可用的（没在跑就自动把它拉起来）。

这样切分的好处：排队姬能脱离 bot 单独使用（自己开窗口、自己叫号、自己放 OBS 叠加页），
而 bot 一开机又能接管它；两边只有一份队列状态，不会出现「同步不一致」。

组件构成：

- ``service:queue``：对外的排队能力接口（供其他插件调用）；
- ``event_handler:danmaku_queue``：拦截 ``BEFORE_MESSAGE_RECEIVED`` 识别弹幕意图；
- ``command:排队`` / ``command:排队管理``：聊天平台上的观众命令与管理命令。
"""

from __future__ import annotations

from typing import Any

from src.app.plugin_system.api import log_api
from src.app.plugin_system.base import BasePlugin, register_plugin
from src.kernel.concurrency import get_task_manager

from .components.danmaku_handler import DanmakuQueueHandler
from .components.queue_admin_command import QueueAdminCommand
from .components.queue_command import QueueCommand
from .components.queue_service import QueueService
from .config import BilibiliQueueConfig
from .core.runtime import (
    QueueRuntime,
    clear_queue_runtime,
    get_queue_runtime,
    init_queue_runtime,
)
from .core.settings import PluginSettings

logger = log_api.get_logger("bilibili_queue")


@register_plugin
class BilibiliQueuePlugin(BasePlugin):
    """B 站直播弹幕排队插件（排队姬的 bot 侧接入）。"""

    plugin_name: str = "bilibili_queue"

    configs: list[type] = [BilibiliQueueConfig]
    dependent_components: list[str] = []

    def __init__(self, config: Any = None) -> None:
        """初始化插件。

        Args:
            config: 框架注入的配置实例。
        """

        super().__init__(config)
        self._runtime: QueueRuntime | None = None
        self._warmup_task_id: str | None = None

    def get_components(self) -> list[type]:
        """返回插件提供的组件类。

        Returns:
            组件类列表（不是实例）。
        """

        return [
            QueueService,
            DanmakuQueueHandler,
            QueueCommand,
            QueueAdminCommand,
        ]

    # ── 生命周期 ──────────────────────────────────────

    async def on_plugin_loaded(self) -> None:
        """初始化运行时，并在后台把排队姬拉起来（不阻塞插件加载）。"""

        settings = PluginSettings.from_config(self.config)
        runtime = QueueRuntime(settings=settings)
        init_queue_runtime(runtime)
        self._runtime = runtime

        if not settings.enabled:
            logger.warning(
                "排队插件已在配置中禁用（plugin.enabled=false）：弹幕不再触发排队，"
                "排队姬仍可单独使用"
            )

        self._warm_up_async(runtime)

        logger.info(
            f"排队插件已加载；排队姬 {settings.app_base_url}"
            f"（叠加页 {settings.app_base_url}/overlay）"
        )

    def _warm_up_async(self, runtime: QueueRuntime) -> None:
        """在后台任务里确保排队姬可用。

        不在这里直接 ``await``：拉起 app 可能要等十几秒（首次启动要加载 Qt），
        插件加载是串行的，卡住它等于拖慢整个 bot 启动。

        Args:
            runtime: 排队运行时。
        """

        async def warm_up() -> None:
            """探测并在必要时拉起排队姬。"""

            try:
                ok, detail = await runtime.ensure_ready()
            except Exception as exc:
                logger.error(f"准备排队姬时出错：{exc}", exc_info=True)
                return

            if ok:
                logger.info(f"排队服务就绪：{detail}")
            else:
                logger.warning(f"排队服务暂不可用：{detail}；弹幕到达时会自动重试")

        try:
            task_info = get_task_manager().create_task(
                warm_up(), name="bilibili_queue_warmup", daemon=True
            )
            self._warmup_task_id = task_info.task_id
        except Exception as exc:
            # 预热只是「尽快把排队姬拉起来」，失败不该影响插件加载：
            # 第一次真正用到排队时会再探测一次
            logger.warning(f"无法创建后台预热任务，将在首次排队时再探测：{exc}")

    async def on_plugin_unloaded(self) -> None:
        """注销运行时。

        默认**不关掉排队姬**——它是能单独使用的程序，用户可能正开着它直播。
        需要「随 bot 一起关」时把 ``app.stop_on_unload`` 设为 true。
        """

        if self._warmup_task_id:
            try:
                get_task_manager().cancel_task(self._warmup_task_id)
            except Exception as exc:
                logger.debug(f"取消预热任务失败：{exc}")
            self._warmup_task_id = None

        runtime = get_queue_runtime()
        if runtime is not None:
            try:
                await runtime.close()
            except Exception as exc:
                logger.error(f"卸载排队插件时出错：{exc}", exc_info=True)

        clear_queue_runtime()
        self._runtime = None
        logger.info("排队插件已卸载")


__all__ = ["BilibiliQueuePlugin"]
