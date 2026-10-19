"""独立弹幕桥的纯函数单测（不连网、不需要 B 站凭证）。"""

from __future__ import annotations

from pathlib import Path

from live_queue.bilibili import KeywordMatcher
from live_queue.bilibili_bridge import (
    DANMAKU_CMD,
    BilibiliSection,
    BridgeConfig,
    _section,
    build_action,
    extract_danmaku_data,
    load_config,
    save_config,
)


def _matcher() -> KeywordMatcher:
    """和默认配置一致的最小匹配器。"""

    return KeywordMatcher(join=["排队", "上车"], leave=["退队"], status=["排队情况"])


def _payload(text: str, **data: object) -> dict:
    """造一个 op=5 弹幕包。"""

    base: dict = {
        "msg": text,
        "uname": "阿狸",
        "open_id": "open-1",
        "msg_id": "m1",
        "room_id": 123,
        "guard_level": 3,
        "fans_medal_level": 7,
    }
    base.update(data)
    return {"cmd": DANMAKU_CMD, "data": base}


def test_extract_danmaku_data_filters_other_cmds() -> None:
    """只有弹幕包会被桥处理。"""

    assert extract_danmaku_data({"cmd": "LIVE_OPEN_PLATFORM_LIKE", "data": {}}) is None
    assert extract_danmaku_data({"cmd": DANMAKU_CMD, "data": {"msg": "排队"}}) == {"msg": "排队"}
    assert extract_danmaku_data("not a dict") is None


def test_join_action_carries_event_id_and_open_id() -> None:
    """入队请求必须带 open_id 与 msg_id（幂等与身份统一的关键）。"""

    action = build_action(extract_danmaku_data(_payload("排队 王棋")) or {}, _matcher())
    assert action is not None
    assert action.kind == "join"
    assert action.queue == "王棋"
    assert action.queue_explicit is True
    assert action.user_id == "open-1"
    assert action.event_id == "m1"

    payload = action.join_payload()
    assert payload["platform"] == "bilibili_live"
    assert payload["event_id"] == "m1"
    assert payload["guild"] == "123"
    assert payload["extra"]["guard_level"] == 3
    assert payload["extra"]["source"] == "bilibili_bridge"


def test_open_id_falls_back_to_uid() -> None:
    """没有 open_id 时退回原始 uid，保证 user_id 非空。"""

    data = extract_danmaku_data(_payload("排队", open_id="", uid=42)) or {}
    action = build_action(data, _matcher())
    assert action is not None
    assert action.user_id == "42"


def test_chatter_is_ignored() -> None:
    """闲聊不拦。"""

    assert build_action(extract_danmaku_data(_payload("今天天气不错")) or {}, _matcher()) is None


def test_admin_commands_are_not_handled_by_bridge() -> None:
    """房管命令由 bot / 排队姬窗口负责，桥不碰（否则可能重复叫号）。"""

    assert build_action(extract_danmaku_data(_payload("下一个")) or {}, _matcher()) is None
    assert build_action(extract_danmaku_data(_payload("清空 王棋")) or {}, _matcher()) is None


def test_leave_action_payload() -> None:
    """退队请求同样带 event_id。"""

    action = build_action(extract_danmaku_data(_payload("退队 王棋")) or {}, _matcher())
    assert action is not None
    assert action.kind == "leave"
    assert action.leave_payload()["event_id"] == "m1"
    assert action.leave_payload()["queue"] == "王棋"


def test_status_action_is_recognized() -> None:
    """状态查询能被识别出来（桥只打日志，不能回弹幕）。"""

    action = build_action(extract_danmaku_data(_payload("排队情况")) or {}, _matcher())
    assert action is not None
    assert action.kind == "status"


def test_empty_msg_is_ignored() -> None:
    """空弹幕直接跳过。"""

    assert build_action({"msg": "   "}, _matcher()) is None


def test_config_round_trip(tmp_path: Path) -> None:
    """配置读写往返。"""

    path = tmp_path / "bridge_config.json"
    config = BridgeConfig()
    config.bilibili.app_id = 123
    config.bilibili.access_key_id = "key"
    config.keywords.join = ["排队"]
    save_config(path, config)

    loaded = load_config(path)
    assert loaded.bilibili.app_id == 123
    assert loaded.bilibili.access_key_id == "key"
    assert loaded.keywords.join == ["排队"]
    assert loaded.app.base_url == "http://127.0.0.1:17650"
    assert loaded.behavior.handle_admin is False


def test_load_config_creates_template(tmp_path: Path) -> None:
    """配置不存在时生成模板（并报出还缺哪些凭证）。"""

    path = tmp_path / "sub" / "bridge_config.json"
    config = load_config(path)
    assert path.exists()
    assert config.bilibili.missing() == [
        "access_key_id",
        "access_key_secret",
        "app_id",
        "id_code",
    ]


def test_load_config_survives_broken_json(tmp_path: Path) -> None:
    """配置坏了也要能起来（按默认值）。"""

    path = tmp_path / "bridge_config.json"
    path.write_text("{ 坏掉的", encoding="utf-8")
    config = load_config(path)
    assert config.behavior.enabled is True
    assert config.app.base_url == "http://127.0.0.1:17650"


def test_section_ignores_unknown_keys_and_bad_shape() -> None:
    """段落里的未知键忽略，整段不是 dict 时退回默认。"""

    section = _section(BilibiliSection, {"host": "http://x", "unknown": 1})
    assert section.host == "http://x"
    assert _section(BilibiliSection, "not-a-dict").host == "https://live-open.biliapi.com"


def test_bilibili_section_missing_reports_all_gaps() -> None:
    """凭证检查一次报全。"""

    section = BilibiliSection(access_key_id="k", app_id=1)
    assert section.missing() == ["access_key_secret", "id_code"]
