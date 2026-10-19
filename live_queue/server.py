"""live_queue 的 HTTP 服务（标准库实现，零第三方依赖）。

它同时服务三类调用方：

- **机器人**（mofox 插件）：把弹幕观众加进队列、叫号、暂停 —— 走 ``/api/*``；
- **OBS / 直播姬**：浏览器源拉 ``/overlay`` 透明叠加页；
- **主播**：浏览器打开 ``/panel`` 用控制面板。

为什么不用 FastAPI 之类的框架：这是个要能单独拷走运行的小工具，用标准库
:mod:`http.server` 就够了，依赖面越小越好装。代价是要自己写路由和鉴权，都在下面。

鉴权规则（只管**写**操作，读操作一律放行，因为 OBS 浏览器源不方便加请求头）：

- 配置了 ``server.token`` → 必须带 ``X-API-Key``（或 ``?token=``）且完全一致；
- 没配 → 只允许来自本机的写操作。

绑定失败时会给出明确原因：``WinError 10048`` 是端口被占用，``WinError 10013`` 是端口
落在 Windows 的系统保留区间（这种端口即使没人监听也绑不上，换个端口即可）。
"""

from __future__ import annotations

import json
import secrets
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from . import __version__
from .hub import QueueHub

_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost", "testclient"})
"""无需 token 即可写队列的来源地址。"""

_NO_STORE = "no-store, max-age=0"
"""禁止浏览器 / OBS 缓存页面与状态。"""

_MAX_BODY_BYTES = 1 << 20
"""请求体上限（1 MiB）。队列操作都是小 JSON，超过这个量级一定是异常请求。"""

_MAX_DRAIN_BYTES = 8 << 20
"""超限请求体最多排空多少字节。超过这个量级就直接断连接，不值得为它浪费带宽。"""

_CORS_HEADERS = {
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
    "Access-Control-Allow-Headers": "Content-Type, X-API-Key",
}
"""给第三方工具（例如自建控制页）留的跨源头；内置页面是同源的，用不到。"""


class ServerStartError(RuntimeError):
    """HTTP 服务启动失败（端口占用或被系统保留等）。"""


def _is_loopback(host: str) -> bool:
    """判断来源是否为本机。

    Args:
        host: 客户端地址字符串。

    Returns:
        是本机返回 ``True``；IPv6 映射形式（``::ffff:127.0.0.1``）也算。
    """

    if host in _LOOPBACK_HOSTS:
        return True
    return host.startswith(("::ffff:127.", "127."))


