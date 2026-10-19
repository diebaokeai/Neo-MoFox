"""排队姬独立启动器：不依赖机器人，双击 / 桌面快捷方式即可用。

行为：
1. 读 live_queue/config.json 得到监听地址（也认 --host/--port 覆盖）；
2. 该地址已经有排队姬在跑 -> 只打开浏览器控制面板，不启动第二个实例；
3. 端口被别的程序占用 -> 给出提示并退出；
4. 否则启动 app.py：装了 PySide6 就开桌面窗口；没装就以无窗口模式运行并自动打开控制面板。

用法：
    python live_queue/start_standalone.py
    python live_queue/start_standalone.py --port 18080
"""

from __future__ import annotations

import json
import socket
import sys
import threading
import time
import urllib.request
import webbrowser
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent
"""应用目录（配置、数据、页面都在它下面）。"""

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 17650
CONFIG_PATH = APP_DIR / "config.json"

# 让脚本无论从哪里被调用都能绝对导入 live_queue
if str(APP_DIR.parent) not in sys.path:
    sys.path.insert(0, str(APP_DIR.parent))


def _normalized_host(host: str) -> str:
    """把监听地址换成可连接 / 可展示的地址。"""

    value = str(host or "").strip()
    if value in {"", "0.0.0.0", "::", "[::]"}:
        return DEFAULT_HOST
    return value


def _load_host_port() -> tuple[str, int]:
    """从 config.json 读 host/port，读不到就用默认值。"""

    host: str = DEFAULT_HOST
    port: int = DEFAULT_PORT
    try:
        raw = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        section = raw.get("server") if isinstance(raw, dict) else None
        if isinstance(section, dict):
            value = str(section.get("host") or "").strip()
            if value:
                host = value
            number = section.get("port")
            if isinstance(number, int) and 0 < number < 65536:
                port = number
    except Exception:
        pass
    return host, port


def _apply_host_port_args(host: str, port: int, args: list[str]) -> tuple[str, int]:
    """让探活地址跟随命令行里的 --host/--port。"""

    for index, token in enumerate(args):
        if token == "--port" and index + 1 < len(args):
            try:
                value = int(args[index + 1])
                if 0 < value < 65536:
                    port = value
            except ValueError:
                pass
        elif token.startswith("--port="):
            try:
                value = int(token.split("=", 1)[1])
                if 0 < value < 65536:
                    port = value
            except ValueError:
                pass
        elif token == "--host" and index + 1 < len(args):
            value = str(args[index + 1]).strip()
            if value:
                host = value
        elif token.startswith("--host="):
            value = token.split("=", 1)[1].strip()
            if value:
                host = value
    return host, port


def _probe(host: str, port: int, timeout: float = 2.0) -> bool:
    """探测该地址上是不是排队姬（GET /api/health）。"""

    url = f"http://{_normalized_host(host)}:{port}/api/health"
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            return int(getattr(response, "status", 0) or 0) == 200
    except Exception:
        return False


def _port_open(host: str, port: int, timeout: float = 1.0) -> bool:
    """该端口是否有程序在监听。"""

    try:
        with socket.create_connection((_normalized_host(host), port), timeout=timeout):
            return True
    except OSError:
        return False


def _open_panel_when_ready(host: str, port: int, timeout: float = 20.0) -> None:
    """后台等 HTTP 服务起来后打开控制面板（无窗口模式用）。"""

    def _worker() -> None:
        deadline = time.time() + timeout
        while time.time() < deadline:
            if _probe(host, port, timeout=1.0):
                webbrowser.open(f"http://{_normalized_host(host)}:{port}/panel")
                return
            time.sleep(0.5)

    threading.Thread(target=_worker, name="live-queue-open-panel", daemon=True).start()


def main(argv: list[str] | None = None) -> int:
    """入口。

    Args:
        argv: 额外参数，原样转给 app.py。

    Returns:
        进程退出码。
    """

    args = list(sys.argv[1:] if argv is None else argv)
    host, port = _apply_host_port_args(*_load_host_port(), args)
    base = f"http://{_normalized_host(host)}:{port}"

    if _probe(host, port):
        print(f"排队姬已经在运行：{base}")
        print(f"  控制面板 -> {base}/panel")
        print("  （没有窗口说明它是无窗口模式启动的；想要窗口请先停掉旧实例）")
        webbrowser.open(f"{base}/panel")
        return 0

    if _port_open(host, port):
        print(f"端口 {port} 已被其它程序占用，而且它不是排队姬。")
        print("  请关掉占用该端口的程序，或修改 live_queue/config.json 的 server.port。")
        return 2

    try:
        import PySide6  # noqa: F401
    except Exception:
        print("未安装 PySide6（没有桌面窗口），将以无窗口模式运行，并自动打开控制面板。")
        print("  想要桌面窗口： pip install PySide6-Essentials")
        _open_panel_when_ready(host, port)
        args = ["--ui", "off", *args]

    from live_queue.app import main as app_main

    return int(app_main(args))


if __name__ == "__main__":
    raise SystemExit(main())
