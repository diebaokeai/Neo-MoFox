"""bilibili_queue 弹幕事件处理器。

订阅 ``BEFORE_MESSAGE_RECEIVED``——消息接收管线的最上游，此时信封还没被转换成
``Message``、还没进聊天流、还没进 chatter。在这里处理排队关键词有两个好处：

1. 能直接读到 B 站平台专属字段（``guard_level`` 只存在于信封的
   ``additional_config`` 里，转换后会丢）；
2. 可以通过返回 ``EventDecision.STOP`` 干净地拦截，命中关键词的弹幕不会再去
   打扰 LLM，省钱也不串味。

两个硬约束（踩过才知道）：

- 返回的 ``params`` **key 集合必须与入参完全一致**，否则框架会丢弃本次处理结果；
- 处理器抛异常会被框架转换成 ``PASS``（等于放行给 LLM），所以这里必须自己
  ``try/except`` 兜住，并保证「决定拦截」时一定返回 ``STOP``。

队列操作全部通过 :meth:`QueueRuntime.call` 打到独立 app；app 不可用时它返回一个
``ok=False`` 的结果（不会抛异常），弹幕仍然被拦截——「排队」是命令，丢给 LLM 只会
得到莫名其妙的回复。
"""

from __future__ import annotations

from typing import Any

from src.app.plugin_system.api import log_api
from src.app.plugin_system.api.send_api import send_text
from src.app.plugin_system.base import BaseEventHandler
from src.app.plugin_system.types import ChatStream, EventType
from src.kernel.event import EventDecision

from ..core import messages
from ..core.envelope import DanmakuInfo, extract_danmaku
from ..core.keywords import Intent, ParsedIntent
from ..core.runtime import QueueRuntime, get_queue_runtime

logger = log_api.get_logger("bilibili_queue.danmaku")


