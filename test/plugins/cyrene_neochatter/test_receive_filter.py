"""cyrene_neochatter 接收开关（B 站直播 / 私聊 / 群聊）的单测。

关闭开关 = 该类消息在接收管线最上游被 STOP 拦截，后续插件看不到它。
"""

from __future__ import annotations

from typing import Any

import pytest

from plugins.bilibili_queue.components.danmaku_handler import DanmakuQueueHandler
from plugins.cyrene_neochatter import event_handler as handler_module
from plugins.cyrene_neochatter.config import CyreneNeoConfig
from plugins.cyrene_neochatter.plugin import CyreneNeoPlugin
from plugins.cyrene_neochatter.event_handler import (
    BILIBILI_LIVE_PLATFORM,
    CyreneReceiveFilter,
    classify_message,
)
from src.core.components.types import EventType
from src.kernel.event import EventBus, EventDecision


class _DummyPlugin:
    """过滤器只要求一个持有它的对象。"""

    plugin_name = "cyrene_neochatter"
    config = None


def _envelope(*, platform: str = "qq", group: bool = False, message_id: str = "m1") -> dict:
    """造一封入站消息信封。"""

    info: dict[str, Any] = {
        "platform": platform,
        "message_id": message_id,
        "user_info": {"user_id": "open-1", "user_nickname": "阿狸"},
    }
    if group:
        info["group_info"] = {"group_id": "12345", "group_name": "测试群"}
    return {
        "direction": "incoming",
        "message_segment": {"type": "text", "data": "你好"},
        "message_info": info,
    }


@pytest.fixture
def switches(monkeypatch: pytest.MonkeyPatch) -> dict[str, bool]:
    """可控的开关表（默认全开）。"""

    values = {"recv_bilibili_live": True, "recv_private": True, "recv_group": True}

    def fake_get_switch(key: str, default: bool = False) -> bool:
        return bool(values.get(key, default))

    monkeypatch.setattr(handler_module, "get_switch", fake_get_switch)
    return values


# ── 分类 ──────────────────────────────────────────────


def test_classify_bilibili_live() -> None:
    """B 站直播按平台判定，优先于群聊判定（直播间弹幕也带 group_info）。"""

    assert classify_message(_envelope(platform=BILIBILI_LIVE_PLATFORM)) == "recv_bilibili_live"
    assert (
        classify_message(_envelope(platform=BILIBILI_LIVE_PLATFORM, group=True))
        == "recv_bilibili_live"
    )


def test_classify_group_and_private() -> None:
    """有 group_info 是群聊，其它是私聊。"""

    assert classify_message(_envelope(platform="qq", group=True)) == "recv_group"
    assert classify_message(_envelope(platform="qq", group=False)) == "recv_private"


def test_classify_rejects_garbage() -> None:
    """无法识别的输入返回空串（永不误拦）。"""

    assert classify_message(None) == ""
    assert classify_message("not-a-dict") == ""
    assert classify_message({}) == ""
    assert classify_message({"message_info": "oops"}) == ""


# ── 单处理器行为 ──────────────────────────────────────


async def test_all_switches_on_passes(switches: dict[str, bool]) -> None:
    """全开时三种消息都放行。"""

    handler = CyreneReceiveFilter(_DummyPlugin())
    for envelope in (
        _envelope(platform=BILIBILI_LIVE_PLATFORM),
        _envelope(group=True),
        _envelope(group=False),
    ):
        decision, _params = await handler.execute(
            "before_message_received", {"envelope": envelope}
        )
        assert decision is EventDecision.PASS


@pytest.mark.parametrize(
    ("key", "envelope"),
    [
        ("recv_bilibili_live", _envelope(platform=BILIBILI_LIVE_PLATFORM)),
        ("recv_group", _envelope(group=True)),
        ("recv_private", _envelope(group=False)),
    ],
)
async def test_switch_off_stops(
    switches: dict[str, bool], key: str, envelope: dict
) -> None:
    """关掉哪个开关就拦截哪类消息，其它类不受影响。"""

    switches[key] = False
    handler = CyreneReceiveFilter(_DummyPlugin())

    decision, _params = await handler.execute(
        "before_message_received", {"envelope": envelope}
    )
    assert decision is EventDecision.STOP

    # 重新打开后同一条消息又能过
    for name in switches:
        switches[name] = True
    decision, _params = await handler.execute(
        "before_message_received", {"envelope": envelope}
    )
    assert decision is EventDecision.PASS


