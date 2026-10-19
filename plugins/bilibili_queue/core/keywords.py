"""bilibili_queue 弹幕文本解析。

把一条弹幕文本解析成「意图 + 队名」：

- ``排队``            → 排进默认队
- ``排队 排位``       → 排进「排位」队
- ``排队：排位赛``    → 别名解析后进「排位」队
- ``退队``            → 退出自己所在的队列
- ``上一个 排位``     → 房管叫号（需要权限，由上层判断）
- ``随便聊聊``        → 与排队无关，返回 ``NONE``

匹配采用**最长关键词优先**，所以「排队情况」会命中状态查询而不是入队关键词
「排队」+ 队名「情况」。
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from enum import Enum

_MAX_QUEUE_NAME_LENGTH = 16
"""队名最大长度，与 :mod:`plugins.bilibili_queue.core.manager` 保持一致。"""

_SEPARATORS = " \t\u3000:：,，、-—–_|/\\"
"""关键词与队名之间允许出现的分隔符（含全角空格与常见中英标点）。"""


class Intent(str, Enum):
    """一条弹幕的意图。"""

    JOIN = "join"
    """入队。"""

    LEAVE = "leave"
    """退队。"""

    STATUS = "status"
    """查询队列状态。"""

    ADMIN = "admin"
    """房管管理命令，具体动作见 :attr:`ParsedIntent.command`。"""

    NONE = "none"
    """与排队无关。"""


# 房管命令的内置触发词表。命令名 → 触发词，按需增删即可。
ADMIN_COMMANDS: dict[str, tuple[str, ...]] = {
    "next": ("下一个", "下一位", "叫号", "叫下一个", "next"),
    "finish": ("结束当前", "结束", "完成了", "done"),
    "requeue": ("放回队首", "放回", "回退", "重新排队", "back"),
    "clear": ("清空队列", "清空", "clear"),
    "pause": ("暂停排队", "暂停", "pause"),
    "resume": ("恢复排队", "继续排队", "恢复", "resume"),
    "list": ("查看队列", "队列状态", "list"),
    "create": ("开队", "建队", "新建队列"),
    "delete": ("删队", "删除队列", "关闭队列"),
    "reset": ("重置队列",),
}
"""房管弹幕命令的内置触发词。上层按 ``admin.guard_level`` 做权限校验。"""


@dataclass
class ParsedIntent:
    """一次弹幕解析的结果。

    Attributes:
        intent: 解析出的意图。
        keyword: 命中的关键词原文。
        queue: 解析出的队名原始文本（可能为空，表示使用默认队）。
        queue_explicit: 队名与关键词之间**有分隔符**，说明用户明确写了队名。
            没有分隔符时（例如「排队一下」）队名是否可信由上层再判断，
            避免把闲聊误判成队名。
        command: ``intent == ADMIN`` 时的动作名，例如 ``next``。
        text: 原始弹幕文本。
    """

    intent: Intent = Intent.NONE
    keyword: str = ""
    queue: str = ""
    queue_explicit: bool = False
    command: str = ""
    text: str = ""

    @property
    def is_actionable(self) -> bool:
        """是否应当拦截这条弹幕、不让它进入 LLM 对话。"""

        return self.intent is not Intent.NONE


def _normalize(text: str, ignore_case: bool) -> str:
    """统一大小写，便于英文关键词匹配。

    Args:
        text: 原始文本。
        ignore_case: 是否忽略大小写。

    Returns:
        归一化后的文本。
    """

    return text.lower() if ignore_case else text


def _strip_leading_separators(text: str) -> tuple[str, bool]:
    """去掉队名前的分隔符，并告知原本是否存在分隔符。

    Args:
        text: 已切掉关键词的剩余文本。

    Returns:
        ``(去掉前导分隔符的文本, 是否存在分隔符)``。
    """

    stripped = text.lstrip(_SEPARATORS)
    return stripped, len(stripped) != len(text)


def looks_like_queue_name(text: str) -> bool:
    """粗略判断一段文本是否像个合法队名。

    Args:
        text: 候选文本。

    Returns:
        长度与管理器约束一致且不含空白时返回 ``True``。
    """

    if not text or len(text) > _MAX_QUEUE_NAME_LENGTH:
        return False
    return not any(ch.isspace() for ch in text)


class KeywordMatcher:
    """把配置里的关键词表编译成「最长优先」匹配器。

    实例不可变，配置变更后重新构造即可。
    """

    def __init__(
        self,
        *,
        join: Iterable[str] = (),
        leave: Iterable[str] = (),
        status: Iterable[str] = (),
        admin_commands: dict[str, tuple[str, ...]] | None = None,
        prefix: str = "",
        ignore_case: bool = True,
    ) -> None:
        """编译关键词表。

        Args:
            join: 入队关键词。
            leave: 退队关键词。
            status: 状态查询关键词。
            admin_commands: 房管命令表；``None`` 时使用 :data:`ADMIN_COMMANDS`。
            prefix: 房管命令前缀；非空时房管命令必须带前缀才会命中。
            ignore_case: 匹配时是否忽略大小写。
        """

        self._ignore_case = bool(ignore_case)
        self._prefix = prefix.strip()

        entries: list[tuple[str, Intent, str]] = []
        for keyword in join:
            if keyword:
                entries.append((str(keyword), Intent.JOIN, ""))
        for keyword in leave:
            if keyword:
                entries.append((str(keyword), Intent.LEAVE, ""))
        for keyword in status:
            if keyword:
                entries.append((str(keyword), Intent.STATUS, ""))

        table = ADMIN_COMMANDS if admin_commands is None else admin_commands
        for command, keywords in table.items():
            for keyword in keywords:
                if keyword:
                    entries.append((str(keyword), Intent.ADMIN, str(command)))

        if self._prefix:
            # 带前缀时，把前缀拼进关键词一起比较，天然实现「必须带前缀」。
            entries = [
                (f"{self._prefix}{keyword}", intent, command)
                for keyword, intent, command in entries
            ]

        # 最长优先，避免短词吃掉长词（如「排队」吃掉「排队情况」）
        entries.sort(key=lambda item: len(item[0]), reverse=True)
        self._entries = entries

    @property
    def join_words(self) -> list[str]:
        """返回当前生效的入队关键词（含前缀，便于提示文案使用）。"""

        return [keyword for keyword, intent, _ in self._entries if intent is Intent.JOIN]

    def parse(self, text: str) -> ParsedIntent:
        """解析一条弹幕。

        Args:
            text: 弹幕正文。

        Returns:
            :class:`ParsedIntent`；未命中任何关键词时 ``intent`` 为 ``NONE``。
        """

        raw = (text or "").strip()
        if not raw:
            return ParsedIntent(intent=Intent.NONE, text=text)

        normalized = _normalize(raw, self._ignore_case)
        for keyword, intent, command in self._entries:
            probe = _normalize(keyword, self._ignore_case)
            if not normalized.startswith(probe):
                continue

            remainder = raw[len(keyword) :]
            queue, explicit = _strip_leading_separators(remainder)
            queue = queue.strip()
            # 入队时如果「队名」不像队名（太长/带空白），退化成默认队，
            # 避免把「排队的人好多啊」这种闲聊误判成排进「的人好多啊」队。
            if intent is Intent.JOIN and not looks_like_queue_name(queue):
                queue = ""
                explicit = False
            return ParsedIntent(
                intent=intent,
                keyword=keyword,
                queue=queue,
                queue_explicit=explicit,
                command=command,
                text=raw,
            )

        return ParsedIntent(intent=Intent.NONE, text=raw)


__all__ = [
    "ADMIN_COMMANDS",
    "Intent",
    "KeywordMatcher",
    "ParsedIntent",
    "looks_like_queue_name",
]
