"""bilibili_queue 消息信封解析的单测。

信封结构按 ``plugins/bilibili_live_adapter/src/dispatcher.py`` 的真实产出构造，
重点覆盖三个容易读错的点：``user_nickname``、单段 ``message_segment`` 是 dict、
``guard_level`` 在 ``additional_config`` 里。
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from plugins.bilibili_queue.core.envelope import (
    DanmakuInfo,
    extract_danmaku,
    extract_text,
)


def _envelope(
    *,
    text: Any = "排队",
    nickname: str = "阿狸",
    role: Any = None,
    user_id: str = "open-1",
    segment_key: str = "message_segment",
) -> dict[str, Any]:
    """构造一封接近真实的 B 站弹幕信封。

    Args:
        text: 文本内容或完整的段结构。
        nickname: 昵称。
        role: ``user_info.role`` 的值。
        user_id: 用户 ID。
        segment_key: 使用 ``message_segment`` 还是 ``message_chain``。

    Returns:
        信封 dict。
    """

    segment = {"type": "text", "data": text} if isinstance(text, str) else text
    return {
        "direction": "incoming",
        segment_key: segment,
        "message_info": {
            "platform": "bilibili_live",
            "message_id": "msg-1",
            "user_info": {
                "user_id": user_id,
                "user_nickname": nickname,
                "role": role if role is not None else SimpleNamespace(value="member"),
            },
            "group_info": {"group_id": "12345", "group_name": "某主播"},
            "additional_config": {
                "bilibili_uid": 998877,
                "guard_level": 3,
                "fans_medal_level": 7,
                "fans_medal_name": "小狐狸",
            },
        },
    }


# ── extract_text ──────────────────────────────────────


def test_extract_text_from_single_dict_segment() -> None:
    """B 站弹幕只有一段，``message_segment`` 是 dict 而不是 list。"""

    assert extract_text(_envelope(text="排队 排位")) == "排队 排位"


def test_extract_text_from_segment_list() -> None:
    """多段时应拼接所有 text 段。"""

    envelope = _envelope(
        text=[
            {"type": "text", "data": "前"},
            {"type": "image", "data": "http://x/y.png"},
            {"type": "text", "data": "后"},
        ]
    )
    assert extract_text(envelope) == "前后"


def test_extract_text_from_message_chain_alias() -> None:
    """没有 ``message_segment`` 时回退读 ``message_chain``。"""

    envelope = _envelope(text="别名通道", segment_key="message_chain")
    assert extract_text(envelope) == "别名通道"


def test_extract_text_from_nested_seglist() -> None:
    """``data`` 里再嵌一层段列表（seglist）也要能取到文本。"""

    envelope = _envelope(
        text={
            "type": "seglist",
            "data": [
                {"type": "text", "data": "嵌"},
                {"type": "text", "data": "套"},
            ],
        }
    )
    assert extract_text(envelope) == "嵌套"


def test_extract_text_ignores_non_text_and_broken_segments() -> None:
    """非 text 段、非 dict 段、非字符串 data 都安全忽略。"""

    envelope = _envelope(
        text=[{"type": "text", "data": 123}, "垃圾", None, {"type": "text", "data": "好"}]
    )
    assert extract_text(envelope) == "好"


def test_extract_text_without_segment_returns_empty() -> None:
    """完全没有内容段时返回空串。"""

    assert extract_text({"message_info": {}}) == ""
    assert extract_text({}) == ""


# ── extract_danmaku ───────────────────────────────────


def test_extract_danmaku_full_fields() -> None:
    """完整信封要把业务字段都读出来。"""

    info = extract_danmaku(_envelope(text="排队 排位"))

    assert info.text == "排队 排位"
    assert info.platform == "bilibili_live"
    assert info.direction == "incoming"
    assert info.message_id == "msg-1"
    assert info.user_id == "open-1"
    assert info.nickname == "阿狸"
    assert info.group_id == "12345"
    assert info.group_name == "某主播"
    assert info.guard_level == 3
    assert info.fans_medal_level == 7
    assert info.bilibili_uid == 998877
    assert info.is_incoming is True


def test_extract_danmaku_reads_role_enum_value() -> None:
    """``UserRole`` 是枚举，要取 ``.value`` 而不是枚举对象本身。"""

    info = extract_danmaku(_envelope(role=SimpleNamespace(value="operator")))
    assert info.role == "operator"


def test_extract_danmaku_accepts_plain_string_role() -> None:
    """普通字符串角色也要能用。"""

    info = extract_danmaku(_envelope(role="operator"))
    assert info.role == "operator"


def test_extract_danmaku_without_role() -> None:
    """没有角色字段时退化成空串。"""

    envelope = _envelope()
    envelope["message_info"]["user_info"].pop("role")
    assert extract_danmaku(envelope).role == ""


def test_extract_danmaku_tolerates_missing_fields() -> None:
    """字段大面积缺失也不能抛异常，否则弹幕会被静默放行给 LLM。"""

    info = extract_danmaku({})
    assert isinstance(info, DanmakuInfo)
    assert info.text == ""
    assert info.user_id == ""
    assert info.guard_level == 0


def test_extract_danmaku_rejects_non_dict() -> None:
    """信封不是 dict 时返回全默认值。"""

    assert extract_danmaku(None).text == ""
    assert extract_danmaku("oops").text == ""  # type: ignore[arg-type]


def test_extract_danmaku_tolerates_wrong_typed_sections() -> None:
    """``message_info`` / ``user_info`` 类型不对时按空处理。"""

    envelope = {
        "message_segment": {"type": "text", "data": "排队"},
        "message_info": "not-a-dict",
    }
    info = extract_danmaku(envelope)
    assert info.text == "排队"
    assert info.platform == ""

    envelope["message_info"] = {"user_info": "bad", "group_info": 5, "additional_config": "x"}
    info = extract_danmaku(envelope)
    assert info.user_id == ""
    assert info.group_id == ""
    assert info.guard_level == 0


def test_extract_danmaku_guard_level_garbage_becomes_zero() -> None:
    """``guard_level`` 是脏数据时按 0 处理，不能影响后续比较。"""

    envelope = _envelope()
    envelope["message_info"]["additional_config"]["guard_level"] = "船长"
    assert extract_danmaku(envelope).guard_level == 0


def test_is_incoming_defaults_to_true_when_direction_missing() -> None:
    """没有 direction 字段时按入站处理（适配器一定会给，但不该因此丢消息）。"""

    envelope = _envelope()
    envelope.pop("direction")
    assert extract_danmaku(envelope).is_incoming is True


def test_is_incoming_false_for_outgoing() -> None:
    """出站信封不应被当成弹幕处理。"""

    envelope = _envelope()
    envelope["direction"] = "outgoing"
    assert extract_danmaku(envelope).is_incoming is False


def test_display_name_fallbacks() -> None:
    """昵称为空时用用户 ID 兜底，都没有时给一个中性称呼。"""

    assert extract_danmaku(_envelope(nickname="")).display_name == "用户open-1"
    assert extract_danmaku(_envelope(nickname="", user_id="")).display_name == "匿名观众"
