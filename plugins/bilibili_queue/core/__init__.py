"""bilibili_queue 插件的核心层。

这里只放**弹幕侧**的东西：把弹幕文本解析成意图、把 app 的返回翻译成回复文案。
队列状态本身由独立应用 ``live_queue`` 持有，插件通过
:mod:`plugins.bilibili_queue.core.client` 访问它，不在这个包里存任何状态。

这个 ``__init__`` 刻意只导出不依赖框架 API 的模块（关键词解析与文案），
这样它们可以被单测直接覆盖：

- :mod:`plugins.bilibili_queue.core.envelope`：统一消息信封 → 弹幕字段
- :mod:`plugins.bilibili_queue.core.keywords`：弹幕文本 → 意图
- :mod:`plugins.bilibili_queue.core.messages`：回复文案
- :mod:`plugins.bilibili_queue.core.client`：访问独立 app 的 HTTP 客户端
- :mod:`plugins.bilibili_queue.core.supervisor`：独立 app 的进程管理
- :mod:`plugins.bilibili_queue.core.runtime`：上面的组合，组件统一入口
"""

from .keywords import ADMIN_COMMANDS, Intent, KeywordMatcher, ParsedIntent
from .messages import (
    admin_usage,
    format_wait,
    join_reply,
    leave_reply,
    operation_reply,
    queue_line,
    status_reply,
)

__all__ = [
    "ADMIN_COMMANDS",
    "Intent",
    "KeywordMatcher",
    "ParsedIntent",
    "admin_usage",
    "format_wait",
    "join_reply",
    "leave_reply",
    "operation_reply",
    "queue_line",
    "status_reply",
]
