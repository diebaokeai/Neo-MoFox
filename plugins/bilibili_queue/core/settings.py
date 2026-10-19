"""bilibili_queue 插件的运行参数。

**队列语义（默认队名、别名、人数上限、重复策略……）不在这里**——那些由独立应用
``live_queue`` 自己持有，插件只负责「把弹幕翻译成对 app 的调用」。这里只放：

- 弹幕侧的规则（关键词、房管权限、哪些平台、要不要拦截弹幕）；
- 与独立 app 的连接方式（地址、密钥、要不要自动拉起）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

DEFAULT_APP_HOST = "127.0.0.1"
"""独立 app 的默认地址。"""

DEFAULT_APP_PORT = 17650
"""独立 app 的默认端口，必须与 ``live_queue/config.py`` 里的默认值保持一致。"""

DEFAULT_START_TIMEOUT = 20.0
"""等待独立 app 就绪的默认秒数（首次启动要加载 Qt，给宽一点）。"""


def _get(section: Any, name: str, default: Any) -> Any:
    """从配置节上安全读一个字段。

    Args:
        section: 配置节对象，可能是 ``None``。
        name: 字段名。
        default: 读不到时的默认值。

    Returns:
        字段值或默认值。
    """

    if section is None:
        return default
    value = getattr(section, name, default)
    return default if value is None else value


def _get_list(section: Any, name: str, default: list[str]) -> list[str]:
    """从配置节上读一个字符串列表并过滤空值。

    Args:
        section: 配置节对象。
        name: 字段名。
        default: 读不到时的默认值。

    Returns:
        非空字符串组成的列表。
    """

    raw = _get(section, name, default)
    if not isinstance(raw, (list, tuple)):
        return list(default)
    return [str(item) for item in raw if str(item).strip()]


@dataclass
class PluginSettings:
    """插件扁平化后的运行参数。

    所有字段都有默认值：配置没加载、配置文件缺字段、旧版配置残留，都不该让插件
    起不来（队列规则在 app 那边，这里出错影响面更小）。
    """

    enabled: bool = True

    # ── 弹幕关键词 ──
    join_keywords: list[str] = field(
        default_factory=lambda: ["排队", "排个队", "上车", "报名"]
    )
    leave_keywords: list[str] = field(
        default_factory=lambda: ["退队", "下车", "取消排队", "退出排队"]
    )
    status_keywords: list[str] = field(
        default_factory=lambda: ["排队情况", "排队查询", "查询排队", "队列状态", "排到哪了"]
    )
    ignore_case: bool = True

    # ── 房管权限 ──
    admin_min_rank: str = "captain"
    allow_danmaku_commands: bool = True
    command_prefix: str = ""

    # ── 行为 ──
    platforms: list[str] = field(default_factory=lambda: ["bilibili_live"])
    block_message_from_llm: bool = True
    reply_in_stream: bool = False
    reply_on_join: bool = True
    reply_on_leave: bool = True
    reply_cooldown: float = 5.0
    queue_status_command: bool = True

    # ── 独立 app 连接 ──
    app_host: str = DEFAULT_APP_HOST
    app_port: int = DEFAULT_APP_PORT
    app_token: str = ""
    app_auto_start: bool = True
    app_start_timeout: float = DEFAULT_START_TIMEOUT
    app_no_window: bool = False
    app_executable: str = ""
    app_extra_args: list[str] = field(default_factory=list)
    app_stop_on_unload: bool = False

    # ── 调试 ──
    log_danmaku: bool = False
    log_commands: bool = True

    @property
    def app_base_url(self) -> str:
        """独立 app 的接口根地址。"""

        return f"http://{self.app_host}:{self.app_port}"

    @classmethod
    def from_config(cls, config: Any) -> PluginSettings:
        """从插件配置对象构造运行参数。

        Args:
            config: ``BasePlugin.config``，可能是 ``None`` 或任意对象。

        Returns:
            扁平化后的运行参数。
        """

        plugin = getattr(config, "plugin", None)
        keywords = getattr(config, "keywords", None)
        admin = getattr(config, "admin", None)
        behavior = getattr(config, "behavior", None)
        app = getattr(config, "app", None)
        debug = getattr(config, "debug", None)

        return cls(
            enabled=bool(_get(plugin, "enabled", True)),
            join_keywords=_get_list(keywords, "join", ["排队", "排个队", "上车", "报名"]),
            leave_keywords=_get_list(keywords, "leave", ["退队", "下车", "取消排队"]),
            status_keywords=_get_list(keywords, "status", ["排队情况", "队列状态"]),
            ignore_case=bool(_get(keywords, "ignore_case", True)),
            admin_min_rank=str(_get(admin, "min_rank", "captain")),
            allow_danmaku_commands=bool(_get(admin, "allow_danmaku_commands", True)),
            command_prefix=str(_get(admin, "command_prefix", "")),
            platforms=_get_list(behavior, "platforms", ["bilibili_live"]),
            block_message_from_llm=bool(_get(behavior, "block_message_from_llm", True)),
            reply_in_stream=bool(_get(behavior, "reply_in_stream", False)),
            reply_on_join=bool(_get(behavior, "reply_on_join", True)),
            reply_on_leave=bool(_get(behavior, "reply_on_leave", True)),
            reply_cooldown=float(_get(behavior, "reply_cooldown", 5.0)),
            queue_status_command=bool(_get(behavior, "queue_status_command", True)),
            app_host=str(_get(app, "host", DEFAULT_APP_HOST)),
            app_port=int(_get(app, "port", DEFAULT_APP_PORT)),
            app_token=str(_get(app, "token", "")),
            app_auto_start=bool(_get(app, "auto_start", True)),
            app_start_timeout=float(_get(app, "start_timeout", DEFAULT_START_TIMEOUT)),
            app_no_window=bool(_get(app, "no_window", False)),
            app_executable=str(_get(app, "executable", "")),
            app_extra_args=_get_list(app, "extra_args", []),
            app_stop_on_unload=bool(_get(app, "stop_on_unload", False)),
            log_danmaku=bool(_get(debug, "log_danmaku", False)),
            log_commands=bool(_get(debug, "log_commands", True)),
        )


__all__ = [
    "DEFAULT_APP_HOST",
    "DEFAULT_APP_PORT",
    "DEFAULT_START_TIMEOUT",
    "PluginSettings",
]
