"""bilibili_queue 对外服务组件（QueueService）的单测。

服务是给其他插件用的稳定接口：不持有状态、全部转发给排队姬、返回值就是 app 的
结果 dict（调用方只看 ``ok``）。
"""

from __future__ import annotations

from typing import Any

import pytest

from plugins.bilibili_queue.components.queue_service import QueueService
from plugins.bilibili_queue.core.runtime import QueueRuntime, clear_queue_runtime


class _DummyPlugin:
    """服务构造只需要一个持有它的对象。"""

    plugin_name = "bilibili_queue"
    config = None


@pytest.fixture
def service(runtime: QueueRuntime) -> QueueService:
    """待测服务实例。"""

    return QueueService(plugin=_DummyPlugin())  # type: ignore[arg-type]


def test_service_identity() -> None:
    """组件名与类型供其他插件引用，不能随意改动。"""

    assert QueueService.name == "queue"
    assert QueueService.component_type == "service"
    assert QueueService.description


def test_is_configured_reflects_plugin_load(runtime: QueueRuntime, service: QueueService) -> None:
    """插件加载后 is_configured 为真，注销后为假。"""

    assert service.is_configured() is True
    clear_queue_runtime()
    assert service.is_configured() is False


async def test_is_ready_follows_app_state(service: QueueService, offline_runtime: Any) -> None:
    """就绪状态跟随排队姬本身（offline_runtime 会覆盖注册）。"""

    assert await service.is_ready() is False


async def test_is_ready_when_online(service: QueueService) -> None:
    """排队姬可用时就绪。"""

    assert await service.is_ready() is True


async def test_health_and_snapshot(service: QueueService, hub: Any) -> None:
    """健康检查与状态快照。"""

    hub.join(user_id="u1", nickname="阿狸", queue="排位")

    health = await service.health()
    assert health["ok"] is True and health["app"] == "live_queue"

    snapshot = await service.snapshot()
    assert snapshot["queues"][0]["title"] == "排位"
    assert snapshot["total_waiting"] == 1

    assert await service.queue_names() == ["排位"]


async def test_join_and_leave(service: QueueService, hub: Any) -> None:
    """入队与退队返回 app 的结果 dict。"""

    joined = await service.join(
        user_id="u1", nickname="阿狸", queue="排位", platform="bilibili_live"
    )
    assert joined["ok"] is True
    assert joined["status"] == "joined"
    assert joined["position"] == 1
    assert joined["ticket"] == 1

    assert hub.manager.get_queue("排位").size == 1  # type: ignore[union-attr]

    left = await service.leave(user_id="u1")
    assert left["ok"] is True
    assert left["queues"] == ["排位"]


async def test_join_passes_queue_explicit(service: QueueService, hub: Any) -> None:
    """``queue_explicit=False`` 时未命中会落到默认队（解析在 app 侧）。"""

    result = await service.join(
        user_id="u1", nickname="阿狸", queue="一下", queue_explicit=False
    )
    assert result["queue"] == "默认"
    assert hub.manager.get_queue("一下") is None


async def test_call_next_and_finish(service: QueueService, hub: Any) -> None:
    """叫号与结束。"""

    await service.join(user_id="u1", nickname="阿狸", queue="排位")

    called = await service.call_next("排位")
    assert called["ok"] is True
    assert called["entry"]["nickname"] == "阿狸"

    finished = await service.finish_current("排位")
    assert finished["ok"] is True


async def test_requeue_and_remove(service: QueueService, hub: Any) -> None:
    """放回与移除。"""

    await service.join(user_id="u1", nickname="阿狸", queue="排位")
    await service.join(user_id="u2", nickname="小满", queue="排位")
    await service.call_next("排位")

    assert (await service.requeue_current("排位"))["ok"] is True
    assert (await service.remove_user(user_id="u2", queue="排位"))["ok"] is True
    assert hub.manager.get_queue("排位").position_of("u2") == 0  # type: ignore[union-attr]


async def test_clear_pause_and_capacity(service: QueueService, hub: Any) -> None:
    """清空、暂停 / 恢复、上限。"""

    await service.join(user_id="u1", queue="排位")
    await service.join(user_id="u2", queue="排位")

    assert (await service.set_paused("排位", paused=True))["ok"] is True
    assert hub.manager.get_queue("排位").paused is True  # type: ignore[union-attr]

    assert (await service.set_paused("排位", paused=False))["ok"] is True

    cleared = await service.clear("排位")
    assert cleared["ok"] is True
    assert cleared["removed"] == 2


async def test_create_delete_and_reset(service: QueueService, hub: Any) -> None:
    """建队、删队、重置。"""

    created = await service.create_queue("排位", display_name="排位赛", capacity=4)
    assert created["ok"] is True
    assert hub.manager.get_queue("排位").title == "排位赛"  # type: ignore[union-attr]

    await service.join(user_id="u1", queue="排位")

    assert (await service.reset())["ok"] is True
    assert hub.manager.get_queue("排位").size == 0  # type: ignore[union-attr]

    assert (await service.delete_queue("排位"))["ok"] is True
    assert hub.manager.get_queue("排位") is None


async def test_operations_when_app_offline(offline_runtime: Any) -> None:
    """排队姬不可用时返回合成的失败结果，而不是抛异常。"""

    service = QueueService(plugin=_DummyPlugin())  # type: ignore[arg-type]

    result = await service.join(user_id="u1", queue="排位")
    assert result["ok"] is False
    assert result["status"] == "app_unavailable"
    assert "不可用" in result["message"]

    assert await service.is_ready() is False
    assert await service.queue_names() == []


async def test_operations_without_runtime_raise(service: QueueService) -> None:
    """插件未加载时抛 RuntimeError（这是配置错误，不该静默）。"""

    clear_queue_runtime()
    with pytest.raises(RuntimeError):
        await service.join(user_id="u1")


def test_runtime_accessor(service: QueueService, runtime: QueueRuntime) -> None:
    """高级接口能拿到运行时本体。"""

    assert service.runtime_accessor()() is runtime
