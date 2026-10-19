"""live_queue 的本地配置。

配置是**应用自己的 JSON 文件**（默认 ``live_queue/config.json``），首次运行自动生成，
不依赖任何机器人框架的配置系统。

手改坏了也不会让应用起不来：类型对不上、字段缺失、多写了不认识的键，都会被打回
默认值（不认识的键会被丢弃，避免越积越乱）。
"""

from __future__ import annotations

import dataclasses
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, TypeVar

T = TypeVar("T")

CONFIG_VERSION = 1
"""配置结构版本，便于以后做迁移。"""


@dataclass
class ServerConfig:
    """HTTP 服务（机器人靠它控制队列，OBS 靠它取叠加页）。"""

    host: str = "127.0.0.1"
    port: int = 17650
    token: str = ""
    """写操作密钥。留空时只允许本机（127.0.0.1）发起写操作。

    机器人通常和本应用在同一台机器上，所以留空即可；跨机器控制时两边都要配。
    """

    flush_interval: float = 5.0
    """脏数据落盘的最小间隔秒数。"""

    purge_interval: float = 30.0
    """检查排队超时的间隔秒数（``entry_timeout`` 为 0 时无实际作用）。"""


@dataclass
class QueueRulesConfig:
    """队列语义规则。放在应用这边，机器人不需要重复配置。"""

    default_queue: str = "默认"
    max_queues: int = 8
    capacity: int = 0
    """每队默认人数上限；``0`` 表示不限。"""

    duplicate_policy: str = "reject"
    """同一观众重复排队时：``reject`` / ``move_to_end`` / ``allow``。"""

    auto_create_queue: bool = False
    """排一个不存在的队时是否自动建队；默认关闭，只允许排已存在的队。"""

    require_explicit_queue: bool = True
    """入队是否必须写明队名；默认开启，不回落默认队。"""

    event_dedupe_seconds: float = 600.0
    """平台事件幂等窗口（秒）：同一个 event_id 在窗口内只处理一次，避免 bot 与独立桥重复入队。"""

    event_dedupe_capacity: int = 5000
    """幂等缓存最大条数（超出后按最早记录淘汰）。"""

    entry_timeout: float = 0.0
    """排队超时自动出队秒数；``0`` 表示永不超时。"""

    aliases: list[str] = field(
        default_factory=lambda: ["排位赛=排位", "上分=排位", "娱乐局=娱乐"]
    )
    """队名别名，格式为 ``别名=正式队名``。"""


@dataclass
class WindowConfig:
    """排队姬窗口。"""

    enabled: bool = True
    title: str = "排队姬"
    width: int = 460
    height: int = 640
    always_on_top: bool = True
    show_buttons: bool = True
    opacity: float = 1.0
    """整窗不透明度 0.3~1.0；窗口采集时建议保持 1.0。"""


@dataclass
class OverlayConfig:
    """OBS 浏览器源叠加页与窗口共用的外观。"""

    title: str = "排队中"
    show_title: bool = False
    queues: list[str] = field(default_factory=list)
    """只显示这些队列；留空显示全部。"""

    max_rows: int = 8
    poll_interval_ms: int = 1000
    show_current: bool = True
    show_wait_time: bool = True
    accent_color: str = "#6fd3ff"
    text_color: str = "#f2f6ff"
    panel_bg: str = "rgba(14, 16, 26, 0.72)"
    panel_border: str = "rgba(120, 200, 255, 0.35)"
    current_bg: str = "rgba(111, 211, 255, 0.16)"
    title_size: int = 22
    name_size: int = 20


@dataclass
class AppConfig:
    """应用总配置。"""

    server: ServerConfig = field(default_factory=ServerConfig)
    queue: QueueRulesConfig = field(default_factory=QueueRulesConfig)
    window: WindowConfig = field(default_factory=WindowConfig)
    overlay: OverlayConfig = field(default_factory=OverlayConfig)

    def to_dict(self) -> dict[str, Any]:
        """导出成可写盘 / 可返回给前端的 dict。

        Returns:
            纯 dict / list / 基本类型组成的结构。
        """

        payload = dataclasses.asdict(self)
        payload["version"] = CONFIG_VERSION
        return payload


def _coerce(current: Any, value: Any) -> Any:
    """按默认值的类型把配置值转成同一类型。

    类型明显对不上时**保留默认值**，而不是硬转（``bool("false")`` 会得到 ``True``，
    这类隐式转换比直接用默认值危险得多）。

    Args:
        current: 默认值（决定目标类型）。
        value: 从配置文件读到的值。

    Returns:
        可安全赋给该字段的值。
    """

    if isinstance(current, bool):
        return value if isinstance(value, bool) else current

    if isinstance(current, int):
        if isinstance(value, bool):
            return current
        if isinstance(value, int):
            return value
        if isinstance(value, float) and value.is_integer():
            return int(value)
        return current

    if isinstance(current, float):
        if isinstance(value, bool):
            return current
        if isinstance(value, (int, float)):
            return float(value)
        return current

    if isinstance(current, str):
        return value if isinstance(value, str) else current

    if isinstance(current, list):
        if not isinstance(value, (list, tuple)):
            return current
        items = list(value)
        # 默认值是「字符串列表」时，顺手把元素规整成非空字符串
        if all(isinstance(item, str) for item in current):
            return [str(item) for item in items if str(item).strip()]
        return items

    return value if isinstance(value, type(current)) else current


def _apply(target: Any, data: Any) -> None:
    """把 dict 里的值合并进 dataclass 实例（就地修改）。

    Args:
        target: 目标 dataclass 实例。
        data: 来源 dict；不是 dict 时什么都不做。
    """

    if not isinstance(data, dict):
        return

    for spec in dataclasses.fields(target):
        if spec.name not in data:
            continue
        current = getattr(target, spec.name)
        value = data[spec.name]
        if dataclasses.is_dataclass(current):
            _apply(current, value)
        else:
            setattr(target, spec.name, _coerce(current, value))


def config_from_dict(data: Any) -> AppConfig:
    """从 dict 构造配置（缺失 / 非法字段走默认值）。

    Args:
        data: 配置 dict。

    Returns:
        配置实例。
    """

    config = AppConfig()
    _apply(config, data)
    return config


def load_config(path: Path) -> tuple[AppConfig, str]:
    """读取配置文件；不存在或损坏时用默认值并回写一份。

    Args:
        path: 配置文件路径。

    Returns:
        ``(配置实例, 状态说明)``，状态说明用于日志（``created`` / ``loaded`` /
        ``recovered``）。
    """

    if not path.exists():
        config = AppConfig()
        save_config(path, config)
        return config, "created"

    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        config = AppConfig()
        save_config(path, config)
        return config, "recovered"

    if not isinstance(raw, dict):
        config = AppConfig()
        save_config(path, config)
        return config, "recovered"

    return config_from_dict(raw), "loaded"


def save_config(path: Path, config: AppConfig) -> None:
    """把配置写盘。

    Args:
        path: 配置文件路径。
        config: 配置实例。

    Raises:
        OSError: 写盘失败。
    """

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(config.to_dict(), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


__all__ = [
    "CONFIG_VERSION",
    "AppConfig",
    "OverlayConfig",
    "QueueRulesConfig",
    "ServerConfig",
    "WindowConfig",
    "config_from_dict",
    "load_config",
    "save_config",
]
