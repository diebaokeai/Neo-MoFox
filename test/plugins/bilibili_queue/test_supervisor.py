"""独立应用（live_queue）进程管理的单测。

``ensure_running`` 是「bot 开着但排队姬没开」这条关键路径的实现，所以这里既测探测
逻辑（用真实 HTTP 服务），也测**真的把 live_queue 拉起来**这条路径。
"""

from __future__ import annotations

import socket
import subprocess
from collections.abc import Iterator
from pathlib import Path

import pytest

from live_queue.config import AppConfig
from live_queue.hub import QueueHub
from live_queue.server import QueueHTTPServer
from live_queue.store import QueueStore
from plugins.bilibili_queue.core.client import QueueAppClient
from plugins.bilibili_queue.core.settings import PluginSettings
from plugins.bilibili_queue.core.supervisor import (
    APP_SCRIPT,
    QueueAppSupervisor,
)

WEB_DIR = Path(__file__).resolve().parents[2] / "live_queue" / "web"


def _free_port() -> int:
    """找一个当前空闲的端口。

    Returns:
        端口号。
    """

    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _settings(**overrides: object) -> PluginSettings:
    """构造运行参数。"""

    settings = PluginSettings()
    for key, value in overrides.items():
        setattr(settings, key, value)
    return settings


@pytest.fixture
def app_server(tmp_path: Path, legacy_app_config: AppConfig) -> Iterator[QueueHTTPServer]:
    """真实跑起来的排队姬服务。"""

    hub = QueueHub(config=legacy_app_config, store=QueueStore(tmp_path / "queues.json"))
    server = QueueHTTPServer(hub=hub, web_dir=WEB_DIR, host="127.0.0.1", port=0)
    server.start()
    yield server
    server.stop()


# ── 命令行拼装 ────────────────────────────────────────


def test_build_command_defaults() -> None:
    """默认命令要指向 live_queue/app.py 并带上地址与 ui 模式。"""

    settings = _settings(app_host="127.0.0.1", app_port=17650)
    supervisor = QueueAppSupervisor(
        settings=settings, client=QueueAppClient(base_url=settings.app_base_url)
    )

    command = supervisor.build_command()
    assert command[1] == str(APP_SCRIPT)
    assert "--host" in command and "127.0.0.1" in command
    assert "--port" in command and "17650" in command
    assert command[command.index("--ui") + 1] == "auto"


def test_build_command_no_window_and_token_and_extra() -> None:
    """``no_window`` / token / extra_args 都要体现到命令行上。"""

    settings = _settings(
        app_port=18000,
        app_token="tok",
        app_no_window=True,
        app_extra_args=["--log-level", "DEBUG", "  "],
        app_executable="D:/python.exe",
    )
    supervisor = QueueAppSupervisor(
        settings=settings, client=QueueAppClient(base_url=settings.app_base_url)
    )

    command = supervisor.build_command()
    assert command[0] == "D:/python.exe"
    assert command[command.index("--ui") + 1] == "off"
    assert command[command.index("--token") + 1] == "tok"
    assert command[-2:] == ["--log-level", "DEBUG"]


def test_app_script_is_inside_repo() -> None:
    """脚本路径要真的存在（否则自动拉起必然失败）。"""

    assert APP_SCRIPT.is_file()
    assert APP_SCRIPT.parent.name == "live_queue"
    assert APP_SCRIPT.name == "app.py"


# ── 探测 ──────────────────────────────────────────────


async def test_probe_running(app_server: QueueHTTPServer) -> None:
    """排队姬在跑时探测结果是 running。"""

    settings = _settings(app_port=app_server.port)
    supervisor = QueueAppSupervisor(
        settings=settings, client=QueueAppClient(base_url=settings.app_base_url)
    )

    state, detail = await supervisor.probe()
    assert state == "running"
    assert "live_queue" in detail
    assert await supervisor.is_running() is True


async def test_probe_offline() -> None:
    """没人监听时是 offline。"""

    settings = _settings(app_port=_free_port())
    supervisor = QueueAppSupervisor(
        settings=settings,
        client=QueueAppClient(base_url=settings.app_base_url, timeout=0.5),
    )

    state, detail = await supervisor.probe()
    assert state == "offline"
    assert detail


async def test_probe_occupied(
    app_server: QueueHTTPServer, tmp_path: Path, legacy_app_config: AppConfig
) -> None:
    """端口上有响应但不是 live_queue 时是 occupied（不能盲目再拉一个）。"""

    hub = QueueHub(config=legacy_app_config, store=QueueStore(tmp_path / "q.json"))
    # 造假：让 health 返回别的 app 标识
    original = hub.health

    def fake_health() -> dict:
        payload = original()
        payload["app"] = "别的程序"
        return payload

    hub.health = fake_health  # type: ignore[method-assign]
    other = QueueHTTPServer(hub=hub, web_dir=WEB_DIR, host="127.0.0.1", port=0)
    other.start()
    try:
        settings = _settings(app_port=other.port)
        supervisor = QueueAppSupervisor(
            settings=settings, client=QueueAppClient(base_url=settings.app_base_url)
        )

        state, detail = await supervisor.probe()
        assert state == "occupied"
        assert "不是 live_queue" in detail
    finally:
        other.stop()


