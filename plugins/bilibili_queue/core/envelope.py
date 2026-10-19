"""bilibili_queue 的统一消息信封解析。

把 Neo-MoFox 的 ``MessageEnvelope`` 读成 :class:`DanmakuInfo`。

这里踩过的坑都写进注释，别再踩第二遍：

- 昵称字段是 ``user_nickname``，**不是** ``nickname``。
- ``message_segment`` 只有一段时是**单个 dict**，多段才是 list。
- B 站直播适配器把原始字段塞在 ``message_info.additional_config``（``guard_level``
  等），原始 payload 在 ``envelope["raw_message"]``。
- ``user_info.role`` 是 ``mofox_wire`` 的 ``UserRole`` 枚举，要取 ``.value``。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


def _segment_text(segment: Any) -> str:
    """取单个 text 段的文本。

    Args:
        segment: 一个消息段。

    Returns:
        文本内容；非 text 段返回空字符串。
    """

    if not isinstance(segment, dict):
        return ""
    if segment.get("type") != "text":
        return ""
    data = segment.get("data")
    return data if isinstance(data, str) else ""


def extract_text(envelope: dict[str, Any]) -> str:
    """从信封里提取纯文本。

    兼容三种形态：单段 dict、段列表、以及 ``data`` 里再嵌一层段列表的 seglist。
    没有 ``message_segment`` 时回退读 ``message_chain``（框架的别名写法）。

    Args:
        envelope: 统一消息信封。

    Returns:
        拼接后的纯文本。
    """

    raw = envelope.get("message_segment")
    if raw is None:
        raw = envelope.get("message_chain")

    if isinstance(raw, dict):
        segments: list[Any] = [raw]
    elif isinstance(raw, list):
        segments = list(raw)
    else:
        return ""

    parts: list[str] = []
    for segment in segments:
        text = _segment_text(segment)
        if text:
            parts.append(text)
        if isinstance(segment, dict) and isinstance(segment.get("data"), list):
            for nested in segment["data"]:
                nested_text = _segment_text(nested)
                if nested_text:
                    parts.append(nested_text)
    return "".join(parts)


def _safe_int(value: Any) -> int:
    """把任意输入转成 int。

    Args:
        value: 原始值，可能是 ``None``、字符串或非法内容。

    Returns:
        转换后的整数；转不了时返回 0。
    """

    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


@dataclass
class DanmakuInfo:
    """一条弹幕被读出来的业务字段。

    Attributes:
        text: 弹幕正文。
        platform: 平台标识（B 站直播为 ``bilibili_live``）。
        direction: 消息方向，只处理 ``incoming``。
        message_id: 平台消息 ID，可用于幂等。
        user_id: 平台用户 ID（B 站是 ``open_id``）。
        nickname: 昵称。
        role: 角色字符串（``operator`` / ``member``）。
        group_id: 群 / 直播间号。
        group_name: 群名 / 主播昵称。
        guard_level: B 站大航海等级，0 表示非舰长。
        fans_medal_level: 粉丝勋章等级。
        bilibili_uid: B 站原始 uid（脱敏 ID 之外的备查字段）。
        extra: ``additional_config`` 原文，便于将来扩展。
    """

    text: str = ""
    platform: str = ""
    direction: str = ""
    message_id: str = ""
    user_id: str = ""
    nickname: str = ""
    role: str = ""
    group_id: str = ""
    group_name: str = ""
    guard_level: int = 0
    fans_medal_level: int = 0
    bilibili_uid: Any = None
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def is_incoming(self) -> bool:
        """是否是入站消息。"""

        return (self.direction or "incoming") == "incoming"

    @property
    def display_name(self) -> str:
        """展示用昵称，为空时退化成用户 ID 前 6 位。"""

        if self.nickname:
            return self.nickname
        return f"用户{self.user_id[:6]}" if self.user_id else "匿名观众"


def extract_danmaku(envelope: Any) -> DanmakuInfo:
    """把统一消息信封解析成 :class:`DanmakuInfo`。

    任何字段缺失都退化成安全默认值，绝不抛异常——事件处理器里抛异常会被框架
    当成 ``PASS`` 放行，等于静默漏掉这条弹幕。

    Args:
        envelope: 统一消息信封，理论上应是 dict。

    Returns:
        解析结果；输入不是 dict 时返回全默认值。
    """

    if not isinstance(envelope, dict):
        return DanmakuInfo()

    info = envelope.get("message_info") or {}
    if not isinstance(info, dict):
        info = {}
    user = info.get("user_info") or {}
    if not isinstance(user, dict):
        user = {}
    group = info.get("group_info") or {}
    if not isinstance(group, dict):
        group = {}
    additional = info.get("additional_config") or {}
    if not isinstance(additional, dict):
        additional = {}

    role = user.get("role")
    role_value = getattr(role, "value", role)

    guard_level = additional.get("guard_level", 0)

    return DanmakuInfo(
        text=extract_text(envelope),
        platform=str(info.get("platform") or ""),
        direction=str(envelope.get("direction") or ""),
        message_id=str(info.get("message_id") or ""),
        user_id=str(user.get("user_id") or ""),
        nickname=str(user.get("user_nickname") or ""),
        role=str(role_value or ""),
        group_id=str(group.get("group_id") or ""),
        group_name=str(group.get("group_name") or ""),
        guard_level=_safe_int(guard_level),
        fans_medal_level=_safe_int(additional.get("fans_medal_level")),
        bilibili_uid=additional.get("bilibili_uid"),
        extra=dict(additional),
    )


__all__ = ["DanmakuInfo", "extract_danmaku", "extract_text"]
