"""bilibili_queue 插件类与 manifest 的一致性测试。

插件规范里最容易踩的坑是「`manifest.include` 是手工维护的、写错了不会报错，
组件会被静默跳过」，所以这里把「代码里的组件」和「manifest 里声明的组件」做双向比对。
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from plugins.bilibili_queue import plugin as plugin_module
from plugins.bilibili_queue.components.danmaku_handler import DanmakuQueueHandler
from plugins.bilibili_queue.components.queue_admin_command import QueueAdminCommand
from plugins.bilibili_queue.components.queue_command import QueueCommand
from plugins.bilibili_queue.components.queue_service import QueueService
from plugins.bilibili_queue.config import BilibiliQueueConfig
from plugins.bilibili_queue.core.runtime import clear_queue_runtime, get_queue_runtime
from plugins.bilibili_queue.plugin import BilibiliQueuePlugin

PLUGIN_DIR = Path(plugin_module.__file__).resolve().parent
MANIFEST_PATH = PLUGIN_DIR / "manifest.json"

_VALID_API_MODULES = {
    "action_api",
    "adapter_api",
    "agent_api",
    "chat_api",
    "command_api",
    "config_api",
    "database_api",
    "event_api",
    "llm_api",
    "log_api",
    "media_api",
    "message_api",
    "permission_api",
    "person_api",
    "plugin_api",
    "prompt_api",
    "router_api",
    "send_api",
    "service_api",
    "storage_api",
    "stream_api",
    "tool_api",
}


@pytest.fixture(scope="module")
def manifest() -> dict[str, Any]:
    """读取插件 manifest。"""

    return json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))


@pytest.fixture
def plugin() -> BilibiliQueuePlugin:
    """实例化插件（配置为全默认，且关掉自动拉起以免测试真去起进程）。"""

    config = BilibiliQueueConfig()
    config.app.auto_start = False
    return BilibiliQueuePlugin(config=config)


# ── manifest 契约 ─────────────────────────────────────


def test_manifest_name_matches_plugin_name(manifest: dict[str, Any]) -> None:
    """``manifest.name`` 与 ``plugin_name`` 不一致会直接加载失败。"""

    assert manifest["name"] == BilibiliQueuePlugin.plugin_name == "bilibili_queue"


def test_manifest_directory_name_matches(manifest: dict[str, Any]) -> None:
    """目录名、manifest.name、plugin_name 三者保持一致。"""

    assert PLUGIN_DIR.name == manifest["name"]


def test_entry_point_exists(manifest: dict[str, Any]) -> None:
    """``entry_point`` 指向的文件必须存在。"""

    assert (PLUGIN_DIR / manifest["entry_point"]).is_file()


def test_manifest_has_required_fields(manifest: dict[str, Any]) -> None:
    """必备元数据字段齐全。"""

    for field in ("name", "version", "description", "author", "dependencies", "include"):
        assert manifest.get(field), field
    assert isinstance(manifest["dependencies"]["plugins"], list)


def test_api_version_keys_are_known(manifest: dict[str, Any]) -> None:
    """``api_version`` 的 key 必须在白名单里，否则加载器会拒绝加载。"""

    declared = manifest["api_version"]
    assert isinstance(declared, dict)
    assert set(declared) <= _VALID_API_MODULES
    assert set(declared) == {"log_api", "send_api"}


def test_python_dependencies_declared(manifest: dict[str, Any]) -> None:
    """窗口依赖 PySide6，仍然声明出来让框架自动安装（app 的窗口要用）。"""

    assert any("PySide6" in item for item in manifest["python_dependencies"])
    assert manifest["dependencies_required"] is False


def test_manifest_include_matches_code(
    manifest: dict[str, Any], plugin: BilibiliQueuePlugin
) -> None:
    """manifest 声明的组件 == 代码里真实提供的组件 + 配置类。"""

    declared = {
        (item["component_type"], item["component_name"]) for item in manifest["include"]
    }
    provided = {(item.component_type, item.name) for item in plugin.get_components()}  # type: ignore[attr-defined]
    expected = provided | {("config", BilibiliQueueConfig.name)}

    assert declared == expected


def test_manifest_has_no_router_component(manifest: dict[str, Any]) -> None:
    """HTTP 服务与页面已迁到独立应用，插件不应再注册 Router。"""

    types = {item["component_type"] for item in manifest["include"]}
    assert "router" not in types


def test_all_components_have_names() -> None:
    """每个组件的统一属性 ``name`` 都不能为空。"""

    for component in (
        QueueService,
        DanmakuQueueHandler,
        QueueCommand,
        QueueAdminCommand,
    ):
        assert component.name, component.__name__
        assert component.description, component.__name__


# ── 插件类契约 ────────────────────────────────────────


def test_plugin_metadata_only_in_manifest(plugin: BilibiliQueuePlugin) -> None:
    """插件类上不能再写 plugin_version / plugin_description（会触发弃用告警）。"""

    assert not hasattr(BilibiliQueuePlugin, "plugin_version")
    assert not hasattr(BilibiliQueuePlugin, "plugin_description")
    assert not hasattr(BilibiliQueuePlugin, "plugin_author")


def test_get_components_returns_classes(plugin: BilibiliQueuePlugin) -> None:
    """``get_components()`` 必须返回类（不是实例），且不含配置类。"""

    components = plugin.get_components()

    assert all(isinstance(item, type) for item in components)
    assert set(components) == {
        QueueService,
        DanmakuQueueHandler,
        QueueCommand,
        QueueAdminCommand,
    }
    assert BilibiliQueueConfig not in components


def test_configs_declared_on_plugin_class() -> None:
    """配置类只能放在 ``configs`` 上。"""

    assert BilibiliQueuePlugin.configs == [BilibiliQueueConfig]


def test_plugin_is_registered() -> None:
    """``@register_plugin`` 必须真的把类注册进全局注册表。"""

    from src.core.components.loader import _plugin_registry

    assert _plugin_registry.get("bilibili_queue") is BilibiliQueuePlugin


# ── 配置渲染 ──────────────────────────────────────────


def test_config_default_generation_round_trip(tmp_path: Path) -> None:
    """默认配置必须能渲染成合法 TOML 并再读回来。

    首次启动就是这条路径：配置文件不存在 → 框架调 ``generate_default`` 生成 →
    立刻 ``load`` 注入插件。任何把描述写成非法 TOML、或者字段类型对不上的改动
    都会在这里暴露，而不是等到主播启动 bot 时才炸。
    """

    path = tmp_path / "config.toml"
    BilibiliQueueConfig.generate_default(path)
    text = path.read_text(encoding="utf-8")

    for section in ("[plugin]", "[keywords]", "[admin]", "[behavior]", "[app]", "[debug]"):
        assert section in text, section

    loaded = BilibiliQueueConfig.load(path)
    assert isinstance(loaded, BilibiliQueueConfig)

    from plugins.bilibili_queue.core.settings import DEFAULT_APP_PORT, PluginSettings

    settings = PluginSettings.from_config(loaded)
    assert settings.app_port == DEFAULT_APP_PORT
    assert settings.join_keywords
    assert settings.admin_min_rank == "captain"


def test_config_tolerates_broken_values_on_startup_path(tmp_path: Path) -> None:
    """手改坏的配置项要退回默认值，不能让插件起不来。

    这里用的是**启动时真实走的路径**（``auto_update=True``）：框架会把
    ``_merge_with_model_defaults`` 包在 try 里，类型不对的字段退回默认值并回写。
    （``load(path)`` 的默认 ``auto_update=False`` 是严格路径，会直接抛
    ValidationError——插件加载器走的是 ``auto_update=True``。）
    """

    path = tmp_path / "config.toml"
    path.write_text(
        "[keywords]\n"
        'join = ["上车"]\n'
        "\n"
        "[app]\n"
        'port = "不是数字"\n'
        "auto_start = false\n",
        encoding="utf-8",
    )

    loaded = BilibiliQueueConfig.load(path, auto_update=True)
    from plugins.bilibili_queue.core.settings import DEFAULT_APP_PORT, PluginSettings

    settings = PluginSettings.from_config(loaded)
    assert settings.join_keywords == ["上车"]
    assert settings.app_port == DEFAULT_APP_PORT  # 类型不对 → 默认值
    assert settings.app_auto_start is False

    # 回写后文件里也应恢复成合法整数
    assert "port = 17650" in path.read_text(encoding="utf-8")


# ── 生命周期 ──────────────────────────────────────────


class _FakeTaskManager:
    """替掉真实任务管理器。

    真实实现会初始化 multiprocessing（进程池/管道），在受限环境里会因权限被拒；
    这里只关心「插件有没有按约定把后台预热任务交出去」。
    """

    def __init__(self) -> None:
        """初始化记录容器。"""

        self.created: list[str] = []
        self.cancelled: list[str] = []

    def create_task(self, coro: Any, name: str | None = None, **_kwargs: Any) -> Any:
        """记录一次任务创建，并关闭协程避免「never awaited」告警。"""

        self.created.append(str(name))
        coro.close()
        return SimpleNamespace(task_id=f"task-{len(self.created)}")

    def cancel_task(self, task_id: str) -> bool:
        """记录一次任务取消。"""

        self.cancelled.append(task_id)
        return True


@pytest.fixture
def fake_task_manager(monkeypatch: pytest.MonkeyPatch) -> _FakeTaskManager:
    """把插件里的 task_manager 换成替身。"""

    manager = _FakeTaskManager()
    monkeypatch.setattr(plugin_module, "get_task_manager", lambda: manager)
    return manager


async def test_plugin_lifecycle_registers_runtime(
    plugin: BilibiliQueuePlugin, fake_task_manager: _FakeTaskManager
) -> None:
    """加载后运行时单例可用、后台预热任务已提交；卸载后清理干净。

    配置里关掉了自动拉起，所以这里不会真的去起 live_queue 进程。
    """

    clear_queue_runtime()
    try:
        await plugin.on_plugin_loaded()

        runtime = get_queue_runtime()
        assert runtime is not None
        assert runtime.settings.app_auto_start is False
        assert plugin._runtime is runtime
        assert fake_task_manager.created == ["bilibili_queue_warmup"]
    finally:
        await plugin.on_plugin_unloaded()

    assert get_queue_runtime() is None
    assert plugin._runtime is None
    assert fake_task_manager.cancelled == ["task-1"]


async def test_plugin_load_survives_task_manager_failure(
    plugin: BilibiliQueuePlugin, monkeypatch: pytest.MonkeyPatch
) -> None:
    """任务管理器不可用时插件仍要加载成功（预热只是尽力而为）。"""

    class BrokenTaskManager:
        """永远抛异常的替身。"""

        def create_task(self, coro: Any, **_kwargs: Any) -> Any:
            """先关掉协程再抛异常（避免 never-awaited 告警）。"""

            coro.close()
            raise PermissionError("沙箱不允许创建进程池")

    monkeypatch.setattr(plugin_module, "get_task_manager", lambda: BrokenTaskManager())

    clear_queue_runtime()
    try:
        await plugin.on_plugin_loaded()
        assert get_queue_runtime() is not None
        assert plugin._warmup_task_id is None
    finally:
        await plugin.on_plugin_unloaded()


async def test_plugin_unload_keeps_app_by_default(
    plugin: BilibiliQueuePlugin, fake_task_manager: _FakeTaskManager
) -> None:
    """默认不关掉独立 app（它是能单独使用的程序）。"""

    stopped: list[bool] = []

    class FakeSupervisor:
        """记录 stop 是否被调用。"""

        def stop(self, **_kwargs: Any) -> bool:
            stopped.append(True)
            return True

    clear_queue_runtime()
    await plugin.on_plugin_loaded()

    runtime = get_queue_runtime()
    assert runtime is not None
    runtime._supervisor = FakeSupervisor()  # type: ignore[assignment]

    await plugin.on_plugin_unloaded()
    assert stopped == []


async def test_plugin_unload_can_stop_app(
    plugin: BilibiliQueuePlugin, fake_task_manager: _FakeTaskManager
) -> None:
    """``app.stop_on_unload=true`` 时才关掉它。"""

    stopped: list[bool] = []

    class FakeSupervisor:
        """记录 stop 是否被调用。"""

        def stop(self, **_kwargs: Any) -> bool:
            stopped.append(True)
            return True

    assert isinstance(plugin.config, BilibiliQueueConfig)
    plugin.config.app.stop_on_unload = True

    clear_queue_runtime()
    await plugin.on_plugin_loaded()

    runtime = get_queue_runtime()
    assert runtime is not None
    runtime._supervisor = FakeSupervisor()  # type: ignore[assignment]

    await plugin.on_plugin_unloaded()
    assert stopped == [True]
