"""live_queue HTTP 服务的单测。

用**真实 socket**（绑定到系统分配的空闲端口）跑，而不是打桩——因为这里踩过
keep-alive 相关的坑（提前返回的分支没读完请求体，会污染连接），只有真实 HTTP
才能测出来。
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from http.client import HTTPConnection
from pathlib import Path

import pytest

from live_queue.config import AppConfig
from live_queue.hub import QueueHub
from live_queue.server import (
    QueueHTTPServer,
    ServerStartError,
    _is_loopback,
)
from live_queue.store import QueueStore

WEB_DIR = Path(__file__).resolve().parents[2] / "live_queue" / "web"


@pytest.fixture
def hub(tmp_path: Path, legacy_config: AppConfig) -> Iterator[QueueHub]:
    """未启动维护线程的服务层（HTTP 测试不需要它）。"""

    instance = QueueHub(
        config=legacy_config, store=QueueStore(tmp_path / "queues.json")
    )
    yield instance


@pytest.fixture
def server(hub: QueueHub) -> Iterator[QueueHTTPServer]:
    """监听随机空闲端口的 HTTP 服务。"""

    instance = QueueHTTPServer(hub=hub, web_dir=WEB_DIR, host="127.0.0.1", port=0)
    instance.start()
    yield instance
    instance.stop()


class Client:
    """带 keep-alive 的测试客户端（``http.client`` 默认复用连接）。"""

    def __init__(self, port: int, token: str = "") -> None:
        """建立连接。

        Args:
            port: 服务端口。
            token: 写操作密钥。
        """

        self._conn = HTTPConnection("127.0.0.1", port, timeout=5)
        self._token = token

    def request(
        self, method: str, path: str, payload: dict | None = None, *, token: str | None = None
    ) -> tuple[int, dict]:
        """发一次请求。

        Args:
            method: HTTP 方法。
            path: 路径。
            payload: JSON 请求体；``None`` 表示不带 body。
            token: 覆盖密钥。

        Returns:
            ``(状态码, 解析后的 JSON)``。
        """

        body = None if payload is None else json.dumps(payload).encode("utf-8")
        headers = {"Content-Type": "application/json"}
        key = self._token if token is None else token
        if key:
            headers["X-API-Key"] = key
        self._conn.request(method, path, body=body, headers=headers)
        response = self._conn.getresponse()
        raw = response.read()
        try:
            parsed = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            parsed = {"_raw": raw.decode("utf-8", errors="replace")}
        return response.status, parsed

    def raw_request(self, method: str, path: str, body: bytes) -> tuple[int, dict]:
        """发送原始 body（用于测试非法 JSON / 超长 body）。

        Args:
            method: HTTP 方法。
            path: 路径。
            body: 原始请求体。

        Returns:
            ``(状态码, 解析后的 JSON)``。
        """

        self._conn.request(method, path, body=body, headers={"Content-Type": "application/json"})
        response = self._conn.getresponse()
        raw = response.read()
        try:
            parsed = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            parsed = {"_raw": raw.decode("utf-8", errors="replace")}
        return response.status, parsed

    def get_html(self, path: str) -> tuple[int, str]:
        """取 HTML 页面。

        Args:
            path: 路径。

        Returns:
            ``(状态码, 页面内容)``。
        """

        self._conn.request("GET", path)
        response = self._conn.getresponse()
        return response.status, response.read().decode("utf-8", errors="replace")

    def close(self) -> None:
        """关闭连接。"""

        self._conn.close()


@pytest.fixture
def client(server: QueueHTTPServer) -> Iterator[Client]:
    """指向测试服务的客户端。"""

    instance = Client(server.port)
    yield instance
    instance.close()


# ── 端口与地址 ────────────────────────────────────────


def test_server_binds_ephemeral_port(server: QueueHTTPServer) -> None:
    """port=0 时回填真实端口，URL 拼装正确。"""

    assert server.port > 0
    assert server.base_url == f"http://127.0.0.1:{server.port}"
    assert server.overlay_url.endswith("/overlay")
    assert server.panel_url.endswith("/panel")
    assert server.is_running is True


def test_server_stop(server: QueueHTTPServer) -> None:
    """停止后标记为未运行。"""

    server.stop()
    assert server.is_running is False


def test_port_conflict_raises_readable_error(hub: QueueHub, server: QueueHTTPServer) -> None:
    """端口被占用时给出可操作提示（而不是裸 OSError）。"""

    clash = QueueHTTPServer(hub=hub, web_dir=WEB_DIR, host="127.0.0.1", port=server.port)
    with pytest.raises(ServerStartError) as excinfo:
        clash.start()
    assert "端口已被占用" in str(excinfo.value)


@pytest.mark.parametrize(
    ("host", "expected"),
    [
        ("127.0.0.1", True),
        ("::1", True),
        ("localhost", True),
        ("::ffff:127.0.0.1", True),
        ("127.0.0.5", True),
        ("192.168.1.10", False),
        ("203.0.113.9", False),
        ("", False),
    ],
)
def test_is_loopback(host: str, expected: bool) -> None:
    """本机判定要覆盖 IPv6 映射形式。"""

    assert _is_loopback(host) is expected


# ── 只读接口 ──────────────────────────────────────────


def test_health_endpoint(client: Client) -> None:
    """健康检查（机器人靠它判断就绪）。"""

    status, body = client.request("GET", "/api/health")
    assert status == 200
    assert body["ok"] is True
    assert body["app"] == "live_queue"


def test_state_and_config_endpoints(client: Client, hub: QueueHub) -> None:
    """状态与展示配置。"""

    hub.join(user_id="u1", nickname="阿狸", queue="排位")

    status, state = client.request("GET", "/api/state")
    assert status == 200
    assert state["changed"] is True
    assert state["queues"][0]["title"] == "排位"
    assert state["view"]["max_rows"] == 8

    _, config = client.request("GET", "/api/config")
    assert config["overlay"]["max_rows"] == 8
    assert config["default_queue"] == "默认"


def test_state_revision_shortcut(client: Client, hub: QueueHub) -> None:
    """``rev`` 与当前版本一致时只回 changed=False。"""

    hub.join(user_id="u1", queue="排位")
    revision = hub.revision

    _, unchanged = client.request("GET", f"/api/state?rev={revision}")
    assert unchanged == {"ok": True, "changed": False, "revision": revision}

    _, changed = client.request("GET", f"/api/state?rev={revision + 1}")
    assert changed["changed"] is True


def test_state_filter_and_limit(client: Client, hub: QueueHub) -> None:
    """过滤与截断。"""

    for index in range(4):
        hub.join(user_id=f"u{index}", queue="排位")
    hub.join(user_id="x", queue="娱乐")

    _, body = client.request("GET", "/api/state?queue=%E6%8E%92%E4%BD%8D&limit=2")
    assert [item["name"] for item in body["queues"]] == ["排位"]
    assert len(body["queues"][0]["entries"]) == 2


def test_queues_endpoint(client: Client, hub: QueueHub) -> None:
    """轻量队列名列表。"""

    hub.create_queue("排位")
    hub.create_queue("娱乐")
    _, body = client.request("GET", "/api/queues")
    assert body["queues"] == ["排位", "娱乐"]


def test_whoami_loopback_can_write(client: Client) -> None:
    """没配 token 时本机可以写。"""

    _, body = client.request("POST", "/api/whoami")
    assert body["can_write"] is True
    assert body["token_required"] is False


def test_unknown_path_returns_404(client: Client) -> None:
    """未知路径返回 JSON 404。"""

    status, body = client.request("GET", "/api/nope")
    assert status == 404
    assert body["ok"] is False


# ── 页面 ──────────────────────────────────────────────


def test_overlay_and_panel_pages(client: Client) -> None:
    """叠加页与控制面板都要能取到。"""

    for path in ("/", "/overlay", "/panel"):
        status, html = client.get_html(path)
        assert status == 200
        assert "<html" in html.lower()

    _, overlay = client.get_html("/overlay")
    assert "排队" in overlay
    _, panel = client.get_html("/panel")
    assert "排队控制台" in panel


def test_missing_page_returns_readable_html(hub: QueueHub, tmp_path: Path) -> None:
    """web 目录不完整时给出可读提示，而不是 500 空白。"""

    server = QueueHTTPServer(hub=hub, web_dir=tmp_path, host="127.0.0.1", port=0)
    server.start()
    try:
        client = Client(server.port)
        try:
            status, html = client.get_html("/overlay")
            assert status == 200
            assert "页面缺失" in html
        finally:
            client.close()
    finally:
        server.stop()


# ── 写接口 ────────────────────────────────────────────


def test_join_and_leave_endpoints(client: Client, hub: QueueHub) -> None:
    """机器人的主路径：把弹幕观众加进队列 / 移出队列。"""

    status, joined = client.request(
        "POST",
        "/api/join",
        {
            "user_id": "open-1",
            "nickname": "阿狸",
            "platform": "bilibili_live",
            "queue": "排位",
            "queue_explicit": True,
            "extra": {"guard_level": 3},
            "guild": "12345",
        },
    )
    assert status == 200
    assert joined["status"] == "joined"
    assert joined["position"] == 1
    assert joined["ticket"] == 1

    entry = hub.snapshot(queue="排位")["queues"][0]["entries"][0]
    assert entry["nickname"] == "阿狸"
    assert hub.queues_of("open-1") == ["排位"]

    _, left = client.request("POST", "/api/leave", {"user_id": "open-1"})
    assert left["ok"] is True
    assert left["queues"] == ["排位"]


def test_queue_explicit_false_falls_back_to_default(client: Client) -> None:
    """不明确的队名落到默认队（解析发生在 app 侧）。"""

    _, body = client.request(
        "POST",
        "/api/join",
        {"user_id": "u1", "queue": "一下", "queue_explicit": False},
    )
    assert body["queue"] == "默认"


def test_write_endpoints_cover_management(client: Client, hub: QueueHub) -> None:
    """管理类写接口全量走一遍。"""

    hub.join(user_id="u1", nickname="阿狸", queue="排位")
    hub.join(user_id="u2", nickname="小满", queue="排位")

    _, called = client.request("POST", "/api/call_next", {"queue": "排位"})
    assert called["ok"] is True and called["entry"]["nickname"] == "阿狸"

    _, requeued = client.request("POST", "/api/requeue", {"queue": "排位"})
    assert requeued["ok"] is True

    _, paused = client.request("POST", "/api/pause", {"queue": "排位", "paused": True})
    assert paused["ok"] is True

    _, resumed = client.request("POST", "/api/pause", {"queue": "排位", "paused": False})
    assert resumed["ok"] is True

    _, capacity = client.request("POST", "/api/capacity", {"queue": "排位", "capacity": 9})
    assert capacity["ok"] is True

    _, added = client.request("POST", "/api/add", {"queue": "排位", "nickname": "手动"})
    assert added["ok"] is True

    _, moved = client.request("POST", "/api/move_front", {"queue": "排位", "user_id": "u2"})
    assert moved["ok"] is True

    _, removed = client.request("POST", "/api/remove", {"queue": "排位", "user_id": "u2"})
    assert removed["ok"] is True

    _, created = client.request("POST", "/api/create", {"queue": "大厅"})
    assert created["ok"] is True

    _, cleared = client.request("POST", "/api/clear", {"queue": "大厅"})
    assert cleared["ok"] is True

    _, deleted = client.request("POST", "/api/delete", {"queue": "大厅"})
    assert deleted["ok"] is True

    _, flushed = client.request("POST", "/api/flush", {})
    assert flushed["ok"] is True


def test_invalid_name_returns_ok_false(client: Client) -> None:
    """非法队名返回 ok=False 而不是 500。"""

    status, body = client.request("POST", "/api/create", {"queue": "带 空格"})
    assert status == 200
    assert body["ok"] is False


def test_bad_json_body_returns_400(client: Client) -> None:
    """非法 JSON → 400。"""

    status, body = client.raw_request("POST", "/api/join", b"{not json")
    assert status == 400
    assert body["ok"] is False


def test_non_object_body_returns_400(client: Client) -> None:
    """JSON 不是对象 → 400。"""

    status, _ = client.raw_request("POST", "/api/join", b"[1,2,3]")
    assert status == 400


def test_oversized_body_returns_413(client: Client) -> None:
    """略超上限的 body：排空后回 413，客户端能正常读到响应。

    注意 ``1 << 20 + 10`` 会被解析成 ``1 << (20 + 10)``（加号优先级更高），
    那是 1 GiB，走的是「直接断连」分支——这里要的是「排空后回 413」。
    """

    status, body = client.raw_request("POST", "/api/join", b"x" * ((1 << 20) + 10))
    assert status == 413
    assert body["ok"] is False


def test_connection_survives_oversized_body(client: Client) -> None:
    """413 之后同一条 keep-alive 连接仍能继续用（body 已被排空）。"""

    client.raw_request("POST", "/api/join", b"x" * ((1 << 20) + 10))
    status, body = client.request("GET", "/api/health")
    assert status == 200
    assert body["ok"] is True


def test_empty_body_is_accepted(client: Client) -> None:
    """没有 body 的写请求按空参数处理（队列留空 = 默认队）。"""

    status, body = client.request("POST", "/api/call_next")
    assert status == 200
    assert body["ok"] is False  # 默认队不存在/为空


# ── 鉴权 ──────────────────────────────────────────────


@pytest.fixture
def token_server(tmp_path: Path, legacy_config: AppConfig) -> Iterator[QueueHTTPServer]:
    """配了 token 的服务。"""

    config = legacy_config
    config.server.token = "s3cret"
    hub = QueueHub(config=config, store=QueueStore(tmp_path / "queues.json"))
    server = QueueHTTPServer(hub=hub, web_dir=WEB_DIR, host="127.0.0.1", port=0)
    server.start()
    yield server
    server.stop()


def test_token_is_required_for_writes(token_server: QueueHTTPServer) -> None:
    """配了 token 后，缺 key 或 key 不对都拒绝。"""

    client = Client(token_server.port)
    try:
        status, body = client.request("POST", "/api/create", {"queue": "排位"})
        assert status == 403
        assert body["ok"] is False

        status, _ = client.request("POST", "/api/create", {"queue": "排位"}, token="nope")
        assert status == 403

        status, body = client.request(
            "POST", "/api/create", {"queue": "排位"}, token="s3cret"
        )
        assert status == 200
        assert body["ok"] is True
    finally:
        client.close()


def test_reads_stay_open_with_token(token_server: QueueHTTPServer) -> None:
    """只读接口不受 token 限制（OBS 浏览器源没法加请求头）。"""

    client = Client(token_server.port)
    try:
        assert client.request("GET", "/api/state")[0] == 200
        assert client.get_html("/overlay")[0] == 200
        _, who = client.request("POST", "/api/whoami")
        assert who["can_write"] is False
        assert who["token_required"] is True
    finally:
        client.close()


def test_denied_write_does_not_corrupt_connection(token_server: QueueHTTPServer) -> None:
    """403 分支也要把请求体读干净，否则下一条请求会被残留字节弄坏。"""

    client = Client(token_server.port)
    try:
        # 带 body 的 403 请求
        status, _ = client.request("POST", "/api/create", {"queue": "排位", "带": "中文"})
        assert status == 403

        # 同一条 keep-alive 连接上的后续请求必须正常
        status, body = client.request("GET", "/api/health")
        assert status == 200
        assert body["ok"] is True

        status, body = client.request(
            "POST", "/api/create", {"queue": "排位"}, token="s3cret"
        )
        assert status == 200 and body["ok"] is True
    finally:
        client.close()


def test_whoami_with_body_keeps_connection_alive(client: Client) -> None:
    """回归：带 body 的 whoami 之后，同一条连接上的 GET 必须正常。

    曾经的 bug 是 whoami 提前返回、没读请求体，导致下一条请求解析成 ``{}GET`` → 501。
    """

    client.request("POST", "/api/whoami", {})
    status, html = client.get_html("/overlay")
    assert status == 200
    assert "排队" in html

    status, body = client.request("GET", "/api/state")
    assert status == 200 and body["ok"] is True


def test_options_preflight(client: Client) -> None:
    """CORS 预检返回 204 与允许头。"""

    status, _ = client.request("OPTIONS", "/api/join")
    assert status == 204
