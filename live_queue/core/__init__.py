"""live_queue 的纯内存核心层：队列数据模型与调度器。

这两个模块**不依赖任何第三方库**（连 PySide6 都不依赖），因此可以被单测直接覆盖，
也可以安全地被窗口线程和 HTTP 线程共用（线程安全由 :class:`live_queue.hub.QueueHub`
统一负责）。
"""

from .manager import QueueManager
from .models import (
    DuplicatePolicy,
    JoinResult,
    JoinStatus,
    QueueEntry,
    QueueState,
    format_duration,
)

__all__ = [
    "DuplicatePolicy",
    "JoinResult",
    "JoinStatus",
    "QueueEntry",
    "QueueManager",
    "QueueState",
    "format_duration",
]
