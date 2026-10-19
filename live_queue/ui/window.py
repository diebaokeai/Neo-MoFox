"""live_queue 的桌面窗口（PySide6）。

和之前的「拉 HTTP 的客户端」写法不同：窗口和 HTTP 服务**在同一个进程里**，所以

- 状态变更由 :class:`~live_queue.hub.QueueHub` 的监听回调主动推过来（不用轮询自己的
  接口），跨线程通过 Qt 信号投递到 UI 线程；
- 按钮直接调 hub 的方法，不走 HTTP，点了立刻生效；
- 只留一个 1 秒的定时器用来刷新「已等待多久」这类会自己变化的内容。

窗口关闭 = 退出整个应用（排队服务也会停），所以关窗前会确认一次。
"""

from __future__ import annotations

from typing import Any

from PySide6.QtCore import Qt, QTimer, QUrl, Signal
from PySide6.QtGui import QDesktopServices, QFont
from PySide6.QtWidgets import (
    QApplication,
    QFrame,
    QHBoxLayout,
    QLabel,
    QMenu,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from ..hub import QueueHub

_REFRESH_INTERVAL_MS = 1000
"""只用于刷新「等待时长」这类自行变化的内容。"""


def human_wait(seconds: float) -> str:
    """把等待秒数压成紧凑文本。

    Args:
        seconds: 等待秒数。

    Returns:
        形如 ``45s`` / ``3m20s`` / ``1h5m`` 的文本。
    """

    total = int(max(0.0, seconds))
    if total < 60:
        return f"{total}s"
    minutes, sec = divmod(total, 60)
    if minutes < 60:
        return f"{minutes}m{sec}s" if sec else f"{minutes}m"
    hours, minute = divmod(minutes, 60)
    return f"{hours}h{minute}m" if minute else f"{hours}h"


class QueueCard(QFrame):
    """单个队列的卡片：标题、当前叫号、等待列表与操作按钮。"""

    def __init__(self, window: QueueWindow, queue_name: str) -> None:
        """构造卡片。

        Args:
            window: 所属主窗口（提供 hub 与配色）。
            queue_name: 队列键名。
        """

        super().__init__()
        self._window = window
        self._name = queue_name
        self._paused = False
        self._rows: list[QWidget] = []

        self.setObjectName("queueCard")
        outer = QVBoxLayout(self)
        outer.setContentsMargins(14, 12, 14, 12)
        outer.setSpacing(8)

        header = QHBoxLayout()
        header.setSpacing(10)
        self._title = QLabel(queue_name)
        self._title.setObjectName("queueTitle")
        self._badges = QLabel("")
        self._badges.setObjectName("queueBadges")
        self._meta = QLabel("")
        self._meta.setObjectName("queueMeta")
        self._meta.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        header.addWidget(self._title)
        header.addWidget(self._badges)
        header.addStretch(1)
        header.addWidget(self._meta)
        outer.addLayout(header)

        self._current = QFrame()
        self._current.setObjectName("currentRow")
        current_layout = QHBoxLayout(self._current)
        current_layout.setContentsMargins(10, 6, 10, 6)
        current_layout.setSpacing(10)
        current_label = QLabel("当前")
        current_label.setObjectName("currentLabel")
        self._current_name = QLabel("-")
        self._current_name.setObjectName("currentName")
        self._current_ticket = QLabel("")
        self._current_ticket.setObjectName("currentTicket")
        current_layout.addWidget(current_label)
        current_layout.addWidget(self._current_name, 1)
        current_layout.addWidget(self._current_ticket)
        outer.addWidget(self._current)

        self._entries = QVBoxLayout()
        self._entries.setSpacing(2)
        outer.addLayout(self._entries)

        tools = QHBoxLayout()
        tools.setSpacing(8)
        button_next = self._button("叫号 ▶", lambda: self._act("/api/call_next"))
        self._button_pause = self._button("暂停", self._toggle_pause)
        button_clear = self._button("清空", self._confirm_clear)
        button_delete = self._button("删除队列", self._confirm_delete, danger=True)
        tools.addWidget(button_next)
        tools.addWidget(self._button_pause)
        tools.addStretch(1)
        tools.addWidget(button_clear)
        tools.addWidget(button_delete)
        outer.addLayout(tools)

    # ── 构造辅助 ──────────────────────────────────────

    def _button(self, text: str, slot: Any, *, danger: bool = False) -> QPushButton:
        """创建卡片上的按钮。

        Args:
            text: 按钮文字。
            slot: 点击回调。
            danger: 是否使用危险配色。

        Returns:
            按钮控件。
        """

        button = QPushButton(text)
        button.setObjectName("dangerButton" if danger else "toolButton")
        button.setCursor(Qt.CursorShape.PointingHandCursor)
        button.setVisible(self._window.show_buttons)
        button.clicked.connect(slot)
        return button

    # ── 操作 ──────────────────────────────────────────

    def _act(self, path: str, extra: dict[str, Any] | None = None) -> None:
        """执行一次队内操作并把结果提示出来。

        Args:
            path: 接口风格的操作名（与 HTTP 接口同名，便于对照）。
            extra: 额外参数。
        """

        self._window.run_action(path, {"queue": self._name, **(extra or {})})

    def _toggle_pause(self) -> None:
        """切换暂停 / 恢复。"""

        self._act("/api/pause", {"paused": not self._paused})

    def _confirm_clear(self) -> None:
        """确认后清空队列。"""

        if self._window.confirm(f"确定清空「{self._title.text()}」的全部排队吗？"):
            self._act("/api/clear")

    def _confirm_delete(self) -> None:
        """确认后删除队列。"""

        if self._window.confirm(f"确定删除队列「{self._title.text()}」吗？此操作不可撤销。"):
            self._act("/api/delete")

    # ── 刷新 ──────────────────────────────────────────

    def update_queue(self, data: dict[str, Any], view: dict[str, Any]) -> None:
        """用最新快照刷新卡片。

        Args:
            data: 单个队列的快照。
            view: 外观配置。
        """

        self._paused = bool(data.get("paused"))
        waiting = int(data.get("waiting") or 0)
        capacity = int(data.get("capacity") or 0)

        self._title.setText(str(data.get("title") or self._name))

        badges = [f"{waiting} 人"]
        if self._paused:
            badges.append("已暂停")
        if capacity > 0 and waiting >= capacity:
            badges.append("已满")
        self._badges.setText("　".join(badges))
        self._meta.setText(
            f"序号 {data.get('next_ticket', 0)} · 已叫号 {data.get('served_count', 0)}"
        )

        current = data.get("current")
        if current and bool(view.get("show_current", True)):
            self._current.setVisible(True)
            self._current_name.setText(str(current.get("nickname") or "-"))
            self._current_ticket.setText(f"#{current.get('ticket', '')}")
        else:
            self._current.setVisible(False)

        self._button_pause.setText("恢复" if self._paused else "暂停")
        self._render_entries(data, view, waiting)

    def _render_entries(self, data: dict[str, Any], view: dict[str, Any], waiting: int) -> None:
        """重画等待列表。

        Args:
            data: 单个队列的快照。
            view: 外观配置。
            waiting: 等待人数。
        """

        for widget in self._rows:
            self._entries.removeWidget(widget)
            widget.deleteLater()
        self._rows.clear()

        entries = list(data.get("entries") or [])
        max_rows = int(view.get("max_rows") or 8)
        show_wait = bool(view.get("show_wait_time", True))

        for index, entry in enumerate(entries[:max_rows], start=1):
            row = QWidget()
            layout = QHBoxLayout(row)
            layout.setContentsMargins(0, 0, 0, 0)
            layout.setSpacing(10)

            rank = QLabel(str(index))
            rank.setObjectName("entryRank")
            rank.setMinimumWidth(26)
            name = QLabel(str(entry.get("nickname") or "-"))
            name.setObjectName("entryName")
            layout.addWidget(rank)
            layout.addWidget(name, 1)

            if show_wait:
                waited = QLabel(human_wait(float(entry.get("waiting_seconds") or 0)))
                waited.setObjectName("entryWait")
                layout.addWidget(waited)

            if self._window.show_buttons:
                user_id = str(entry.get("user_id") or "")
                top = QPushButton("置顶")
                top.setObjectName("tinyButton")
                top.setCursor(Qt.CursorShape.PointingHandCursor)
                top.clicked.connect(
                    lambda _checked=False, uid=user_id: self._act(
                        "/api/move_front", {"user_id": uid}
                    )
                )
                remove = QPushButton("移除")
                remove.setObjectName("tinyButton")
                remove.setCursor(Qt.CursorShape.PointingHandCursor)
                remove.clicked.connect(
                    lambda _checked=False, uid=user_id: self._act("/api/remove", {"user_id": uid})
                )
                layout.addWidget(top)
                layout.addWidget(remove)

            self._entries.addWidget(row)
            self._rows.append(row)

        if waiting > len(entries[:max_rows]):
            more = QLabel(f"…… 还有 {waiting - len(entries[:max_rows])} 人")
            more.setObjectName("entryMore")
            self._entries.addWidget(more)
            self._rows.append(more)

        if not entries and not data.get("current"):
            empty = QLabel("暂时没人排队")
            empty.setObjectName("entryMore")
            self._entries.addWidget(empty)
            self._rows.append(empty)


class QueueWindow(QWidget):
    """排队姬主窗口。"""

    state_changed = Signal()
    """由 hub 的监听回调发射（可能来自任意线程），Qt 会自动排队到 UI 线程。"""

    def __init__(
        self,
        *,
        hub: QueueHub,
        server_url: str = "",
        confirm_on_close: bool = True,
        logger: Any = None,
    ) -> None:
        """构造主窗口。

        Args:
            hub: 队列服务层。
            server_url: HTTP 服务地址，显示在状态栏里方便复制到 OBS。
            confirm_on_close: 关窗前是否确认（关窗会停掉排队服务）。
            logger: 可选日志器。
        """

        super().__init__()
        self._hub = hub
        self._server_url = server_url.rstrip("/")
        self._confirm_on_close = confirm_on_close
        self._logger = logger
        self._cards: dict[str, QueueCard] = {}
        self._names: list[str] = []
        self._view: dict[str, Any] = {}
        self.show_buttons = bool(hub.config.window.show_buttons)

        window_config = hub.config.window
        self.setWindowTitle(window_config.title)
        if window_config.always_on_top:
            self.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint, True)
        self.resize(window_config.width, window_config.height)
        self.setMinimumSize(360, 320)

        root = QVBoxLayout(self)
        root.setContentsMargins(12, 12, 12, 10)
        root.setSpacing(8)

        self._status = QLabel("正在启动…")
        self._status.setObjectName("statusBar")
        self._status.setWordWrap(True)
        root.addWidget(self._status)

        self._links = QLabel("")
        self._links.setObjectName("linkRow")
        self._links.setOpenExternalLinks(True)
        self._links.setWordWrap(True)
        self._links.setTextInteractionFlags(Qt.TextInteractionFlag.TextBrowserInteraction)
        self._links.setCursor(Qt.CursorShape.PointingHandCursor)
        self._links.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self._links.customContextMenuRequested.connect(self._show_link_menu)
        root.addWidget(self._links)

        host = QWidget()
        self._cards_layout = QVBoxLayout(host)
        self._cards_layout.setContentsMargins(0, 0, 0, 0)
        self._cards_layout.setSpacing(10)
        self._cards_layout.addStretch(1)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setWidget(host)
        root.addWidget(scroll, 1)

        self._toast = QLabel("")
        self._toast.setObjectName("toast")
        self._toast.setWordWrap(True)
        root.addWidget(self._toast)

        if window_config.opacity < 1.0:
            self.setWindowOpacity(max(0.3, min(1.0, window_config.opacity)))

        self.state_changed.connect(self.refresh)
        hub.add_listener(self._on_hub_change)

        self._timer = QTimer(self)
        self._timer.timeout.connect(self.refresh)
        self._timer.start(_REFRESH_INTERVAL_MS)

    # ── 与 hub 的联动 ─────────────────────────────────

    def _on_hub_change(self) -> None:
        """hub 的变更回调（可能来自 HTTP 线程）。

        这里只发信号，绝不碰控件——Qt 会把信号排队投递到 UI 线程执行 :meth:`refresh`。
        """

        self.state_changed.emit()

    def detach(self) -> None:
        """从 hub 注销监听并停掉定时器（退出前调用，避免回调打到已销毁的窗口）。"""

        self._timer.stop()
        try:
            self._hub.remove_listener(self._on_hub_change)
        except Exception as exc:  # 退出流程不该因为注销失败而卡住
            self._log(f"注销窗口监听失败：{exc}")

    # ── 操作 ──────────────────────────────────────────

    def run_action(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        """执行一次队列操作，并把结果提示在底部。

        窗口和 hub 同进程，所以直接调用，不走 HTTP。

        Args:
            path: 与 HTTP 接口同名的操作路径。
            payload: 参数。

        Returns:
            hub 返回的结果 dict。
        """

        result = self._dispatch(path, payload)
        ok = bool(result.get("ok"))
        message = str(result.get("message") or ("操作成功" if ok else "操作失败"))
        self._toast.setText(("✅ " if ok else "⛔ ") + message)
        if not ok:
            self._log(f"{path} 失败：{message}")
        return result

    def _dispatch(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        """把操作名映射到 hub 方法。

        Args:
            path: 操作路径。
            payload: 参数。

        Returns:
            结果 dict。
        """

        hub = self._hub
        queue = str(payload.get("queue") or "")
        user_id = str(payload.get("user_id") or "")

        if path == "/api/call_next":
            return hub.call_next(queue)
        if path == "/api/finish":
            return hub.finish_current(queue)
        if path == "/api/requeue":
            return hub.requeue_current(queue)
        if path == "/api/clear":
            return hub.clear(queue)
        if path == "/api/pause":
            return hub.set_paused(queue, paused=bool(payload.get("paused", True)))
        if path == "/api/capacity":
            return hub.set_capacity(queue, int(payload.get("capacity") or 0))
        if path == "/api/create":
            capacity = int(payload.get("capacity") or 0)
            return hub.create_queue(
                queue,
                display_name=str(payload.get("display_name") or ""),
                capacity=capacity if capacity > 0 else None,
            )
        if path == "/api/delete":
            return hub.delete_queue(queue)
        if path == "/api/remove":
            return hub.remove_user(user_id, queue)
        if path == "/api/move_front":
            return hub.move_front(user_id, queue)
        if path == "/api/add":
            return hub.add_manual(str(payload.get("nickname") or ""), queue)
        return {"ok": False, "message": f"未知操作：{path}"}

    def confirm(self, text: str) -> bool:
        """弹一个确认框。

        Args:
            text: 确认文案。

        Returns:
            用户点了「确定」返回 ``True``。
        """

        answer = QMessageBox.question(
            self,
            self.windowTitle(),
            text,
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        return answer == QMessageBox.StandardButton.Yes

    # ── 刷新 ──────────────────────────────────────────

    def refresh(self) -> None:
        """按当前 hub 状态重画界面。"""

        snapshot = self._hub.snapshot()
        view = snapshot.get("view")
        if isinstance(view, dict):
            self._view = view
            self._apply_style()

        queues = list(snapshot.get("queues") or [])
        names = [str(item.get("name") or "") for item in queues]
        if names != self._names:
            self._rebuild_cards(names)

        for item in queues:
            card = self._cards.get(str(item.get("name") or ""))
            if card is not None:
                card.update_queue(item, self._view)

        total = snapshot.get("total_waiting", 0)
        self._status.setText(f"✅ 运行中 · {len(names)} 个队列 / {total} 人排队")
        self._update_links()

    # ── 页面链接 ──────────────────────────────────────

    def _page_urls(self) -> dict[str, str]:
        """窗口里展示的可点击页面地址（控制面板 / OBS 叠加页）。"""

        if not self._server_url:
            return {}
        return {
            "控制面板": f"{self._server_url}/panel",
            "OBS 叠加页": f"{self._server_url}/overlay",
        }

    def _update_links(self) -> None:
        """刷新两个超链接：点一下就用默认浏览器打开。"""

        urls = self._page_urls()
        if not urls:
            self._links.setText("")
            self._links.setToolTip("")
            return
        accent = str((self._view or {}).get("accent_color") or "#6fd3ff")
        parts = [
            f'<a href="{url}" style="color:{accent}; text-decoration:none;" title="{url}">{label}</a>'
            for label, url in urls.items()
        ]
        self._links.setText("　·　".join(parts))
        self._links.setToolTip("\n".join(f"{label}：{url}" for label, url in urls.items()))

    def _show_link_menu(self, position: Any) -> None:
        """右键菜单：用浏览器打开，或复制地址（OBS 浏览器源要复制叠加页地址）。"""

        urls = self._page_urls()
        if not urls:
            return
        menu = QMenu(self)
        for label, url in urls.items():
            open_action = menu.addAction(f"用浏览器打开 {label}")
            open_action.triggered.connect(
                lambda _checked=False, target=url: self._open_page(target)
            )
            copy_action = menu.addAction(f"复制{label}地址")
            copy_action.triggered.connect(
                lambda _checked=False, target=url: self._copy_page(target)
            )
        menu.exec(self._links.mapToGlobal(position))

    def _open_page(self, url: str) -> None:
        """用系统默认浏览器打开地址。"""

        QDesktopServices.openUrl(QUrl(url))

    def _copy_page(self, url: str) -> None:
        """把地址复制到剪贴板。"""

        QApplication.clipboard().setText(url)
        self._toast.setText(f"📋 已复制：{url}")

    def _rebuild_cards(self, names: list[str]) -> None:
        """按新的队列名列表重建卡片。

        Args:
            names: 队列名列表（显示顺序）。
        """

        for card in self._cards.values():
            self._cards_layout.removeWidget(card)
            card.deleteLater()
        self._cards.clear()

        for name in names:
            card = QueueCard(self, name)
            self._cards[name] = card
            self._cards_layout.insertWidget(self._cards_layout.count() - 1, card)

        self._names = list(names)

    def _apply_style(self) -> None:
        """根据当前外观配置重建样式表。"""

        view = self._view or {}
        accent = str(view.get("accent_color") or "#6fd3ff")
        text = str(view.get("text_color") or "#f2f6ff")
        panel = str(view.get("panel_bg") or "rgba(14, 16, 26, 0.92)")
        border = str(view.get("panel_border") or "rgba(120, 200, 255, 0.35)")
        current_bg = str(view.get("current_bg") or "rgba(111, 211, 255, 0.16)")
        title_size = int(view.get("title_size") or 22)
        name_size = int(view.get("name_size") or 20)

        self.setStyleSheet(
            f"""
            QWidget {{
                background: #10131c;
                color: {text};
                font-family: "Microsoft YaHei", "PingFang SC", sans-serif;
                font-size: 14px;
            }}
            #statusBar {{ color: {accent}; font-size: 12px; padding: 2px 2px 6px 2px; }}
            #linkRow {{ color: {text}; font-size: 13px; padding: 0 2px 4px 2px; }}
            #queueCard {{
                background: {panel};
                border: 1px solid {border};
                border-radius: 12px;
            }}
            #queueTitle {{ color: {accent}; font-size: {max(14, title_size - 3)}px; font-weight: 700; }}
            #queueBadges, #queueMeta {{ color: {text}; font-size: 12px; }}
            #queueMeta {{ opacity: 0.6; }}
            #currentRow {{
                background: {current_bg};
                border-left: 4px solid {accent};
                border-radius: 8px;
            }}
            #currentLabel {{ color: {accent}; font-size: 12px; }}
            #currentName {{ font-size: {max(14, name_size - 2)}px; font-weight: 700; }}
            #currentTicket, #entryWait, #entryMore {{ color: {text}; font-size: 12px; opacity: 0.65; }}
            #entryRank {{ color: {accent}; font-weight: 700; }}
            #entryName {{ font-size: {max(13, name_size - 4)}px; }}
            #toast {{ color: {text}; font-size: 12px; opacity: 0.9; }}
            QPushButton {{
                background: #1e2230;
                color: {text};
                border: 1px solid {border};
                border-radius: 8px;
                padding: 5px 12px;
                font-size: 13px;
            }}
            QPushButton:hover:enabled {{ border-color: {accent}; color: {accent}; }}
            QPushButton:disabled {{ color: #6b7280; border-color: #2b3040; }}
            #tinyButton {{ padding: 2px 8px; font-size: 12px; }}
            #dangerButton:hover:enabled {{ border-color: #ff6b6b; color: #ff6b6b; }}
            QScrollArea, QScrollBar:vertical {{ background: transparent; border: none; }}
            QScrollBar:vertical {{ width: 8px; }}
            QScrollBar::handle:vertical {{ background: {border}; border-radius: 4px; }}
            """
        )

    # ── 关闭 ──────────────────────────────────────────

    def closeEvent(self, event: Any) -> None:
        """关窗时确认（关窗等于退出整个应用）。

        Args:
            event: Qt 关闭事件。
        """

        if not self._confirm_on_close:
            event.accept()
            return

        if self.confirm("关闭排队姬会同时停止排队服务，机器人将无法再排队。确定关闭吗？"):
            event.accept()
        else:
            event.ignore()

    def _log(self, message: str) -> None:
        """记日志。

        Args:
            message: 日志正文。
        """

        if self._logger is not None and callable(getattr(self._logger, "info", None)):
            self._logger.info(message)


def create_application() -> QApplication:
    """创建（或复用）QApplication 并设置字体。

    Returns:
        QApplication 实例。
    """

    import sys

    app = QApplication.instance()
    if app is None:
        app = QApplication(sys.argv[:1])
    app.setApplicationName("live_queue")
    app.setFont(QFont("Microsoft YaHei", 10))
    return app


__all__ = ["QueueCard", "QueueWindow", "create_application", "human_wait"]
