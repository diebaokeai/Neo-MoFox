"""live_queue 桌面窗口的单测。

窗口逻辑跑在 Qt 离屏后端（``QT_QPA_PLATFORM=offscreen``，见 ``conftest.py``），
所以不会在开发机上真的弹窗。这里验证的是「窗口是否正确反映并驱动 hub 状态」，
像素级的渲染由 ``live_queue/app.py`` 的实际运行来保证。
"""

from __future__ import annotations

import threading
from pathlib import Path

import pytest

pytest.importorskip("PySide6", reason="窗口功能依赖 PySide6，未安装时跳过")

from live_queue.config import AppConfig
from live_queue.hub import QueueHub
from live_queue.store import QueueStore
from live_queue.ui.window import (
    QueueWindow,
    create_application,
    human_wait,
)


@pytest.fixture(scope="module")
def qt_app():
    """整个模块共用一个 QApplication（Qt 不允许创建多个）。"""

    return create_application()


@pytest.fixture
def hub(tmp_path: Path, legacy_config: AppConfig) -> QueueHub:
    """不带维护线程的 hub（窗口测试不需要落盘）。"""

    return QueueHub(config=legacy_config, store=QueueStore(tmp_path / "queues.json"))


@pytest.fixture
def window(qt_app, hub: QueueHub) -> QueueWindow:
    """构造并显示窗口。"""

    instance = QueueWindow(hub=hub, server_url="http://127.0.0.1:17650", confirm_on_close=False)
    instance.refresh()
    yield instance
    instance.detach()
    instance.close()
    instance.deleteLater()


# ── 纯函数 ────────────────────────────────────────────


@pytest.mark.parametrize(
    ("seconds", "expected"),
    [
        (0, "0s"),
        (45, "45s"),
        (60, "1m"),
        (95, "1m35s"),
        (3600, "1h"),
        (3900, "1h5m"),
        (-3, "0s"),
    ],
)
def test_human_wait(seconds: float, expected: str) -> None:
    """等待时长格式化。"""

    assert human_wait(seconds) == expected


# ── 窗口与 hub 的联动 ─────────────────────────────────


def test_window_reflects_hub_state(window: QueueWindow, hub: QueueHub) -> None:
    """hub 里加人后，刷新能把队列渲染出来。"""

    hub.join(user_id="u1", nickname="阿狸", queue="排位")
    window.refresh()

    assert "排位" in window._cards
    card = window._cards["排位"]
    assert card._title.text() == "排位"
    assert "1 人" in card._badges.text()
    assert card._current.isVisible() is False  # 还没叫号


def test_window_shows_current(window: QueueWindow, hub: QueueHub) -> None:
    """叫号后卡片要显示「当前」。"""

    hub.join(user_id="u1", nickname="阿狸", queue="排位")
    hub.call_next("排位")
    window.refresh()

    card = window._cards["排位"]
    assert card._current_name.text() == "阿狸"
    assert "#1" in card._current_ticket.text()


def test_window_rebuilds_cards_on_queue_change(window: QueueWindow, hub: QueueHub) -> None:
    """队列集合变化时重建卡片。"""

    hub.create_queue("排位")
    hub.create_queue("娱乐")
    window.refresh()
    assert set(window._cards) == {"排位", "娱乐"}

    hub.delete_queue("娱乐")
    window.refresh()
    assert set(window._cards) == {"排位"}


def test_window_state_change_signal_is_wired(window: QueueWindow, hub: QueueHub) -> None:
    """同线程内的变更会立刻刷新（信号直连）。"""

    hub.join(user_id="u1", nickname="阿狸", queue="排位")
    # 不手动 refresh：hub 的监听回调应该已经把卡片建好了
    assert "排位" in window._cards


def test_window_updates_after_cross_thread_change(qt_app, window: QueueWindow, hub: QueueHub) -> None:
    """跨线程变更通过 QueuedConnection 投递，处理事件后窗口跟上。"""

    def worker() -> None:
        hub.join(user_id="u2", nickname="小满", queue="跨线程队")

    thread = threading.Thread(target=worker)
    thread.start()
    thread.join(timeout=5)

    qt_app.processEvents()
    assert "跨线程队" in window._cards


def test_window_paused_badge(window: QueueWindow, hub: QueueHub) -> None:
    """暂停状态要有标记，按钮文案也要跟着变。"""

    hub.join(user_id="u1", queue="排位")
    hub.set_paused("排位", paused=True)
    window.refresh()

    card = window._cards["排位"]
    assert "已暂停" in card._badges.text()
    assert card._button_pause.text() == "恢复"


def test_window_buttons_visibility(window: QueueWindow, hub: QueueHub, qt_app) -> None:
    """``show_buttons=false`` 时不显示操作入口（会被 OBS 抓到画面里）。"""

    hub.config.window.show_buttons = False
    hidden = QueueWindow(hub=hub, confirm_on_close=False)
    try:
        hub.create_queue("排位")
        hidden.refresh()
        card = hidden._cards["排位"]
        assert card._button_pause.isVisible() is False
    finally:
        hidden.detach()
        hidden.deleteLater()


# ── 窗口驱动 hub ──────────────────────────────────────