class QueueRequestHandler(BaseHTTPRequestHandler):
    """请求处理器。

    由 :class:`QueueHTTPServer` 动态派生出子类，并把 ``hub`` / ``web_dir`` /
    ``logger`` 挂成类属性。
    """

    server_version = f"live_queue/{__version__}"
    protocol_version = "HTTP/1.1"
    """用 HTTP/1.1 + 正确的 Content-Length，让 OBS 的每秒轮询走 keep-alive。"""

    hub: QueueHub
    web_dir: Path
    logger: Any = None

    # ── 入口 ──────────────────────────────────────────

    def do_GET(self) -> None:
        """处理 GET。"""

        self._handle("GET")

    def do_POST(self) -> None:
        """处理 POST。"""

        self._handle("POST")

    def do_OPTIONS(self) -> None:
        """处理 CORS 预检。"""

        self._respond(204, b"", "text/plain; charset=utf-8", extra=_CORS_HEADERS)

    # ── 路由 ──────────────────────────────────────────

    def _handle(self, method: str) -> None:
        """按路径分发请求。

        Args:
            method: ``GET`` 或 ``POST``。
        """

        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"
        query = parse_qs(parsed.query)

        try:
            if method == "GET":
                self._route_get(path, query)
            else:
                self._route_post(path)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            # 客户端（OBS 刷新、控制台切页、机器人超时）直接断开是常态，不是错误
            self.close_connection = True
        except Exception as exc:  # 任何未预期异常都返回 JSON，不让连接直接断掉
            self._log("error", f"处理 {method} {self.path} 失败：{exc}")
            try:
                self._send_json(500, {"ok": False, "message": f"服务内部错误：{exc}"})
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                self.close_connection = True

    def _route_get(self, path: str, query: dict[str, list[str]]) -> None:
        """处理只读路由。

        Args:
            path: 去掉尾部斜杠的路径。
            query: 查询参数。
        """

        if path in ("/", "/overlay"):
            self._send_html(self._load_page("overlay.html"))
            return
        if path == "/panel":
            self._send_html(self._load_page("control.html"))
            return
        if path == "/api/state":
            self._send_json(200, self._state_payload(query))
            return
        if path == "/api/config":
            self._send_json(200, self.hub.overlay_config())
            return
        if path == "/api/health":
            self._send_json(200, self.hub.health())
            return
        if path == "/api/queues":
            self._send_json(200, {"ok": True, "queues": self.hub.queue_names()})
            return
        if path == "/api/whoami":
            self._send_json(200, self._whoami_payload())
            return
        self._send_json(404, {"ok": False, "message": f"未知路径：{path}"})

    def _route_post(self, path: str) -> None:
        """处理写路由。

        **先把请求体完整读掉**再分支：HTTP/1.1 的 keep-alive 连接上，没读完的请求体
        会被当成下一个请求的开头，症状是下一条请求变成 ``{}GET`` 这种鬼东西然后 501。
        所以在 whoami / 鉴权失败这些提前返回的分支里也必须先排空 body。

        Args:
            path: 去掉尾部斜杠的路径。
        """

        raw = self._read_raw_body()
        if raw is None:
            return  # 超长请求体，已经回过 413

        if path == "/api/whoami":
            # 探测接口不算写操作，谁都能调
            self._send_json(200, self._whoami_payload())
            return

        if not self._can_write():
            self._send_json(403, {"ok": False, "message": self._forbidden_reason()})
            return

        body = self._parse_json(raw)
        if body is None:
            return  # 已经在 _parse_json 里回过 400

        handler = self._write_routes().get(path)
        if handler is None:
            self._send_json(404, {"ok": False, "message": f"未知路径：{path}"})
            return

        self._send_json(200, handler(body))

    def _write_routes(self) -> dict[str, Any]:
        """构造写操作路由表。

        Returns:
            路径 → 处理函数的映射；处理函数接收请求体 dict，返回响应 dict。
        """

        hub = self.hub
        return {
            "/api/join": lambda b: hub.join(
                user_id=str(b.get("user_id") or ""),
                nickname=str(b.get("nickname") or ""),
                platform=str(b.get("platform") or ""),
                queue=str(b.get("queue") or ""),
                queue_explicit=bool(b.get("queue_explicit", True)),
                extra=b.get("extra") if isinstance(b.get("extra"), dict) else None,
                guild=str(b.get("guild") or ""),
                event_id=str(b.get("event_id") or ""),
            ),
            "/api/leave": lambda b: hub.leave(
                user_id=str(b.get("user_id") or ""),
                queue=str(b.get("queue") or ""),
                queue_explicit=bool(b.get("queue_explicit", False)),
                event_id=str(b.get("event_id") or ""),
            ),
            "/api/add": lambda b: hub.add_manual(
                nickname=str(b.get("nickname") or ""), queue=str(b.get("queue") or "")
            ),
            "/api/call_next": lambda b: hub.call_next(str(b.get("queue") or "")),
            "/api/finish": lambda b: hub.finish_current(str(b.get("queue") or "")),
            "/api/requeue": lambda b: hub.requeue_current(str(b.get("queue") or "")),
            "/api/clear": lambda b: hub.clear(str(b.get("queue") or "")),
            "/api/pause": lambda b: hub.set_paused(
                str(b.get("queue") or ""), paused=bool(b.get("paused", True))
            ),
            "/api/capacity": lambda b: hub.set_capacity(
                str(b.get("queue") or ""), _as_int(b.get("capacity"), 0)
            ),
            "/api/create": lambda b: hub.create_queue(
                str(b.get("queue") or ""),
                display_name=str(b.get("display_name") or ""),
                capacity=(
                    _as_int(b.get("capacity"), 0) if _as_int(b.get("capacity"), 0) > 0 else None
                ),
            ),
            "/api/delete": lambda b: hub.delete_queue(str(b.get("queue") or "")),
            "/api/reset": lambda b: hub.reset(delete_queues=bool(b.get("delete", False))),
            "/api/remove": lambda b: hub.remove_user(
                str(b.get("user_id") or ""), str(b.get("queue") or "")
            ),
            "/api/move_front": lambda b: hub.move_front(
                str(b.get("user_id") or ""), str(b.get("queue") or "")
            ),
            "/api/flush": lambda _b: {
                "ok": True,
                "status": "flushed",
                "message": "已保存" if hub.flush(force=True) else "无需保存",
            },
        }

    # ── 业务辅助 ──────────────────────────────────────

    def _state_payload(self, query: dict[str, list[str]]) -> dict[str, Any]:
        """组装 ``/api/state`` 的响应体。

        Args:
            query: 查询参数（``rev`` / ``queue`` / ``limit``）。

        Returns:
            响应 dict；``rev`` 命中当前版本时只回 ``changed=False``。
        """

        queue = _first(query, "queue", "")
        limit = _as_int(_first(query, "limit", 0), 0)
        snapshot = self.hub.snapshot(queue=queue, limit=max(0, limit))

        rev_raw = _first(query, "rev", None)
        if rev_raw is not None:
            try:
                rev = int(rev_raw)
            except (TypeError, ValueError):
                rev = None
            if rev is not None and rev == snapshot.get("revision"):
                return {"ok": True, "changed": False, "revision": rev}

        return {"changed": True, **snapshot}

    def _whoami_payload(self) -> dict[str, Any]:
        """组装 ``/api/whoami`` 的响应体。

        Returns:
            含写权限信息的 dict。
        """

        return {
            "ok": True,
            "can_write": self._can_write(),
            "token_required": bool(self.hub.config.server.token.strip()),
            "client": self.client_address[0] if self.client_address else "",
        }

    def _can_write(self) -> bool:
        """判断当前请求是否有写权限。

        Returns:
            有权限返回 ``True``。
        """

        token = self.hub.config.server.token.strip()
        if token:
            provided = self.headers.get("X-API-Key") or _first(
                parse_qs(urlparse(self.path).query), "token", ""
            )
            return bool(provided) and secrets.compare_digest(str(provided), token)
        return _is_loopback(self.client_address[0] if self.client_address else "")

    def _forbidden_reason(self) -> str:
        """写权限被拒时的可读原因。

        Returns:
            提示文案。
        """

        if self.hub.config.server.token.strip():
            return "管理密钥不正确（请求头 X-API-Key）"
        return "未配置 server.token，仅允许本机操作队列"

    def _read_raw_body(self) -> bytes | None:
        """读取完整请求体。

        Args:
            raw: 无。

        Returns:
            请求体字节；没有 body 时返回空字节。请求体超过上限时已回复 413 并返回
            ``None``（同时关闭连接，避免半截 body 污染后续请求）。
        """

        try:
            length = int(self.headers.get("Content-Length") or 0)
        except (TypeError, ValueError):
            length = 0

        if length <= 0:
            return b""

        if length > _MAX_BODY_BYTES:
            # 关键：先把 body 排空再回错。直接关连接会让还在写数据的客户端收到 RST，
            # 连 413 都读不到；只有大到离谱的请求才值得放弃连接。
            if length <= _MAX_DRAIN_BYTES:
                remaining = length
                while remaining > 0:
                    chunk = self.rfile.read(min(65536, remaining))
                    if not chunk:
                        break
                    remaining -= len(chunk)
            else:
                self.close_connection = True
            self._send_json(
                413,
                {"ok": False, "message": f"请求体过大（上限 {_MAX_BODY_BYTES} 字节）"},
            )
            return None

        try:
            return self.rfile.read(length)
        except OSError as exc:
            self._log("warning", f"读取请求体失败：{exc}")
            self.close_connection = True
            return b""

    def _parse_json(self, raw: bytes) -> dict[str, Any] | None:
        """把请求体解析成 JSON 对象。

        Args:
            raw: 请求体字节。

        Returns:
            解析后的 dict；解析失败时已回复 400 并返回 ``None``。
        """

        if not raw:
            return {}

        try:
            parsed = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._send_json(400, {"ok": False, "message": "请求体不是合法 JSON"})
            return None

        if not isinstance(parsed, dict):
            self._send_json(400, {"ok": False, "message": "请求体必须是 JSON 对象"})
            return None
        return parsed

    def _load_page(self, filename: str) -> str:
        """读取内置页面。

        Args:
            filename: ``web/`` 下的文件名。

        Returns:
            页面内容；缺失时返回一个说明性的 HTML。
        """

        path = self.web_dir / filename
        try:
            return path.read_text(encoding="utf-8")
        except OSError as exc:
            self._log("error", f"读取页面 {path} 失败：{exc}")
            return (
                "<!DOCTYPE html><meta charset='utf-8'>"
                f"<h3>页面缺失</h3><p>找不到 {filename}，请确认 web/ 目录完整。</p>"
            )

    # ── 响应 ──────────────────────────────────────────

    def _send_json(self, status: int, payload: dict[str, Any]) -> None:
        """返回 JSON 响应。

        Args:
            status: HTTP 状态码。
            payload: 响应体。
        """

        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self._respond(status, body, "application/json; charset=utf-8", extra=_CORS_HEADERS)

    def _send_html(self, html: str) -> None:
        """返回 HTML 响应。

        Args:
            html: 页面内容。
        """

        self._respond(
            200, html.encode("utf-8"), "text/html; charset=utf-8", extra=_CORS_HEADERS
        )

    def _respond(
        self,
        status: int,
        body: bytes,
        content_type: str,
        *,
        extra: dict[str, str] | None = None,
    ) -> None:
        """发送响应。

        Args:
            status: HTTP 状态码。
            body: 响应体字节。
            content_type: Content-Type。
            extra: 额外响应头。
        """

        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", _NO_STORE)
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        self.end_headers()
        if body:
            self.wfile.write(body)

    def _log(self, level: str, message: str) -> None:
        """按级别记日志。

        Args:
            level: 日志级别名。
            message: 日志正文。
        """

        if self.logger is None:
            return
        emit = getattr(self.logger, level, None)
        if callable(emit):
            emit(message)

    def log_message(self, format: str, *args: Any) -> None:
        """覆盖基类的 stderr 日志，改为 debug 级（避免每次轮询都刷屏）。

        Args:
            format: 格式串。
            *args: 格式参数。
        """

        self._log("debug", f"{self.client_address[0]} {format % args}")


