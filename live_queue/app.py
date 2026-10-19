"""live_queue 应用入口。

用法（不需要机器人，也不需要 mofox 仓库里的任何东西）::

    python live_queue/app.py                 # 起 HTTP 服务 + 打开排队姬窗口
    python live_queue/app.py --ui off        # 只起服务（headless，适合放服务器/远程）
    python live_queue/app.py --port 18080    # 换端口
    python live_queue/app.py --selftest      # 不监听端口，只自检配置与界面

启动后会打印两个地址：

- ``/overlay``：填进 OBS / 直播姬的「浏览器源」；
- ``/panel``：主播自己用的控制面板。

机器人（mofox 插件）通过同一套 ``/api/*`` 接口控制它，见插件 README。
"""

from __future__ import annotations

import argparse
import logging
import sys
import threading
from pathlib import Path
from typing import Any

APP_DIR = Path(__file__).resolve().parent
"""应用目录（配置、数据、页面都在它下面）。"""

# 让 `python live_queue/app.py` 与 `python -m live_queue.app` 都能用绝对导入
if str(APP_DIR.parent) not in sys.path:
    sys.path.insert(0, str(APP_DIR.parent))

from live_queue import __version__
from live_queue.config import AppConfig, load_config, save_config
from live_queue.hub import QueueHub
from live_queue.server import QueueHTTPServer, ServerStartError
from live_queue.store import QueueStore

DEFAULT_CONFIG_PATH = APP_DIR / "config.json"
"""默认配置文件路径。"""

DEFAULT_DATA_PATH = APP_DIR / "data" / "queues.json"
"""默认状态文件路径。"""

WEB_DIR = APP_DIR / "web"
"""内置页面目录。"""


def _build_logger(level: str) -> logging.Logger:
    """构造控制台日志器。

    Args:
        level: 日志级别名（``DEBUG`` / ``INFO`` / ...）。

    Returns:
        日志器实例。
    """

    logger = logging.getLogger("live_queue")
    logger.setLevel(getattr(logging, level.upper(), logging.INFO))
    if not logger.handlers:
        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(logging.Formatter("[%(asctime)s] %(levelname)s %(message)s", "%H:%M:%S"))
        logger.addHandler(handler)
    return logger


def build_parser() -> argparse.ArgumentParser:
    """构造命令行解析器。

    Returns:
        解析器实例。
    """

    parser = argparse.ArgumentParser(
        prog="live_queue",
        description="独立运行的直播排队应用（排队姬）：多命名队列 + OBS 叠加页 + 可选桌面窗口",
    )
    parser.add_argument("--version", action="version", version=f"live_queue {__version__}")
    parser.add_argument("--config", default=str(DEFAULT_CONFIG_PATH), help="配置文件路径")
    parser.add_argument("--data", default="", help="队列状态文件路径（默认 live_queue/data/queues.json）")
    parser.add_argument("--host", default="", help="HTTP 监听地址（默认取配置，127.0.0.1）")
    parser.add_argument("--port", type=int, default=0, help="HTTP 监听端口（默认取配置）")
    parser.add_argument("--token", default="", help="写操作密钥；留空则只允许本机写")
    parser.add_argument(
        "--ui",
        choices=("auto", "on", "off"),
        default="auto",
        help="界面模式：auto=装了 PySide6 就开窗口，on=必须有窗口，off=只用 HTTP 服务",
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=("DEBUG", "INFO", "WARNING", "ERROR"),
        help="日志级别",
    )
    parser.add_argument(
        "--no-confirm-close",
        action="store_true",
        help="关窗时不弹确认（自动化 / 冒烟测试用）",
    )
    parser.add_argument(
        "--exit-after",
        type=float,
        default=0.0,
        help="运行指定秒数后自动退出；0 表示一直运行（冒烟测试用）",
    )
    parser.add_argument(
        "--selftest",
        action="store_true",
        help="只加载配置与数据、不做监听，然后退出（检查环境用）",
    )
    return parser


def apply_overrides(config: AppConfig, args: argparse.Namespace) -> AppConfig:
    """把命令行参数覆盖到配置上。

    Args:
        config: 从文件读到的配置。
        args: 命令行参数。

    Returns:
        覆盖后的配置（就地修改并返回同一实例）。
    """

    if args.host:
        config.server.host = str(args.host)
    if args.port:
        config.server.port = int(args.port)
    if args.token:
        config.server.token = str(args.token)
    if args.ui == "off":
        config.window.enabled = False
    elif args.ui == "on":
        config.window.enabled = True
    return config


def resolve_data_path(args: argparse.Namespace) -> Path:
    """确定状态文件路径。

    Args:
        args: 命令行参数。

    Returns:
        状态文件路径。
    """

    if args.data:
        return Path(args.data).expanduser().resolve()
    return DEFAULT_DATA_PATH


