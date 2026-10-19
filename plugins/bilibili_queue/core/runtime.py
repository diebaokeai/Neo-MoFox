"""bilibili_queue 插件的运行时门面。

把三样东西缝在一起，供各个组件统一使用：

- :class:`~plugins.bilibili_queue.core.settings.PluginSettings`：弹幕侧规则；
- :class:`~plugins.bilibili_queue.core.client.QueueAppClient`：访问独立 app；
- :class:`~plugins.bilibili_queue.core.supervisor.QueueAppSupervisor`：app 不在时把它拉起来。

它还是**模块级单例**（见 :func:`get_queue_runtime`），因为框架的 Service 组件每次
``get_service()`` 都会新建实例，状态不能挂在组件实例上。

就绪检查带 30 秒缓存：否则每条弹幕都要先打一次 ``/api/health``，白白多一半请求。
调用 app 失败时会 :meth:`QueueRuntime.invalidate_ready` 让缓存失效，下一轮重新探测
（app 崩了/被关了也能自愈）。
"""

from __future__ import annotations

import time
from typing import Any

from src.app.plugin_system.api import log_api

from .client import QueueAppClient, QueueAppError
from .keywords import KeywordMatcher, ParsedIntent
from .settings import PluginSettings
from .supervisor import QueueAppSupervisor

logger = log_api.get_logger("bilibili_queue.runtime")

_READY_CACHE_SECONDS = 30.0
"""就绪状态缓存时长（秒）。"""

_RANK_ALLOWED: dict[str, frozenset[int]] = {
    "anyone": frozenset({0, 1, 2, 3}),
    "captain": frozenset({1, 2, 3}),
    "admiral": frozenset({1, 2}),
    "governor": frozenset({1}),
}
"""大航海等级门槛。B 站 ``guard_level`` 数字越小等级越高：1=总督、2=提督、3=舰长。"""