class QueueHTTPServer:
    """把 :class:`QueueRequestHandler` 跑在后台线程里。"""

    def __init__(
        self,
        *,
        hub: QueueHub,
        web_dir: Path,
        host: str | None = None,
        port: int | None = None,
        logger: Any = None,
    ) -> None:
        """记录绑定参数，不立即监听。

        Args:
            hub: 队列服务层。
            web_dir: 内置页面目录。
            host: 监听地址；``None`` 时取配置。
            port: 监听端口；``None`` 时取配置。
            logger: 可选日志器。
        """

        self._hub = hub
        self._web_dir = Path(web_dir)
        self._host = host or hub.config.server.host
        self._port = int(port if port is not None else hub.config.server.port)
        self._logger = logger
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    # ── 属性 ──────────────────────────────────────────

    @property
    def host(self) -> str:
        """监听地址。"""

        return self._host

    @property
    def port(self) -> int:
        """监听端口。"""

        return self._port

    @property
    def base_url(self) -> str:
        """对外可访问的根地址（不含路径）。"""

        return f"http://{self._host}:{self._port}"

    @property
    def overlay_url(self) -> str:
        """OBS 浏览器源应填的地址。"""

        return f"{self.base_url}/overlay"

    @property
    def panel_url(self) -> str:
        """控制面板地址。"""

        return f"{self.base_url}/panel"

    @property
    def is_running(self) -> bool:
        """服务是否在跑。"""

        return self._server is not None

    # ── 生命周期 ──────────────────────────────────────

    def start(self) -> None:
        """绑定端口并在后台线程开始服务。

        Raises:
            ServerStartError: 端口被占用或被系统保留。
        """

        handler_class = type(
            "BoundQueueRequestHandler",
            (QueueRequestHandler,),
            {"hub": self._hub, "web_dir": self._web_dir, "logger": self._logger},
        )

        # 关掉 SO_REUSEADDR：Windows 上它会允许两个进程绑同一个端口，
        # 那「端口已被占用」这个判断就失效了
        class _Server(ThreadingHTTPServer):
            allow_reuse_address = False
            daemon_threads = True

            def handle_error(self, request: Any, client_address: Any) -> None:
                """覆盖默认实现，把「客户端主动断开」这类常态错误静音。

                Args:
                    request: 触发错误的请求。
                    client_address: 客户端地址。
                """

                exc = sys.exc_info()[1]
                if isinstance(
                    exc, (ConnectionResetError, BrokenPipeError, ConnectionAbortedError)
                ):
                    return
                super().handle_error(request, client_address)

        try:
            self._server = _Server((self._host, self._port), handler_class)
        except OSError as exc:
            raise ServerStartError(self._explain_bind_error(exc)) from exc

        # 传 port=0 时由系统分配空闲端口（测试用），这里回填真实端口
        bound = self._server.server_address
        if isinstance(bound, tuple) and len(bound) >= 2:
            self._port = int(bound[1])

        self._thread = threading.Thread(
            target=self._server.serve_forever, name="live_queue_http", daemon=True
        )
        self._thread.start()
        self._log("info", f"HTTP 服务已启动：{self.base_url}")

    def stop(self, *, timeout: float = 5.0) -> None:
        """停止服务。

        Args:
            timeout: 等待服务线程退出的秒数。
        """

        server = self._server
        if server is not None:
            server.shutdown()
            server.server_close()
        self._server = None

        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=timeout)
        self._thread = None
        self._log("info", "HTTP 服务已停止")

    @staticmethod
    def _explain_bind_error(exc: OSError) -> str:
        """把绑定失败翻译成可操作的提示。

        Args:
            exc: 绑定抛出的异常。

        Returns:
            提示文案。
        """

        winerror = getattr(exc, "winerror", None)
        if winerror == 10048 or getattr(exc, "errno", None) == 98:
            return "端口已被占用：可能已经有一个 live_queue 在跑，换端口或先关掉它"
        if winerror == 10013:
            return (
                "端口被系统保留（Windows 的 Hyper-V/WSL 会占用一些端口区间，"
                "这种端口即使没人监听也绑不上）：请换一个端口，"
                "可用 netsh int ipv4 show excludedportrange protocol=tcp 查看"
            )
        return f"端口绑定失败：{exc}"

    def _log(self, level: str, message: str) -> None:
        """按级别记日志。

        Args:
            level: 日志级别名。
            message: 日志正文。
        """

        if self._logger is None:
            return
        emit = getattr(self._logger, level, None)
        if callable(emit):
            emit(message)


def _first(query: dict[str, list[str]], key: str, default: Any) -> Any:
    """取查询参数里的第一个值。

    Args:
        query: ``parse_qs`` 的结果。
        key: 参数名。
        default: 缺省值。

    Returns:
        参数值或默认值。
    """

    values = query.get(key)
    if not values:
        return default
    return values[0]


def _as_int(value: Any, default: int) -> int:
    """把任意值转成 int。

    Args:
        value: 原始值。
        default: 转不了时的默认值。

    Returns:
        整数值。
    """

    try:
        return int(value)
    except (TypeError, ValueError):
        return default


__all__ = [
    "QueueHTTPServer",
    "QueueRequestHandler",
    "ServerStartError",
]