def _load_window_class(ui_mode: str, logger: logging.Logger) -> Any:
    """按界面模式决定是否加载窗口类。

    Args:
        ui_mode: ``auto`` / ``on`` / ``off``。
        logger: 日志器。

    Returns:
        ``(QueueWindow 类, create_application 函数)``；不启用界面时返回 ``(None, None)``。
    """

    if ui_mode == "off":
        return None, None

    try:
        from live_queue.ui.window import (
            QueueWindow,
            create_application,
        )

        return QueueWindow, create_application
    except ImportError as exc:
        if ui_mode == "on":
            logger.error(f"--ui on 需要 PySide6，但导入失败：{exc}")
            logger.error("请执行 `pip install PySide6-Essentials`，或改用 --ui off")
            raise SystemExit(3) from exc
        logger.warning(f"未安装 PySide6（{exc}），以无界面模式运行；叠加页与控制面板照常可用")
        return None, None


def main(argv: list[str] | None = None) -> int:
    """程序入口。

    Args:
        argv: 命令行参数；``None`` 时取 ``sys.argv[1:]``。

    Returns:
        进程退出码。
    """

    args = build_parser().parse_args(argv)
    logger = _build_logger(args.log_level)

    config_path = Path(args.config).expanduser().resolve()
    config, state = load_config(config_path)
    config = apply_overrides(config, args)
    if state == "created":
        logger.info(f"已生成默认配置：{config_path}")
        save_config(config_path, config)
    elif state == "recovered":
        logger.warning(f"配置文件损坏，已重置为默认值：{config_path}")

    window_class, create_application = _load_window_class(args.ui, logger)

    data_path = resolve_data_path(args)
    store = QueueStore(data_path)
    hub = QueueHub(config=config, store=store, logger=logger)
    hub.start()

    if args.selftest:
        health = hub.health()
        print(f"SELFTEST OK：config={config_path}")
        print(f"  数据文件={data_path}")
        print(f"  界面={'开启' if window_class else '关闭'}")
        print(f"  队列数={health['queue_count']} 等待={health['total_waiting']}")
        hub.stop()
        return 0

    server = QueueHTTPServer(hub=hub, web_dir=WEB_DIR, logger=logger)
    try:
        server.start()
    except ServerStartError as exc:
        logger.error(str(exc))
        hub.stop()
        return 2

    print(f"排队姬已启动（live_queue {__version__}）")
    print(f"  OBS 浏览器源 → {server.overlay_url}")
    print(f"  控制面板     → {server.panel_url}")
    print(f"  数据文件     → {data_path}")
    if config.server.token:
        print("  写操作需要 X-API-Key")
    else:
        print("  写操作仅限本机（未配置 server.token）")

    if window_class is None:
        return _run_headless(hub, server, args, logger)

    app = create_application()
    window = window_class(
        hub=hub,
        server_url=server.base_url,
        confirm_on_close=not args.no_confirm_close,
        logger=logger,
    )
    window.refresh()
    window.show()

    if args.exit_after > 0:
        from PySide6.QtCore import QTimer

        QTimer.singleShot(int(args.exit_after * 1000), app.quit)

    try:
        return int(app.exec())
    finally:
        window.detach()
        server.stop()
        hub.stop()
        logger.info("排队姬已退出，队列状态已保存")


def _run_headless(
    hub: QueueHub,
    server: QueueHTTPServer,
    args: argparse.Namespace,
    logger: logging.Logger,
) -> int:
    """无界面模式下阻塞运行，直到 Ctrl+C 或到点自动退出。

    Args:
        hub: 队列服务层。
        server: HTTP 服务。
        args: 命令行参数。
        logger: 日志器。

    Returns:
        进程退出码。
    """

    stop = threading.Event()

    def _on_signal(_signum: int, _frame: Any) -> None:
        """收到中断信号时请求退出。

        Args:
            _signum: 信号编号。
            _frame: 当前栈帧。
        """

        stop.set()

    try:
        import signal

        signal.signal(signal.SIGINT, _on_signal)
        signal.signal(signal.SIGTERM, _on_signal)
    except (ValueError, OSError) as exc:
        logger.debug(f"注册信号处理失败（不影响运行）：{exc}")

    if args.exit_after > 0:
        stop.wait(args.exit_after)
    else:
        logger.info("无界面模式运行中，按 Ctrl+C 退出")
        try:
            while not stop.is_set():
                # 用带超时的 wait 让 Ctrl+C 能被及时处理
                stop.wait(0.5)
        except KeyboardInterrupt:
            logger.info("收到中断信号")

    server.stop()
    hub.stop()
    logger.info("排队姬已退出，队列状态已保存")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
