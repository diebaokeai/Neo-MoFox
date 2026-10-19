"""bilibili_queue 插件测试的公共夹具。

插件本身**不持有队列状态**（状态在独立应用 ``live_queue`` 里），所以测试要造一个
「假排队姬」：

- :class:`FakeAppClient`：把插件对 app 的 HTTP 调用**直接转给一个真实的 live_queue
  hub**。这样测的是真实的队列语义（重复入队、叫号、暂停……），又不用起 socket。
  注意这是**测试**里的借用；插件生产代码只通过 HTTP 访问 live_queue，没有任何
  import 关系，架构边界没有被破坏。
- :class:`FakeSupervisor`：替掉真实的进程管理，避免测试真的去拉起一个窗口。
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from live_queue.config import AppConfig
from live_queue.hub import QueueHub
from live_queue.store import QueueStore
from plugins.bilibili_queue.core.runtime import (
    QueueRuntime,
    clear_queue_runtime,
    init_queue_runtime,
)
from plugins.bilibili_queue.core.settings import PluginSettings


class FakeAppClient:
    """把调用转发给真实 live_queue hub 的假客户端。"""

    def __init__(self, hub: QueueHub) -> None:
        """记录后端 hub。

        Args:
            hub: 真实的 live_queue 服务层。
        """

        self.hub = hub
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def _record(self, method: str, kwargs: dict[str, Any]) -> None:
        """记录一次调用，便于断言「插件到底发了什么请求」。

        Args:
            method: 方法名。
            kwargs: 参数。
        """

        self.calls.append((method, kwargs))

    async def health(self) -> dict[str, Any]:
        """健康检查。"""

        self._record("health", {})
        return self.hub.health()

    async def state(self, *, queue: str = "", limit: int = 0) -> dict[str, Any]:
        """状态快照。"""

        self._record("state", {"queue": queue, "limit": limit})
        return self.hub.snapshot(queue=queue, limit=limit)

    async def queues(self) -> dict[str, Any]:
        """队列名列表（与真实客户端一致：返回原始 dict）。"""

        self._record("queues", {})
        return {"ok": True, "queues": self.hub.queue_names()}

    async def queue_names(self) -> list[str]:
        """队列名列表（便捷形式）。"""

        self._record("queue_names", {})
        return self.hub.queue_names()

    async def whoami(self) -> dict[str, Any]:
        """写权限探测。"""

        self._record("whoami", {})
        return {"ok": True, "can_write": True, "token_required": False}

    async def join(self, **kwargs: Any) -> dict[str, Any]:
        """入队。"""

        self._record("join", kwargs)
        return self.hub.join(**kwargs)

    async def leave(self, **kwargs: Any) -> dict[str, Any]:
        """退队。"""

        self._record("leave", kwargs)
        return self.hub.leave(**kwargs)

    async def call_next(self, queue: str = "") -> dict[str, Any]:
        """叫号。"""

        self._record("call_next", {"queue": queue})
        return self.hub.call_next(queue)

    async def finish(self, queue: str = "") -> dict[str, Any]:
        """结束当前。"""

        self._record("finish", {"queue": queue})
        return self.hub.finish_current(queue)

    async def requeue(self, queue: str = "") -> dict[str, Any]:
        """放回队首。"""

        self._record("requeue", {"queue": queue})
        return self.hub.requeue_current(queue)

    async def clear(self, queue: str = "") -> dict[str, Any]:
        """清空队列。"""

        self._record("clear", {"queue": queue})
        return self.hub.clear(queue)

    async def set_paused(self, queue: str = "", *, paused: bool) -> dict[str, Any]:
        """暂停 / 恢复。"""

        self._record("set_paused", {"queue": queue, "paused": paused})
        return self.hub.set_paused(queue, paused=paused)

    async def set_capacity(self, queue: str = "", capacity: int = 0) -> dict[str, Any]:
        """改上限。"""

        self._record("set_capacity", {"queue": queue, "capacity": capacity})
        return self.hub.set_capacity(queue, capacity)

    async def create_queue(
        self, queue: str, *, display_name: str = "", capacity: int = 0
    ) -> dict[str, Any]:
        """建队。"""

        self._record("create_queue", {"queue": queue, "capacity": capacity})
        return self.hub.create_queue(
            queue, display_name=display_name, capacity=capacity if capacity > 0 else None
        )

    async def delete_queue(self, queue: str) -> dict[str, Any]:
        """删队。"""

        self._record("delete_queue", {"queue": queue})
        return self.hub.delete_queue(queue)

    async def reset(self, *, delete_queues: bool = False) -> dict[str, Any]:
        """重置全部队列。"""

        self._record("reset", {"delete_queues": delete_queues})
        return self.hub.reset(delete_queues=delete_queues)

    async def remove_user(self, user_id: str, queue: str = "") -> dict[str, Any]:
        """移除用户。"""

        self._record("remove_user", {"user_id": user_id, "queue": queue})
        return self.hub.remove_user(user_id, queue)

    async def move_front(self, user_id: str, queue: str = "") -> dict[str, Any]:
        """置顶。"""

        self._record("move_front", {"user_id": user_id, "queue": queue})
        return self.hub.move_front(user_id, queue)

    async def add_manual(self, nickname: str, queue: str = "") -> dict[str, Any]:
        """手动加人。"""

        self._record("add_manual", {"nickname": nickname, "queue": queue})
        return self.hub.add_manual(nickname, queue)


class FakeSupervisor:
    """假的进程管理：只回答「就绪 / 不就绪」，不真的起进程。"""

    def __init__(self, *, ready: bool = True, detail: str = "fake app running") -> None:
        """记录要返回的就绪状态。

        Args:
            ready: ``ensure_running`` 是否返回就绪。
            detail: 说明文案。
        """

        self.ready = ready
        self.detail = detail
        self.calls = 0

    async def ensure_running(self) -> tuple[bool, str]:
        """返回预设的就绪状态。"""

        self.calls += 1
        return self.ready, self.detail

    def stop(self, **_kwargs: Any) -> bool:
        """空实现。"""

        return False


@pytest.fixture
def legacy_app_config() -> AppConfig:
    """旧的宽松队列语义（允许自动建队、没写队名可回落默认队）。

    产品默认已经是「必须写明一个已存在的队名、不自动建队」；严格语义由
    live_queue 的 test_strict_queue.py 覆盖。插件用例这里用宽松语义，
    方便聚焦 HTTP / 权限 / 关键词解析等主题。
    """

    config = AppConfig()
    config.queue.auto_create_queue = True
    config.queue.require_explicit_queue = False
    return config


@pytest.fixture
def hub(tmp_path: Path, legacy_app_config: AppConfig) -> QueueHub:
    """真实但不出网的 live_queue 服务层。"""

    return QueueHub(
        config=legacy_app_config, store=QueueStore(tmp_path / "queues.json")
    )


@pytest.fixture
def app_client(hub: QueueHub) -> FakeAppClient:
    """转发到真实 hub 的假客户端。"""

    return FakeAppClient(hub)


@pytest.fixture
def settings() -> PluginSettings:
    """默认运行参数。"""

    return PluginSettings()


@pytest.fixture
def runtime(
    settings: PluginSettings, app_client: FakeAppClient
) -> Iterator[QueueRuntime]:
    """注册好的运行时单例（默认「排队姬可用」）。"""

    instance = QueueRuntime(
        settings=settings, client=app_client, supervisor=FakeSupervisor()
    )
    clear_queue_runtime()
    init_queue_runtime(instance)
    yield instance
    clear_queue_runtime()


@pytest.fixture
def offline_runtime(
    settings: PluginSettings, app_client: FakeAppClient
) -> Iterator[QueueRuntime]:
    """排队姬不可用状态下的运行时。"""

    instance = QueueRuntime(
        settings=settings,
        client=app_client,
        supervisor=FakeSupervisor(ready=False, detail="fake app offline"),
    )
    clear_queue_runtime()
    init_queue_runtime(instance)
    yield instance
    clear_queue_runtime()