def test_run_action_calls_next(window: QueueWindow, hub: QueueHub) -> None:
    """窗口按钮直接调 hub（不走 HTTP）。"""

    hub.join(user_id="u1", nickname="阿狸", queue="排位")
    window.refresh()

    result = window.run_action("/api/call_next", {"queue": "排位"})

    assert result["ok"] is True
    assert hub.snapshot(queue="排位")["queues"][0]["current"]["nickname"] == "阿狸"
    assert "阿狸" in window._toast.text()


def test_run_action_reports_failure(window: QueueWindow) -> None:
    """失败要在底部提示出来。"""

    result = window.run_action("/api/call_next", {"queue": "不存在的队"})
    assert result["ok"] is False
    assert window._toast.text().startswith("⛔")


def test_run_action_unknown_path(window: QueueWindow) -> None:
    """未知操作返回明确失败。"""

    result = window.run_action("/api/nope", {})
    assert result["ok"] is False
    assert "未知操作" in result["message"]


def test_run_action_pause_and_clear(window: QueueWindow, hub: QueueHub) -> None:
    """暂停、清空、删除、置顶、移除、手动加人都能通过窗口触发。"""

    hub.join(user_id="u1", nickname="阿狸", queue="排位")
    hub.join(user_id="u2", nickname="小满", queue="排位")

    assert window.run_action("/api/pause", {"queue": "排位", "paused": True})["ok"] is True
    assert hub.snapshot(queue="排位")["queues"][0]["paused"] is True

    assert window.run_action("/api/capacity", {"queue": "排位", "capacity": 4})["ok"] is True
    assert hub.snapshot(queue="排位")["queues"][0]["capacity"] == 4

    assert window.run_action(
        "/api/move_front", {"queue": "排位", "user_id": "u2"}
    )["ok"] is True
    assert hub.snapshot(queue="排位")["queues"][0]["entries"][0]["user_id"] == "u2"

    assert window.run_action("/api/remove", {"queue": "排位", "user_id": "u2"})["ok"] is True

    # 队列还处于暂停状态，手动加人必须先恢复（暂停时拒绝入队是正确行为）
    assert window.run_action("/api/add", {"queue": "排位", "nickname": "手动"})["ok"] is False
    assert window.run_action("/api/pause", {"queue": "排位", "paused": False})["ok"] is True
    assert window.run_action(
        "/api/add", {"queue": "排位", "nickname": "手动"}
    )["ok"] is True
    assert window.run_action("/api/clear", {"queue": "排位"})["ok"] is True

    assert window.run_action("/api/create", {"queue": "新队"})["ok"] is True
    assert window.run_action("/api/delete", {"queue": "新队"})["ok"] is True


# ── 生命周期 ──────────────────────────────────────────


def test_detach_stops_notifications(window: QueueWindow, hub: QueueHub) -> None:
    """detach 之后 hub 的变更不再打到窗口。"""

    window.detach()
    before = dict(window._cards)

    hub.join(user_id="u1", nickname="阿狸", queue="排位")

    assert dict(window._cards) == before


def test_detach_is_idempotent(window: QueueWindow) -> None:
    """重复 detach 安全。"""

    window.detach()
    window.detach()


def test_close_event_accepts_without_confirmation(qt_app, hub: QueueHub) -> None:
    """``confirm_on_close=False`` 时直接关。"""

    instance = QueueWindow(hub=hub, confirm_on_close=False)
    try:
        accepted: list[bool] = []

        class FakeEvent:
            """最小化的事件替身。"""

            def accept(self) -> None:
                """记录 accept。"""

                accepted.append(True)

            def ignore(self) -> None:
                """记录 ignore。"""

                accepted.append(False)

        instance.closeEvent(FakeEvent())  # type: ignore[arg-type]
        assert accepted == [True]
    finally:
        instance.detach()
        instance.deleteLater()


def test_close_event_asks_for_confirmation(qt_app, hub: QueueHub, monkeypatch) -> None:
    """默认会先确认（关窗等于停掉排队服务）。"""

    instance = QueueWindow(hub=hub, confirm_on_close=True)
    try:
        asked: list[str] = []
        monkeypatch.setattr(
            instance, "confirm", lambda text: asked.append(text) or False
        )

        accepted: list[bool] = []

        class FakeEvent:
            """最小化的事件替身。"""

            def accept(self) -> None:
                """记录 accept。"""

                accepted.append(True)

            def ignore(self) -> None:
                """记录 ignore。"""

                accepted.append(False)

        instance.closeEvent(FakeEvent())  # type: ignore[arg-type]
        assert asked and "机器人将无法再排队" in asked[0]
        assert accepted == [False]
    finally:
        instance.detach()
        instance.deleteLater()


def test_window_uses_config_title_and_size(
    qt_app, tmp_path: Path, legacy_config: AppConfig
) -> None:
    """窗口标题与尺寸来自配置。"""

    config = legacy_config
    config.window.title = "我的排队姬"
    config.window.width = 500
    config.window.height = 700
    hub = QueueHub(config=config, store=QueueStore(tmp_path / "q.json"))

    instance = QueueWindow(hub=hub, confirm_on_close=False)
    try:
        assert instance.windowTitle() == "我的排队姬"
        assert instance.width() == 500
        assert instance.height() == 700
    finally:
        instance.detach()
        instance.deleteLater()