async def test_unknown_envelope_passes_even_when_all_off(
    switches: dict[str, bool],
) -> None:
    """认不出类别的信封放行（避免误拦框架内部消息）。"""

    for name in switches:
        switches[name] = False
    handler = CyreneReceiveFilter(_DummyPlugin())

    decision, _params = await handler.execute(
        "before_message_received", {"envelope": {"direction": "incoming"}}
    )
    assert decision is EventDecision.PASS


# ── 顺序：必须跑在弹幕插件前面 ────────────────────────


def test_weight_outranks_danmaku_handler() -> None:
    """权重必须高于弹幕处理器，否则「关闭 B 站直播」拦不住排队。"""

    assert CyreneReceiveFilter.weight > DanmakuQueueHandler.weight


async def test_bus_chain_stops_downstream_handlers(switches: dict[str, bool]) -> None:
    """总线级验证：开关关闭时，低优先级处理器完全收不到消息。"""

    bus = EventBus(name="test_receive_filter")
    handler = CyreneReceiveFilter(_DummyPlugin())
    seen: list[str] = []

    async def downstream(event_name: str, params: dict[str, Any]):
        seen.append(str(params.get("envelope", {}).get("message_info", {}).get("message_id")))
        return EventDecision.SUCCESS, params

    bus.subscribe(
        EventType.BEFORE_MESSAGE_RECEIVED,
        handler.execute,
        priority=CyreneReceiveFilter.weight,
    )
    bus.subscribe(EventType.BEFORE_MESSAGE_RECEIVED, downstream, priority=10)

    # 开关关闭 -> STOP，下游收不到
    switches["recv_private"] = False
    decision, _params = await bus.publish(
        EventType.BEFORE_MESSAGE_RECEIVED, {"envelope": _envelope(group=False)}
    )
    assert decision is EventDecision.STOP
    assert seen == []

    # 开关打开 -> 正常传下去
    switches["recv_private"] = True
    decision, _params = await bus.publish(
        EventType.BEFORE_MESSAGE_RECEIVED, {"envelope": _envelope(group=False)}
    )
    assert decision is EventDecision.SUCCESS
    assert seen == ["m1"]

# ── 注册守卫（踩过的坑）──────────────────────────────


def _registered_names(cfg: CyreneNeoConfig) -> set[str]:
    """插件实际会注册的组件名集合。"""

    return {
        getattr(cls, "name", "") or getattr(cls, "handler_name", "")
        for cls in CyreneNeoPlugin(cfg).get_components()
    }


def test_receive_filter_is_registered_by_plugin() -> None:
    """过滤器必须在 get_components() 里。

    组件是靠 plugin.get_components() 注册的，manifest.include 只做声明；
    只写 manifest 不写 get_components 会「开关点了没反应」。
    """

    cfg = CyreneNeoConfig()
    cfg.general.enabled = True
    cfg.dfc_plugin.enabled = False  # 接收开关与 DFC 人格层无关，关掉也要在
    assert "cyrene_receive_filter" in _registered_names(cfg)


def test_manifest_event_handlers_are_all_registered() -> None:
    """manifest 里声明的 event_handler 必须真的被 get_components() 注册。"""

    import json
    from pathlib import Path

    manifest_path = Path(handler_module.__file__).resolve().parent / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    declared = {
        str(item["component_name"])
        for item in manifest.get("include", [])
        if item.get("component_type") == "event_handler"
    }
    cfg = CyreneNeoConfig()
    cfg.general.enabled = True
    cfg.dfc_plugin.enabled = True
    missing = declared - _registered_names(cfg)
    assert not missing, f"manifest 声明了但没注册: {sorted(missing)}"
