"""插件与独立应用之间的 HTTP 契约测试。

这里会**真的起一个 live_queue HTTP 服务**（绑定系统分配的空闲端口），然后用插件的
``QueueAppClient`` 去打它。这是唯一能验证「插件 ↔ 排队姬」协议没有写歪的方式——
两边各自单测都绿、接口字段却对不上的情况太常见了。
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from live_queue.config import AppConfig
from live_queue.hub import QueueHub
from live_queue.server import QueueHTTPServer
from live_queue.store import QueueStore
from plugins.bilibili_queue.core.client import QueueAppClient, QueueAppError

WEB_DIR = Path(__file__).resolve().parents[2] / "live_queue" / "web"


@pytest.fixture
def app_hub(tmp_path: Path, legacy_app_config: AppConfig) -> QueueHub:
    """独立应用的服务层。"""

    return QueueHub(config=legacy_app_config, store=QueueStore(tmp_path / "queues.json"))


@pytest.fixture
def app_server(app_hub: QueueHub) -> Iterator[QueueHTTPServer]:
    """真实跑起来的排队姬 HTTP 服务。"""

    server = QueueHTTPServer(hub=app_hub, web_dir=WEB_DIR, host="127.0.0.1", port=0)
    server.start()
    yield server
    server.stop()


@pytest.fixture
def client(app_server: QueueHTTPServer) -> QueueAppClient:
    """指向该服务的插件客户端。"""

    return QueueAppClient(base_url=f"http://127.0.0.1:{app_server.port}")


# ── 正常路径 ──────────────────────────────────────────


async def test_health_and_queues(client: QueueAppClient) -> None:
    """健康检查与队列名列表。"""

    health = await client.health()
    assert health["app"] == "live_queue"

    assert await client.queue_names() == []


async def test_join_leave_and_state(client: QueueAppClient, app_hub: QueueHub) -> None:
    """入队 → 状态 → 退队的完整往返。"""

    joined = await client.join(
        user_id="open-1",
        nickname="阿狸",
        platform="bilibili_live",
        queue="排位",
        queue_explicit=True,
        extra={"guard_level": 3},
        guild="12345",
    )
    assert joined["ok"] is True
    assert joined["status"] == "joined"
    assert joined["position"] == 1
    assert joined["ticket"] == 1

    state = await client.state()
    entry = state["queues"][0]["entries"][0]
    assert entry["nickname"] == "阿狸"
    assert entry["platform"] == "bilibili_live"
    assert state["view"]["max_rows"] == 8

    # extra / guild 也要真的传过去
    assert app_hub.manager.get_queue("排位").entries[0].extra["guard_level"] == 3  # type: ignore[union-attr]

    left = await client.leave(user_id="open-1")
    assert left["ok"] is True
    assert left["queues"] == ["排位"]


async def test_queue_explicit_flag_travels(client: QueueAppClient) -> None:
    """``queue_explicit`` 决定了「排队一下」是排默认队还是建「一下」队。"""

    implicit = await client.join(user_id="u1", queue="一下", queue_explicit=False)
    assert implicit["queue"] == "默认"

    explicit = await client.join(user_id="u1", queue="一下", queue_explicit=True)
    assert explicit["queue"] == "一下"


async def test_management_calls(client: QueueAppClient, app_hub: QueueHub) -> None:
    """管理类调用的往返。"""

    await client.join(user_id="u1", nickname="阿狸", queue="排位")
    await client.join(user_id="u2", nickname="小满", queue="排位")

    called = await client.call_next("排位")
    assert called["ok"] is True and called["entry"]["nickname"] == "阿狸"

    assert (await client.requeue("排位"))["ok"] is True
    assert (await client.set_paused("排位", paused=True))["ok"] is True
    assert (await client.set_paused("排位", paused=False))["ok"] is True
    assert (await client.set_capacity("排位", 9))["ok"] is True
    assert (await client.add_manual("手动", "排位"))["ok"] is True
    assert (await client.move_front("u2", "排位"))["ok"] is True
    assert (await client.remove_user("u2", "排位"))["ok"] is True
    assert (await client.call_next("排位"))["ok"] is True
    assert (await client.finish("排位"))["ok"] is True
    assert (await client.clear("排位"))["ok"] is True

    created = await client.create_queue("大厅", display_name="大厅队", capacity=4)
    assert created["ok"] is True
    assert (await client.delete_queue("大厅"))["ok"] is True

    assert (await client.reset())["ok"] is True
    assert (await client.flush())["ok"] is True

    assert app_hub.manager.get_queue("排位") is not None


async def test_queues_listing(client: QueueAppClient) -> None:
    """队列名列表。"""

    await client.create_queue("排位")
    await client.create_queue("娱乐")
    assert await client.queue_names() == ["排位", "娱乐"]
    assert (await client.queues())["queues"] == ["排位", "娱乐"]


# ── 鉴权 ──────────────────────────────────────────────


async def test_wrong_token_is_reported_clearly(app_hub: QueueHub) -> None:
    """密钥不匹配时要给出能直接照着改的提示。"""

    app_hub.config.server.token = "s3cret"
    server = QueueHTTPServer(hub=app_hub, web_dir=WEB_DIR, host="127.0.0.1", port=0)
    server.start()
    try:
        client = QueueAppClient(base_url=f"http://127.0.0.1:{server.port}", token="wrong")
        with pytest.raises(QueueAppError) as excinfo:
            await client.join(user_id="u1", queue="排位")

        assert excinfo.value.status == 403
        assert "管理密钥" in str(excinfo.value)
    finally:
        server.stop()


async def test_correct_token_works(app_hub: QueueHub) -> None:
    """密钥正确时写操作放行。"""

    app_hub.config.server.token = "s3cret"
    server = QueueHTTPServer(hub=app_hub, web_dir=WEB_DIR, host="127.0.0.1", port=0)
    server.start()
    try:
        client = QueueAppClient(base_url=f"http://127.0.0.1:{server.port}", token="s3cret")
        assert (await client.join(user_id="u1", queue="排位"))["ok"] is True
    finally:
        server.stop()


# ── 异常路径 ──────────────────────────────────────────


async def test_offline_app_raises_offline_error() -> None:
    """连不上时错误类别是 offline（上层据此判断要不要拉起 app）。"""

    client = QueueAppClient(base_url="http://127.0.0.1:1", timeout=0.5)
    with pytest.raises(QueueAppError) as excinfo:
        await client.health()

    assert excinfo.value.kind == "offline"
    assert "连不上" in str(excinfo.value) or "超时" in str(excinfo.value)


async def test_non_json_response_is_reported(client: QueueAppClient) -> None:
    """拿到 HTML（例如打错路径）时要报 bad_response，而不是抛 JSON 解析异常。"""

    with pytest.raises(QueueAppError) as excinfo:
        await client.get("/overlay")

    assert excinfo.value.kind == "bad_response"
    assert "不是 JSON" in str(excinfo.value)


async def test_404_is_reported(client: QueueAppClient) -> None:
    """未知路径返回 HTTP 错误。"""

    with pytest.raises(QueueAppError) as excinfo:
        await client.get("/api/nope")

    assert excinfo.value.kind == "http"
    assert excinfo.value.status == 404


async def test_base_url_is_normalised() -> None:
    """尾部斜杠不影响拼装。"""

    client = QueueAppClient(base_url="http://127.0.0.1:17650/")
    assert client.base_url == "http://127.0.0.1:17650"