class QueueRuntime:
    """插件运行时的统一入口。"""

    def __init__(
        self,
        *,
        settings: PluginSettings | None = None,
        client: QueueAppClient | None = None,
        supervisor: QueueAppSupervisor | None = None,
    ) -> None:
        """构造运行时。

        Args:
            settings: 运行参数；``None`` 时用全默认值。
            client: HTTP 客户端；``None`` 时按参数自建。
            supervisor: app 进程管理；``None`` 时自建。
        """

        self._settings = settings or PluginSettings()
        self._client = client or QueueAppClient(
            base_url=self._settings.app_base_url, token=self._settings.app_token
        )
        self._supervisor = supervisor or QueueAppSupervisor(
            settings=self._settings, client=self._client
        )
        self._matcher = self._build_matcher(self._settings)
        self._ready_until = 0.0
        self._last_reply_at: dict[str, float] = {}

    # ── 只读属性 ──────────────────────────────────────

    @property
    def settings(self) -> PluginSettings:
        """当前运行参数。"""

        return self._settings

    @property
    def client(self) -> QueueAppClient:
        """独立 app 的客户端。"""

        return self._client

    @property
    def supervisor(self) -> QueueAppSupervisor:
        """独立 app 的进程管理。"""

        return self._supervisor

    @property
    def matcher(self) -> KeywordMatcher:
        """弹幕关键词匹配器。"""

        return self._matcher

    # ── 构造辅助 ──────────────────────────────────────

    @staticmethod
    def _build_matcher(settings: PluginSettings) -> KeywordMatcher:
        """按运行参数编译关键词匹配器。

        Args:
            settings: 运行参数。

        Returns:
            匹配器实例。
        """

        return KeywordMatcher(
            join=settings.join_keywords,
            leave=settings.leave_keywords,
            status=settings.status_keywords,
            prefix=settings.command_prefix,
            ignore_case=settings.ignore_case,
        )

    def apply_settings(self, settings: PluginSettings) -> None:
        """热更新运行参数。

        Args:
            settings: 新的运行参数。
        """

        self._settings = settings
        self._matcher = self._build_matcher(settings)
        self._client = QueueAppClient(
            base_url=settings.app_base_url, token=settings.app_token
        )
        self._supervisor = QueueAppSupervisor(settings=settings, client=self._client)
        self.invalidate_ready()
        logger.info("排队插件配置已热更新")

    # ── 就绪状态 ──────────────────────────────────────

    async def ensure_ready(self, *, force: bool = False) -> tuple[bool, str]:
        """确保独立 app 可用（必要时拉起），带缓存。

        Args:
            force: 忽略缓存强制重新探测。

        Returns:
            ``(是否可用, 说明)``。
        """

        now = time.time()
        if not force and now < self._ready_until:
            return True, "就绪状态缓存命中"

        ok, detail = await self._supervisor.ensure_running()
        self._ready_until = time.time() + _READY_CACHE_SECONDS if ok else 0.0
        return ok, detail

    def invalidate_ready(self) -> None:
        """让就绪缓存失效（调用 app 失败后使用）。"""

        self._ready_until = 0.0

    async def call(self, method: str, /, **kwargs: Any) -> dict[str, Any]:
        """确保 app 就绪后调用它的某个方法。

        **永远返回一个「长得像 app 返回」的 dict**（含 ``ok`` / ``status`` / ``message``）：
        app 不可用或调用失败时，这里合成一个 ``ok=False`` 的结果，而不是抛异常或返回
        ``None``。这样上层组件只需要看 ``payload["ok"]``，不用到处写 try/except。

        Args:
            method: :class:`QueueAppClient` 上的方法名，例如 ``join``。
            **kwargs: 透传给该方法。

        Returns:
            app 的结果 dict，或合成的失败 dict。
        """

        ok, detail = await self.ensure_ready()
        if not ok:
            return {
                "ok": False,
                "status": "app_unavailable",
                "message": f"排队服务暂时不可用（{detail}）",
            }

        func = getattr(self._client, method, None)
        if not callable(func) or method.startswith("_"):
            raise AttributeError(f"QueueRuntime.call 不支持的方法：{method}")

        try:
            result = await func(**kwargs)
        except QueueAppError as exc:
            # 让缓存失效：app 可能刚崩/被关，下一轮重新探测（必要时重新拉起）
            self.invalidate_ready()
            logger.error(f"调用独立排队姬失败（{method}）：{exc}")
            return {
                "ok": False,
                "status": "app_error",
                "message": f"排队服务出错了（{exc}）",
            }

        if not isinstance(result, dict):
            # 兜底：万一某个客户端方法返回的不是 dict，也要保证上层拿到的是 dict，
            # 否则一个属性访问就会炸掉整条弹幕处理链路
            return {"ok": True, "status": "ok", "message": "", "value": result}
        return result

    async def close(self) -> None:
        """插件卸载时的收尾。

        默认**不关掉独立 app**——它是能单独使用的产品，用户可能正开着它直播。
        需要「随 bot 一起关」时把配置 ``app.stop_on_unload`` 设为 true。
        """

        if self._settings.app_stop_on_unload:
            self._supervisor.stop()
        else:
            logger.info("保留独立 app 继续运行（app.stop_on_unload=false）")

    # ── 弹幕解析与权限 ────────────────────────────────

    def parse(self, text: str) -> ParsedIntent:
        """解析一条弹幕。

        Args:
            text: 弹幕正文。

        Returns:
            :class:`ParsedIntent`。
        """

        return self._matcher.parse(text)

    def supports_platform(self, platform: str) -> bool:
        """该平台是否允许触发排队。

        Args:
            platform: 平台标识。

        Returns:
            在白名单里返回 ``True``。
        """

        if not self._settings.platforms:
            return True
        return str(platform or "") in self._settings.platforms

    def is_admin(self, *, guard_level: int = 0, sender_role: str = "") -> bool:
        """判断发送者是否有弹幕管理权限。

        Args:
            guard_level: B 站大航海等级（0 表示非舰长）。
            sender_role: 统一消息模型里的角色字符串。

        Returns:
            有权限返回 ``True``。
        """

        allowed = _RANK_ALLOWED.get(self._settings.admin_min_rank)
        if allowed is None:
            # 配置写错时按最严处理，避免误放开权限
            allowed = _RANK_ALLOWED["governor"]
        try:
            level = int(guard_level or 0)
        except (TypeError, ValueError):
            level = 0
        if level in allowed:
            return True
        return str(sender_role or "").lower() == "operator"

    def reply_allowed(self, stream_id: str) -> bool:
        """自动回复的节流判断（调用即计数）。

        Args:
            stream_id: 聊天流 ID。

        Returns:
            允许回复返回 ``True``。
        """

        cooldown = self._settings.reply_cooldown
        if cooldown <= 0:
            return True
        moment = time.time()
        last = self._last_reply_at.get(stream_id, 0.0)
        if moment - last < cooldown:
            return False
        self._last_reply_at[stream_id] = moment
        return True


_runtime: QueueRuntime | None = None
"""模块级单例。"""


def get_queue_runtime() -> QueueRuntime | None:
    """返回当前的运行时单例。

    Returns:
        已初始化的运行时；插件尚未加载时返回 ``None``。
    """

    return _runtime


def init_queue_runtime(runtime: QueueRuntime) -> None:
    """注册运行时单例（由插件在 ``on_plugin_loaded`` 调用）。

    Args:
        runtime: 运行时实例。
    """

    global _runtime
    _runtime = runtime


def clear_queue_runtime() -> None:
    """注销运行时单例（由插件在 ``on_plugin_unloaded`` 调用）。"""

    global _runtime
    _runtime = None


__all__ = [
    "QueueRuntime",
    "clear_queue_runtime",
    "get_queue_runtime",
    "init_queue_runtime",
]