class DanmakuQueueHandler(BaseEventHandler):
    """从直播弹幕里识别排队意图并操作排队姬。"""

    name: str = "danmaku_queue"
    description: str = "监听直播弹幕，命中关键词即入队/退队/查队列，并支持舰长弹幕管理队列"
    weight: int = 100
    """优先级调高，确保排在消息分发器之前。"""

    init_subscribe: list[EventType | str] = [EventType.BEFORE_MESSAGE_RECEIVED]

    # ── 入口 ──────────────────────────────────────────

    async def execute(
        self, event_name: str, params: dict[str, Any]
    ) -> tuple[EventDecision, dict[str, Any]]:
        """处理一条入站信封。

        Args:
            event_name: 事件名（本处理器只订阅 ``before_message_received``）。
            params: 事件参数，含 ``envelope`` 与 ``adapter_signature``。

        Returns:
            ``(决策, params)``；``params`` 原样返回以保持 key 集合不变。
        """

        runtime = get_queue_runtime()
        if runtime is None or not runtime.settings.enabled:
            return EventDecision.PASS, params

        try:
            acted = await self._handle(runtime, params)
        except Exception as exc:
            # 抛出去会被框架当 PASS 放行，但会丢掉上下文；这里显式记录后放行
            logger.error(f"弹幕排队处理失败，已放行给对话流程: {exc}", exc_info=True)
            return EventDecision.PASS, params

        if not acted:
            return EventDecision.PASS, params

        if runtime.settings.block_message_from_llm:
            return EventDecision.STOP, params
        return EventDecision.SUCCESS, params

    # ── 业务 ──────────────────────────────────────────

    async def _handle(self, runtime: QueueRuntime, params: dict[str, Any]) -> bool:
        """识别意图并执行对应操作。

        Args:
            runtime: 排队运行时。
            params: 事件参数。

        Returns:
            是否「处理了这条弹幕」。``False`` 表示放行给对话流程。
        """

        info = extract_danmaku(params.get("envelope"))
        if not info.is_incoming or not info.text:
            return False
        if not runtime.supports_platform(info.platform):
            return False

        if runtime.settings.log_danmaku:
            logger.debug(f"弹幕[{info.platform}/{info.group_id}] {info.display_name}: {info.text}")

        intent = runtime.parse(info.text)
        if intent.intent is Intent.NONE:
            return False

        if intent.intent is Intent.JOIN:
            await self._do_join(runtime, info, intent)
            return True

        if intent.intent is Intent.LEAVE:
            await self._do_leave(runtime, info, intent)
            return True

        if intent.intent is Intent.STATUS:
            return await self._do_status(runtime, info)

        if intent.intent is Intent.ADMIN:
            return await self._do_admin(runtime, info, intent)

        return False

    async def _do_join(
        self, runtime: QueueRuntime, info: DanmakuInfo, intent: ParsedIntent
    ) -> None:
        """处理入队。

        Args:
            runtime: 排队运行时。
            info: 弹幕信息。
            intent: 解析结果。
        """

        payload = await runtime.call(
            "join",
            user_id=info.user_id or info.display_name,
            nickname=info.display_name,
            platform=info.platform,
            queue=intent.queue,
            queue_explicit=intent.queue_explicit,
            extra={
                "guard_level": info.guard_level,
                "fans_medal_level": info.fans_medal_level,
                "bilibili_uid": info.bilibili_uid,
            },
            guild=info.group_id,
            event_id=info.message_id,
        )

        status = str(payload.get("status") or "")
        queue_title = str(payload.get("queue_title") or intent.queue or "默认队")
        queue_names: list[str] = []
        if status in {"queue_required", "queue_not_found"}:
            queue_names = await self._known_queue_names(runtime)
        if payload.get("ok") and status == "joined":
            self._log(runtime, f"{info.display_name} 加入「{queue_title}」第 {payload.get('position', '?')} 位")
        elif status in {"already_in", "duplicate_event"}:
            pass  # 重复入队 / 重复事件是高频噪音，不记日志
        else:
            self._log(runtime, f"{info.display_name} 入队「{queue_title}」未成功：{status}")

        if runtime.settings.reply_on_join and status != "duplicate_event":
            await self._reply(
                runtime,
                info,
                messages.join_reply(
                    payload,
                    join_words=runtime.settings.join_keywords,
                    queue_names=queue_names,
                ),
            )

    async def _known_queue_names(self, runtime: QueueRuntime) -> list[str]:
        """取当前已有的队列名（入队失败时告诉观众有哪些队可排）。"""

        payload = await runtime.call("queues")
        raw = payload.get("queues")
        names: list[str] = []
        if isinstance(raw, list):
            for item in raw:
                name = str(item or "").strip()
                if name and name not in names:
                    names.append(name)
        return names

    async def _do_leave(
        self, runtime: QueueRuntime, info: DanmakuInfo, intent: ParsedIntent
    ) -> None:
        """处理退队。

        Args:
            runtime: 排队运行时。
            info: 弹幕信息。
            intent: 解析结果。
        """

        payload = await runtime.call(
            "leave",
            user_id=info.user_id,
            queue=intent.queue,
            queue_explicit=intent.queue_explicit,
            event_id=info.message_id,
        )

        queues = payload.get("queues")
        titles = [str(item) for item in queues] if isinstance(queues, list) else []
        if titles:
            self._log(runtime, f"{info.display_name} 退出 {'、'.join(titles)}")

        if runtime.settings.reply_on_leave:
            await self._reply(runtime, info, messages.leave_reply(payload, info.display_name))

    async def _do_status(self, runtime: QueueRuntime, info: DanmakuInfo) -> bool:
        """处理队列状态查询。

        Args:
            runtime: 排队运行时。
            info: 弹幕信息。

        Returns:
            是否处理了这条弹幕。配置关闭状态查询时返回 ``False``，让弹幕照常进入
            对话流程。
        """

        if not runtime.settings.queue_status_command:
            return False

        payload = await runtime.call("state")
        await self._reply(runtime, info, messages.status_reply(payload))
        return True

    async def _do_admin(
        self, runtime: QueueRuntime, info: DanmakuInfo, intent: ParsedIntent
    ) -> bool:
        """处理舰长弹幕管理命令。

        Args:
            runtime: 排队运行时。
            info: 弹幕信息。
            intent: 解析结果。

        Returns:
            是否处理了这条弹幕。无权限时返回 ``False``，让弹幕照常进入对话流程
            （管理员关键词如「下一个」都是常见口语，不该被无条件吞掉）。
        """

        if not runtime.settings.allow_danmaku_commands:
            return False
        if not runtime.is_admin(guard_level=info.guard_level, sender_role=info.role):
            return False

        queue = intent.queue
        command = intent.command
        reply: dict[str, Any] | None = None

        if command == "next":
            reply = await runtime.call("call_next", queue=queue)
        elif command == "finish":
            reply = await runtime.call("finish", queue=queue)
        elif command == "requeue":
            reply = await runtime.call("requeue", queue=queue)
        elif command == "clear":
            reply = await runtime.call("clear", queue=queue)
            if reply.get("ok"):
                self._log(runtime, f"{info.display_name} 清空队列 {queue or '(默认)'}")
        elif command in ("pause", "resume"):
            paused = command == "pause"
            reply = await runtime.call("set_paused", queue=queue, paused=paused)
            if reply.get("ok"):
                self._log(runtime, f"{info.display_name} {'暂停' if paused else '恢复'}队列 {queue or '(默认)'}")
        elif command == "list":
            payload = await runtime.call("state")
            await self._reply(runtime, info, messages.status_reply(payload))
            return True
        elif command == "create":
            if not queue:
                await self._reply(runtime, info, "开队需要写队名，例如「开队 排位」。")
                return True
            reply = await runtime.call("create_queue", queue=queue)
            if reply.get("ok"):
                self._log(runtime, f"{info.display_name} 创建队列 {queue}")
        elif command == "delete":
            if not queue:
                await self._reply(runtime, info, "删队需要写队名，例如「删队 排位」。")
                return True
            reply = await runtime.call("delete_queue", queue=queue)
        elif command == "reset":
            reply = await runtime.call("reset")
            if reply.get("ok"):
                self._log(runtime, f"{info.display_name} 重置全部队列")
        else:
            # 未实现的命令：给出帮助而不是静默吞掉
            await self._reply(runtime, info, messages.admin_usage(runtime.settings.command_prefix))
            return True

        if reply is not None:
            text = messages.operation_reply(reply)
            if reply.get("ok"):
                self._log(runtime, f"{info.display_name} {command} -> {text}")
            await self._reply(runtime, info, text)
        return True

    # ── 回复与日志 ────────────────────────────────────

    @staticmethod
    def _stream_id(info: DanmakuInfo) -> str:
        """按信封内容推导聊天流 ID。

        Args:
            info: 弹幕信息。

        Returns:
            聊天流 ID；平台信息不足时返回空字符串。
        """

        if not info.platform:
            return ""
        if info.group_id:
            return ChatStream.generate_stream_id(info.platform, group_id=info.group_id)
        if info.user_id:
            return ChatStream.generate_stream_id(info.platform, user_id=info.user_id)
        return ""

    async def _reply(self, runtime: QueueRuntime, info: DanmakuInfo, text: str) -> None:
        """把反馈文案发给原聊天流（带节流）。

        B 站开放平台禁止第三方 bot 发弹幕，该适配器的出站是 no-op，所以对 B 站
        直播间调用这里不会有任何效果——这是平台限制，不是 bug。对支持出站的平台
        （如 QQ）才会真正送达，因此默认关闭 ``behavior.reply_in_stream``。

        Args:
            runtime: 排队运行时。
            info: 弹幕信息。
            text: 回复文本。
        """

        if not text:
            return

        if not runtime.settings.reply_in_stream:
            # 仍然打进日志，方便主播在控制台/日志里看到排队进展
            logger.info(f"[排队] {text}")
            return

        stream_id = self._stream_id(info)
        if not stream_id:
            return
        if not runtime.reply_allowed(stream_id):
            return

        try:
            await send_text(text, stream_id=stream_id, platform=info.platform or None)
        except Exception as exc:
            logger.debug(f"排队反馈发送失败（不影响队列状态）: {exc}")
        logger.info(f"[排队] {text}")

    @staticmethod
    def _log(runtime: QueueRuntime, text: str) -> None:
        """按配置记录队列操作日志。

        Args:
            runtime: 排队运行时。
            text: 日志正文。
        """

        if runtime.settings.log_commands:
            logger.info(f"[排队] {text}")


__all__ = ["DanmakuQueueHandler"]
