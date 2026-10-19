"""bilibili_queue 命令组件共用的小工具。

命令组件拿到的是已经转换好的 :class:`~src.core.models.message.Message`，本模块
负责把它读成排队需要的发送者信息。B 站的大航海等级只存在于原始 payload 里
（``Message.raw_data``），所以这里要顺着 ``raw_data`` 再捞一次。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass
class SenderInfo:
    """一条命令的发送者信息。

    Attributes:
        user_id: 平台用户 ID。
        nickname: 昵称。
        platform: 平台标识。
        group_id: 群号 / 直播间号。
        guard_level: B 站大航海等级（0 表示非舰长）。
        role: 统一消息模型里的角色字符串。
    """

    user_id: str = ""
    nickname: str = ""
    platform: str = ""
    group_id: str = ""
    guard_level: int = 0
    role: str = ""

    @property
    def display_name(self) -> str:
        """展示用昵称，为空时退化成用户 ID 前 6 位。"""

        if self.nickname:
            return self.nickname
        return f"用户{self.user_id[:6]}" if self.user_id else "未知用户"


def _first_int(source: Any, key: str) -> int:
    """从一个 dict 里安全取整数。

    Args:
        source: 候选 dict。
        key: 字段名。

    Returns:
        整数值；取不到时返回 0。
    """

    if not isinstance(source, dict):
        return 0
    try:
        return int(source.get(key) or 0)
    except (TypeError, ValueError):
        return 0


def sender_info(message: Any) -> SenderInfo:
    """从命令携带的 ``Message`` 上提取发送者信息。

    任何字段缺失都退化成默认值，绝不抛异常——命令拿不到发送者信息应该还能
    给出可读的错误提示，而不是崩掉。

    Args:
        message: ``BaseCommand._message``，可能是 ``None``。

    Returns:
        提取出的 :class:`SenderInfo`。
    """

    if message is None:
        return SenderInfo()

    extra = getattr(message, "extra", None) or {}
    raw = getattr(message, "raw_data", None)

    group_id = ""
    if isinstance(extra, dict):
        group_id = str(extra.get("group_id") or "")

    guard_level = _first_int(raw, "guard_level")
    if guard_level == 0:
        guard_level = _first_int(extra, "guard_level")

    return SenderInfo(
        user_id=str(getattr(message, "sender_id", "") or ""),
        nickname=str(getattr(message, "sender_name", "") or ""),
        platform=str(getattr(message, "platform", "") or ""),
        group_id=group_id,
        guard_level=guard_level,
        role=str(getattr(message, "sender_role", "") or ""),
    )


__all__ = ["SenderInfo", "sender_info"]
