"""bilibili_queue 插件运行参数（PluginSettings）的单测。"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from plugins.bilibili_queue.core.settings import (
    DEFAULT_APP_HOST,
    DEFAULT_APP_PORT,
    PluginSettings,
)


def _config(**overrides: Any) -> Any:
    """构造一个形状类似插件配置的对象。

    Args:
        **overrides: 覆盖项，形如 ``app__port=1234``。

    Returns:
        可用 ``getattr`` 逐节访问的配置对象。
    """

    sections: dict[str, Any] = {
        "plugin": SimpleNamespace(enabled=True),
        "keywords": SimpleNamespace(
            join=["排队"], leave=["退队"], status=["排队情况"], ignore_case=True
        ),
        "admin": SimpleNamespace(
            min_rank="captain", allow_danmaku_commands=True, command_prefix=""
        ),
        "behavior": SimpleNamespace(
            platforms=["bilibili_live"],
            block_message_from_llm=True,
            reply_in_stream=False,
            reply_on_join=True,
            reply_on_leave=True,
            reply_cooldown=5.0,
            queue_status_command=True,
        ),
        "app": SimpleNamespace(
            host=DEFAULT_APP_HOST,
            port=DEFAULT_APP_PORT,
            token="",
            auto_start=True,
            start_timeout=20.0,
            no_window=False,
            executable="",
            extra_args=[],
            stop_on_unload=False,
        ),
        "debug": SimpleNamespace(log_danmaku=False, log_commands=True),
    }
    for key, value in overrides.items():
        section, _, field = key.partition("__")
        if field:
            setattr(sections[section], field, value)
        else:
            sections[key] = value
    return SimpleNamespace(**sections)


# ── 默认值 ────────────────────────────────────────────


def test_defaults_when_config_missing() -> None:
    """配置读不到时全默认，插件仍能起来。"""

    settings = PluginSettings.from_config(None)

    assert settings.enabled is True
    assert settings.join_keywords == ["排队", "排个队", "上车", "报名"]
    assert settings.platforms == ["bilibili_live"]
    assert settings.app_host == DEFAULT_APP_HOST
    assert settings.app_port == DEFAULT_APP_PORT
    assert settings.app_auto_start is True
    assert settings.app_stop_on_unload is False


def test_app_base_url() -> None:
    """接口根地址拼装。"""

    settings = PluginSettings(app_host="192.168.1.9", app_port=18080)
    assert settings.app_base_url == "http://192.168.1.9:18080"


def test_default_port_matches_live_queue() -> None:
    """插件默认端口必须和独立应用的默认端口一致，否则开箱即用就是坏的。"""

    from live_queue.config import ServerConfig

    assert DEFAULT_APP_PORT == ServerConfig().port


# ── 从配置映射 ────────────────────────────────────────


def test_reads_every_section() -> None:
    """每个配置节都要映射进来。"""

    settings = PluginSettings.from_config(
        _config(
            keywords__join=["上车"],
            admin__min_rank="admiral",
            behavior__platforms=["bilibili_live", "onebot"],
            app__port=19000,
            app__token="tok",
            debug__log_danmaku=True,
        )
    )

    assert settings.join_keywords == ["上车"]
    assert settings.admin_min_rank == "admiral"
    assert settings.platforms == ["bilibili_live", "onebot"]
    assert settings.app_port == 19000
    assert settings.app_token == "tok"
    assert settings.log_danmaku is True


def test_blank_keywords_are_filtered() -> None:
    """关键词列表里的空项被过滤，避免「所有弹幕都命中」。"""

    settings = PluginSettings.from_config(_config(keywords__join=["排队", " ", ""]))
    assert settings.join_keywords == ["排队"]


def test_wrong_section_type_falls_back() -> None:
    """某一节类型不对时退回默认值。"""

    config = _config()
    config.app = ["not", "a", "section"]
    settings = PluginSettings.from_config(config)

    assert settings.app_port == DEFAULT_APP_PORT
    assert settings.app_auto_start is True


def test_missing_fields_use_section_defaults() -> None:
    """节存在但缺字段时用默认值。"""

    config = _config()
    config.behavior = SimpleNamespace(platforms=["bilibili_live"])
    settings = PluginSettings.from_config(config)

    assert settings.reply_cooldown == 5.0
    assert settings.reply_on_join is True
    assert settings.queue_status_command is True