async def test_probe_unauthorized(app_server: QueueHTTPServer) -> None:
    """token 不匹配时是 unauthorized（提示改配置，而不是反复拉起）。"""

    app_server._hub.config.server.token = "s3cret"
    settings = _settings(app_port=app_server.port, app_token="wrong")
    supervisor = QueueAppSupervisor(
        settings=settings, client=QueueAppClient(base_url=settings.app_base_url, token="wrong")
    )

    state, detail = await supervisor.probe()
    assert state == "unauthorized"
    assert "密钥不匹配" in detail


# ── ensure_running ────────────────────────────────────


async def test_ensure_running_uses_existing_app(app_server: QueueHTTPServer) -> None:
    """已经在跑时直接返回就绪，不启动任何进程。"""

    settings = _settings(app_port=app_server.port)
    supervisor = QueueAppSupervisor(
        settings=settings, client=QueueAppClient(base_url=settings.app_base_url)
    )

    ok, detail = await supervisor.ensure_running()

    assert ok is True
    assert "已在运行" in detail
    assert supervisor.spawned is False


async def test_ensure_running_respects_auto_start_off() -> None:
    """关掉自动拉起时只提示，不起进程。"""

    settings = _settings(app_port=_free_port(), app_auto_start=False)
    supervisor = QueueAppSupervisor(
        settings=settings,
        client=QueueAppClient(base_url=settings.app_base_url, timeout=0.5),
    )

    ok, detail = await supervisor.ensure_running()

    assert ok is False
    assert "自动拉起" in detail
    assert supervisor.spawned is False


async def test_ensure_running_refuses_when_occupied(
    app_server: QueueHTTPServer, tmp_path: Path, legacy_app_config: AppConfig
) -> None:
    """端口被别的程序占着时不盲目拉起（否则会一直起不来还看不懂）。"""

    hub = QueueHub(config=legacy_app_config, store=QueueStore(tmp_path / "q.json"))
    original = hub.health
    hub.health = lambda: {**original(), "app": "别的程序"}  # type: ignore[method-assign]

    other = QueueHTTPServer(hub=hub, web_dir=WEB_DIR, host="127.0.0.1", port=0)
    other.start()
    try:
        settings = _settings(app_port=other.port, app_auto_start=True)
        supervisor = QueueAppSupervisor(
            settings=settings, client=QueueAppClient(base_url=settings.app_base_url)
        )

        ok, detail = await supervisor.ensure_running()
        assert ok is False
        assert supervisor.spawned is False
        assert "不是 live_queue" in detail
    finally:
        other.stop()


async def test_ensure_running_reports_missing_script(tmp_path: Path) -> None:
    """找不到 app 脚本时给出明确失败，而不是抛异常。"""

    settings = _settings(app_port=_free_port())
    supervisor = QueueAppSupervisor(
        settings=settings,
        client=QueueAppClient(base_url=settings.app_base_url, timeout=0.5),
        app_script=tmp_path / "nope.py",
    )

    ok, detail = await supervisor.ensure_running()
    assert ok is False
    assert "失败" in detail or "未就绪" in detail


async def test_spawn_failure_is_reported(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Popen 抛异常时返回 False 且不残留状态。"""

    def boom(*_args: object, **_kwargs: object) -> None:
        raise OSError("不允许创建进程")

    monkeypatch.setattr(subprocess, "Popen", boom)

    settings = _settings(app_port=_free_port(), app_start_timeout=1.0)
    supervisor = QueueAppSupervisor(
        settings=settings,
        client=QueueAppClient(base_url=settings.app_base_url, timeout=0.5),
        app_script=Path(__file__).resolve(),  # 存在的文件即可
    )

    ok, _detail = await supervisor.ensure_running()
    assert ok is False
    assert supervisor.spawned is False


async def test_stop_without_process_is_safe() -> None:
    """没拉起过时 stop 是空操作。"""

    settings = _settings()
    supervisor = QueueAppSupervisor(
        settings=settings, client=QueueAppClient(base_url=settings.app_base_url)
    )

    assert supervisor.stop() is False
    assert supervisor.is_alive is False


# ── 真·自动拉起 ───────────────────────────────────────


async def test_ensure_running_actually_spawns_live_queue(tmp_path: Path) -> None:
    """端到端：真的把 live_queue/app.py 拉起来，等它就绪，再关掉。

    这是「bot 开着、排队姬没开」这条路径的最终验证——只有真的起一次进程才能确认
    命令行参数、就绪探测、关停都是通的。
    """

    port = _free_port()
    settings = _settings(
        app_port=port,
        app_auto_start=True,
        app_no_window=True,
        app_start_timeout=30.0,
        app_extra_args=["--config", str(tmp_path / "app-config.json"), "--data", str(tmp_path / "app-data.json")],
    )
    supervisor = QueueAppSupervisor(
        settings=settings,
        client=QueueAppClient(base_url=settings.app_base_url, timeout=1.0),
    )

    try:
        ok, detail = await supervisor.ensure_running()
        assert ok is True, detail
        assert supervisor.spawned is True
        assert supervisor.is_alive is True

        # 起来之后要真的能用：新规则不会自动建队，先手动建一个再入队
        client = QueueAppClient(base_url=settings.app_base_url, timeout=2.0)
        assert (await client.health())["app"] == "live_queue"
        created = await client.create_queue("排位")
        assert created.get("ok") is True, created
        assert (await client.join(user_id="u1", nickname="阿狸", queue="排位"))["ok"] is True
        assert (await client.call_next("排位"))["entry"]["nickname"] == "阿狸"

        # 再调一次不应该重复起进程
        assert (await supervisor.ensure_running())[0] is True
    finally:
        supervisor.stop()

    assert supervisor.is_alive is False
