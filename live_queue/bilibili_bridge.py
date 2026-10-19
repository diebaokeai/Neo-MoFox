"""live_queue 的独立 B 站弹幕桥：不跑 MoFox 也能让弹幕排队。

链路::

    B 站直播开放平台长连 ──► bilibili_bridge.py ──HTTP──► live_queue /api/join
                                                      └──► 排队姬窗口 / OBS 叠加页

与 bot 同时在线时不会重复排队的保证（三层）：

1. **平台单会话锁**：B 站开放平台同一房间同时只允许一个长连会话，第二个 start_app
   会拿到 7002「房间重复游戏」。本桥遇到 7002 会退避重试并写明原因，不与 bot 抢会话。
2. **应用侧幂等**：每次调用都带 event_id（平台 msg_id），排队姬在
   queue.event_dedupe_seconds 窗口内只处理一次，重复事件返回 duplicate_event。
3. **身份统一**：本桥与 bot 插件都用 open_id 当 user_id，配合
   duplicate_policy=reject，同一个人在同一队列里永远只占一个位置。

用法::

    python live_queue/bilibili_bridge.py --selftest     # 只自检，不连网
    python live_queue/bilibili_bridge.py                # 正式跑（需要凭证）
    python live_queue/bilibili_bridge.py --dry-run      # 连上但只打印，不调排队姬

凭证写在 live_queue/bridge_config.json（首次运行会自动生成模板）。
"""

from __future__ import annotations

import argparse
import asyncio
import dataclasses
import json
import logging
import signal
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

APP_DIR = Path(__file__).resolve().parent
"""应用目录（配置、数据、页面都在它下面）。"""

# 让 python live_queue/bilibili_bridge.py 与 python -m live_queue.bilibili_bridge 都能用
if str(APP_DIR.parent) not in sys.path:
    sys.path.insert(0, str(APP_DIR.parent))

try:  # pragma: no cover - 依赖缺失时给出可读提示
    import httpx
except ImportError:  # pragma: no cover
    httpx = None  # type: ignore[assignment]

from live_queue.bilibili import (  # noqa: E402
    BilibiliApi,
    BilibiliApiError,
    BilibiliClient,
    BilibiliClientError,
    Intent,
    KeywordMatcher,
)

DEFAULT_CONFIG_PATH = APP_DIR / "bridge_config.json"
"""默认配置文件路径。"""

DEFAULT_SESSION_PATH = APP_DIR / "data" / "bridge_session.json"
"""记录最后一次 game_id：重启时先释放它，否则会撞 7002（遗留锁约 30 分钟）。"""

DANMAKU_CMD = "LIVE_OPEN_PLATFORM_DM"
"""弹幕业务包的 cmd。"""

IGNORED_CMDS = frozenset(
    {
        "LIVE_OPEN_PLATFORM_LIKE",
        "LIVE_OPEN_PLATFORM_SEND_GIFT",
        "LIVE_OPEN_PLATFORM_SUPER_CHAT",
        "LIVE_OPEN_PLATFORM_GUARD",
    }
)
"""本桥不处理的业务包（只记 debug 日志）。"""

DUPLICATE_ROOM_CODE = 7002
"""B 站「房间重复游戏」错误码：说明已有会话（通常是 bot）在占用该房间。"""

logger = logging.getLogger("live_queue.bilibili_bridge")


# ── 配置 ──────────────────────────────────────────────


@dataclass
class BilibiliSection:
    """B 站开放平台凭证（与 bot 的 bilibili_live_adapter 可以是同一套）。"""

    host: str = "https://live-open.biliapi.com"
    access_key_id: str = ""
    access_key_secret: str = ""
    app_id: int = 0
    id_code: str = ""

    def missing(self) -> list[str]:
        """返回没填的必填项。"""

        gaps: list[str] = []
        if not str(self.access_key_id or "").strip():
            gaps.append("access_key_id")
        if not str(self.access_key_secret or "").strip():
            gaps.append("access_key_secret")
        if int(self.app_id or 0) <= 0:
            gaps.append("app_id")
        if not str(self.id_code or "").strip():
            gaps.append("id_code")
        return gaps


@dataclass
class ConnectionSection:
    """长连与重连参数。"""

    request_timeout: float = 15.0
    heartbeat_ws_interval: float = 20.0
    heartbeat_app_interval: float = 20.0
    auth_timeout: float = 10.0
    reconnect_min_seconds: float = 3.0
    reconnect_max_seconds: float = 60.0
    standby_retry_seconds: float = 30.0
    """撞到 7002（房间被 bot 占用）时的退避秒数。"""


@dataclass
class AppSection:
    """排队姬（live_queue）的连接参数。"""

    base_url: str = "http://127.0.0.1:17650"
    token: str = ""
    timeout: float = 5.0


@dataclass
class KeywordsSection:
    """弹幕关键词（与 bot 插件默认值一致）。"""

    join: list[str] = field(default_factory=lambda: ["排队", "排个队", "上车", "报名"])
    leave: list[str] = field(default_factory=lambda: ["退队", "下车", "取消排队", "退出排队"])
    status: list[str] = field(
        default_factory=lambda: ["排队情况", "排队查询", "查询排队", "队列状态"]
    )
    ignore_case: bool = True


@dataclass
class BehaviorSection:
    """行为开关。"""

    enabled: bool = True
    log_danmaku: bool = False
    handle_status: bool = True
    handle_admin: bool = False
    """房管命令（下一个 / 清空 / 暂停等）默认不处理：那是 bot 与排队姬窗口的职责。"""


@dataclass
class BridgeConfig:
    """桥的完整配置。"""

    bilibili: BilibiliSection = field(default_factory=BilibiliSection)
    connection: ConnectionSection = field(default_factory=ConnectionSection)
    app: AppSection = field(default_factory=AppSection)
    keywords: KeywordsSection = field(default_factory=KeywordsSection)
    behavior: BehaviorSection = field(default_factory=BehaviorSection)

    def to_dict(self) -> dict[str, Any]:
        """转成可落盘的 dict。"""

        return asdict(self)

    @classmethod
    def from_dict(cls, data: Any) -> "BridgeConfig":
        """用 dict 覆盖默认值；结构不对的段落回退默认。"""

        if not isinstance(data, dict):
            return cls()
        return cls(
            bilibili=_section(BilibiliSection, data.get("bilibili")),
            connection=_section(ConnectionSection, data.get("connection")),
            app=_section(AppSection, data.get("app")),
            keywords=_section(KeywordsSection, data.get("keywords")),
            behavior=_section(BehaviorSection, data.get("behavior")),
        )


def _section(cls: type, raw: Any) -> Any:
    """按字段名过滤后构造配置段；非法输入退回默认。"""

    if not isinstance(raw, dict):
        return cls()
    allowed = {item.name for item in dataclasses.fields(cls)}
    kwargs = {key: value for key, value in raw.items() if key in allowed}
    try:
        return cls(**kwargs)
    except TypeError:
        return cls()


def load_config(path: Path) -> BridgeConfig:
    """读取配置；不存在时生成模板。

    Args:
        path: 配置文件路径。

    Returns:
        配置对象。
    """

    if not path.exists():
        config = BridgeConfig()
        save_config(path, config)
        logger.warning(f"已生成配置模板：{path}，请填写 B 站凭证后重跑")
        return config
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        logger.error(f"配置读取失败，按默认值继续：{exc}")
        return BridgeConfig()
    return BridgeConfig.from_dict(data)


def save_config(path: Path, config: BridgeConfig) -> None:
    """把配置写回文件（自动建目录）。

    Args:
        path: 配置文件路径。
        config: 配置对象。
    """

    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(config.to_dict(), ensure_ascii=False, indent=2) + "\n"
    path.write_text(text, encoding="utf-8")


# ── 排队姬 HTTP 客户端 ────────────────────────────────


class QueueAppClient:
    """排队姬的 HTTP 客户端（只用到 join / leave / state）。"""

    def __init__(self, *, base_url: str, token: str = "", timeout: float = 5.0) -> None:
        """记录地址与密钥。

        Args:
            base_url: 例如 http://127.0.0.1:17650。
            token: 排队姬 server.token；留空表示本机直连。
            timeout: 单次请求超时（秒）。
        """

        if httpx is None:  # pragma: no cover - 由 main 提前拦截
            raise RuntimeError("缺少依赖 httpx：pip install httpx")
        self._base = str(base_url or "").rstrip("/")
        self._timeout = max(0.5, float(timeout))
        headers = {"X-API-Key": token} if token else {}
        self._client = httpx.AsyncClient(
            base_url=self._base, timeout=self._timeout, headers=headers
        )

    async def aclose(self) -> None:
        """关闭底层连接池。"""

        await self._client.aclose()

    async def join(self, **payload: Any) -> dict[str, Any]:
        """入队。"""

        return await self._post("/api/join", payload)

    async def leave(self, **payload: Any) -> dict[str, Any]:
        """退队。"""

        return await self._post("/api/leave", payload)

    async def state(self, *, queue: str = "", limit: int = 0) -> dict[str, Any]:
        """取队列快照。"""

        resp = await self._client.get(
            "/api/state", params={"queue": queue, "limit": limit or None}
        )
        return self._decode(resp)

    async def _post(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        """POST 一个 JSON 并解析。"""

        resp = await self._client.post(path, json=payload)
        return self._decode(resp)

    @staticmethod
    def _decode(resp: Any) -> dict[str, Any]:
        """把响应解成 dict（失败也给一个 ok=False 的结构，调用方不用 try）。"""

        try:
            data = resp.json()
        except Exception:
            return {
                "ok": False,
                "status": "bad_response",
                "message": f"排队姬返回了非 JSON（HTTP {getattr(resp, 'status_code', '?')}）",
            }
        if not isinstance(data, dict):
            return {"ok": False, "status": "bad_response", "message": "排队姬返回结构异常"}
        return data


# ── 弹幕 → 动作（纯函数，可单测）───────────────────────


@dataclass
class DanmakuAction:
    """一条弹幕解析出来的待执行动作。"""

    kind: str
    """join / leave / status。"""

    user_id: str
    nickname: str
    queue: str
    queue_explicit: bool
    event_id: str
    room_id: str
    guard_level: int
    fans_medal_level: int
    bilibili_uid: Any
    text: str

    def join_payload(self) -> dict[str, Any]:
        """转成 /api/join 请求体。"""

        return {
            "user_id": self.user_id,
            "nickname": self.nickname,
            "platform": "bilibili_live",
            "queue": self.queue,
            "queue_explicit": self.queue_explicit,
            "guild": self.room_id,
            "event_id": self.event_id,
            "extra": {
                "guard_level": self.guard_level,
                "fans_medal_level": self.fans_medal_level,
                "bilibili_uid": self.bilibili_uid,
                "source": "bilibili_bridge",
            },
        }

    def leave_payload(self) -> dict[str, Any]:
        """转成 /api/leave 请求体。"""

        return {
            "user_id": self.user_id,
            "queue": self.queue,
            "queue_explicit": self.queue_explicit,
            "event_id": self.event_id,
        }


def extract_danmaku_data(payload: Any) -> dict[str, Any] | None:
    """从 op=5 业务包里取出弹幕 data。

    Args:
        payload: 长连回调的原始 dict。

    Returns:
        弹幕 data；不是弹幕时返回 None。
    """

    if not isinstance(payload, dict):
        return None
    if str(payload.get("cmd") or "") != DANMAKU_CMD:
        return None
    data = payload.get("data")
    return data if isinstance(data, dict) else None


def _safe_int(value: Any) -> int:
    """安全转 int。"""

    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def build_action(data: dict[str, Any], matcher: KeywordMatcher) -> DanmakuAction | None:
    """把一条弹幕数据解析成动作。

    房管命令（下一个 / 清空 / 暂停…）本桥一律不处理：那是 bot 与排队姬窗口的职责，
    桥只做观众侧的「排队 / 退队 / 查队列」，避免重复叫号。

    Args:
        data: 弹幕 data。
        matcher: 关键词匹配器。

    Returns:
        动作；没命中队列关键词时返回 None。
    """

    text = str(data.get("msg") or "").strip()
    if not text:
        return None

    parsed = matcher.parse(text)
    if parsed.intent is Intent.JOIN:
        kind = "join"
    elif parsed.intent is Intent.LEAVE:
        kind = "leave"
    elif parsed.intent is Intent.STATUS:
        kind = "status"
    else:
        return None

    return DanmakuAction(
        kind=kind,
        user_id=str(data.get("open_id") or data.get("uid") or "anon"),
        nickname=str(data.get("uname") or ""),
        queue=str(parsed.queue or ""),
        queue_explicit=bool(parsed.queue_explicit),
        event_id=str(data.get("msg_id") or ""),
        room_id=str(data.get("room_id") or ""),
        guard_level=_safe_int(data.get("guard_level")),
        fans_medal_level=_safe_int(data.get("fans_medal_level")),
        bilibili_uid=data.get("uid"),
        text=text,
    )


# ── 桥本体 ────────────────────────────────────────────


class BilibiliBridge:
    """一条长连会话 + 弹幕落地逻辑 + 外层重连。"""

    def __init__(
        self,
        config: BridgeConfig,
        *,
        app: QueueAppClient,
        dry_run: bool = False,
        session_path: Path = DEFAULT_SESSION_PATH,
    ) -> None:
        """记录配置与依赖。

        Args:
            config: 桥配置。
            app: 排队姬客户端。
            dry_run: 只打印不真调排队姬。
            session_path: game_id 记录文件。
        """

        self._config = config
        self._app = app
        self._dry_run = bool(dry_run)
        self._session_path = session_path
        self._matcher = KeywordMatcher(
            join=config.keywords.join,
            leave=config.keywords.leave,
            status=config.keywords.status,
            ignore_case=config.keywords.ignore_case,
        )
        self._stopping = asyncio.Event()

    def request_stop(self) -> None:
        """请求退出（信号处理用）。"""

        self._stopping.set()

    @property
    def matcher(self) -> KeywordMatcher:
        """当前生效的关键词匹配器。"""

        return self._matcher

    # ── 弹幕回调 ──────────────────────────────────────

    async def on_event(self, payload: dict[str, Any]) -> None:
        """长连业务包回调。"""

        cmd = str(payload.get("cmd") or "") if isinstance(payload, dict) else ""
        data = extract_danmaku_data(payload)
        if data is None:
            if cmd and cmd not in IGNORED_CMDS:
                logger.debug(f"忽略业务包 cmd={cmd}")
            return

        if self._config.behavior.log_danmaku:
            logger.info(f"弹幕 {data.get('uname')}: {data.get('msg')}")

        action = build_action(data, self._matcher)
        if action is None:
            return
        if action.kind == "status" and not self._config.behavior.handle_status:
            return
        await self._dispatch(action)

    async def _dispatch(self, action: DanmakuAction) -> None:
        """把动作落到排队姬。"""

        if action.kind == "join":
            result = await self._send("join", action.join_payload())
            self._log_result(action, result)
        elif action.kind == "leave":
            result = await self._send("leave", action.leave_payload())
            self._log_result(action, result)
        elif action.kind == "status":
            result = await self._send("state", {"queue": action.queue})
            rows = result.get("queues")
            items = [row for row in rows if isinstance(row, dict)] if isinstance(rows, list) else []
            summary = "、".join(
                f"{row.get('title') or row.get('name') or '?'}({row.get('waiting', 0)}人)"
                for row in items
            )
            logger.info(f"[状态] {action.nickname} 查询队列：{summary or '还没有队列'}")

    async def _send(self, kind: str, payload: dict[str, Any]) -> dict[str, Any]:
        """真正发请求（dry-run 时只打印）。"""

        if self._dry_run:
            logger.info(f"[dry-run] {kind} {json.dumps(payload, ensure_ascii=False)}")
            return {"ok": True, "status": "dry_run", "message": "dry-run"}
        if kind == "join":
            return await self._app.join(**payload)
        if kind == "leave":
            return await self._app.leave(**payload)
        return await self._app.state(queue=str(payload.get("queue") or ""))

    @staticmethod
    def _log_result(action: DanmakuAction, result: dict[str, Any]) -> None:
        """按结果状态打日志（成功 info，重复 debug，失败 warning）。"""

        status = str(result.get("status") or "")
        message = str(result.get("message") or "")
        if status in {"joined", "moved", "left"}:
            logger.info(f"[排队] {action.nickname} {message or status}")
        elif status in {"already_in", "duplicate_event", "not_queued"}:
            logger.debug(f"[排队] {action.nickname} 忽略（{status}）")
        else:
            logger.warning(f"[排队] {action.nickname} 未成功：{status} {message}".rstrip())

    # ── 会话循环 ──────────────────────────────────────

    async def run(self) -> None:
        """外层重连循环：start_app → 长连 → 断了就退避重连。"""

        bili = self._config.bilibili
        conn = self._config.connection
        backoff = max(1.0, float(conn.reconnect_min_seconds))

        api = BilibiliApi(
            host=bili.host,
            access_key_id=bili.access_key_id,
            access_key_secret=bili.access_key_secret,
            app_id=bili.app_id,
            id_code=bili.id_code,
            timeout=float(conn.request_timeout),
        )
        await self._cleanup_stale_session(api)

        try:
            while not self._stopping.is_set():
                try:
                    start_resp = await api.start_app()
                except BilibiliApiError as exc:
                    if exc.code == DUPLICATE_ROOM_CODE:
                        logger.warning(
                            "房间已被另一个会话占用（code=7002，通常是 bot 的 bilibili_live_adapter）："
                            f"桥接待命 {float(conn.standby_retry_seconds):.0f} 秒后重试。"
                            "想用桥接请先停掉 bot 的那个适配器。"
                        )
                        await self._sleep(float(conn.standby_retry_seconds))
                        continue
                    logger.error(f"B 站接口错误：{exc}；{backoff:.0f} 秒后重连")
                    await self._sleep(backoff)
                    backoff = min(float(conn.reconnect_max_seconds), backoff * 2)
                    continue
                except Exception as exc:
                    logger.error(f"建立会话失败：{exc}", exc_info=True)
                    await self._sleep(backoff)
                    backoff = min(float(conn.reconnect_max_seconds), backoff * 2)
                    continue

                self._save_game_id(start_resp.game_id)
                logger.info(
                    f"长连会话已建立：房间 {start_resp.anchor_room_id}"
                    f"（{start_resp.anchor_uname}）game_id={start_resp.game_id}"
                )

                client = BilibiliClient(
                    api=api,
                    on_event=self.on_event,
                    heartbeat_ws_interval=float(conn.heartbeat_ws_interval),
                    heartbeat_app_interval=float(conn.heartbeat_app_interval),
                    auth_timeout=float(conn.auth_timeout),
                )
                try:
                    await client.start(start_resp)
                    backoff = max(1.0, float(conn.reconnect_min_seconds))
                    await self._wait_closed(client)
                except BilibiliClientError as exc:
                    logger.warning(f"长连异常：{exc}")
                    await client.stop()
                except Exception as exc:
                    logger.error(f"会话异常：{exc}", exc_info=True)
                    await client.stop()

                await self._end_session(api)
                if self._stopping.is_set():
                    break
                logger.warning(f"长连已断开；{backoff:.0f} 秒后重连")
                await self._sleep(backoff)
                backoff = min(float(conn.reconnect_max_seconds), backoff * 2)
        finally:
            await self._end_session(api)
            await api.aclose()

    async def _wait_closed(self, client: BilibiliClient) -> None:
        """等长连结束，或者外部要求停止。"""

        closed = asyncio.create_task(client.wait_closed())
        stopped = asyncio.create_task(self._stopping.wait())
        try:
            await asyncio.wait({closed, stopped}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for task in (closed, stopped):
                if not task.done():
                    task.cancel()
            await asyncio.gather(closed, stopped, return_exceptions=True)
            await client.stop()

    async def _sleep(self, seconds: float) -> None:
        """可被 request_stop 打断的等待。"""

        try:
            await asyncio.wait_for(self._stopping.wait(), timeout=max(0.1, float(seconds)))
        except asyncio.TimeoutError:
            pass

    # ── game_id 持久化（防 7002 遗留锁）───────────────

    def _load_game_id(self) -> str:
        """读上次记录的 game_id。"""

        try:
            data = json.loads(self._session_path.read_text(encoding="utf-8"))
        except Exception:
            return ""
        return str(data.get("game_id") or "") if isinstance(data, dict) else ""

    def _save_game_id(self, game_id: str) -> None:
        """记录 game_id（失败不影响主流程）。"""

        try:
            self._session_path.parent.mkdir(parents=True, exist_ok=True)
            text = json.dumps({"game_id": game_id}, ensure_ascii=False, indent=2) + "\n"
            self._session_path.write_text(text, encoding="utf-8")
        except Exception as exc:
            logger.debug(f"记录 game_id 失败：{exc}")

    async def _cleanup_stale_session(self, api: BilibiliApi) -> None:
        """启动时先释放上次遗留的会话，否则半小时内新会话都会被 7002 拒绝。"""

        stale = self._load_game_id()
        if not stale:
            return
        try:
            await api.end_app(stale)
            logger.info(f"已释放上次遗留的会话 game_id={stale}")
        except Exception as exc:
            logger.debug(f"释放遗留会话失败（通常已过期）：{exc}")
        self._save_game_id("")

    async def _end_session(self, api: BilibiliApi) -> None:
        """结束当前会话（幂等，失败只记日志）。"""

        game_id = self._load_game_id()
        if not game_id:
            return
        try:
            await api.end_app(game_id)
        except Exception as exc:
            logger.debug(f"结束会话失败：{exc}")
        self._save_game_id("")


# ── 命令行 ────────────────────────────────────────────


def build_parser() -> argparse.ArgumentParser:
    """构造命令行解析器。"""

    parser = argparse.ArgumentParser(
        prog="bilibili_bridge",
        description="独立 B 站弹幕桥：把直播弹幕转成排队姬的入队 / 退队请求（不需要 MoFox）",
    )
    parser.add_argument("--config", default=str(DEFAULT_CONFIG_PATH), help="配置文件路径")
    parser.add_argument("--app-url", default="", help="排队姬地址（覆盖配置）")
    parser.add_argument("--app-token", default="", help="排队姬 server.token（覆盖配置）")
    parser.add_argument("--dry-run", action="store_true", help="连上但只打印，不真的调排队姬")
    parser.add_argument("--selftest", action="store_true", help="不连网，只自检配置与关键词")
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="日志级别",
    )
    return parser


def _selftest(config: BridgeConfig, config_path: Path) -> int:
    """不连网的自检：配置 + 关键词 + 动作构造。"""

    matcher = KeywordMatcher(
        join=config.keywords.join,
        leave=config.keywords.leave,
        status=config.keywords.status,
        ignore_case=config.keywords.ignore_case,
    )
    logger.info(f"配置文件：{config_path}")
    logger.info(
        f"排队姬：{config.app.base_url}"
        f"（{'带 token' if config.app.token else '本机直连'}）"
    )
    gaps = config.bilibili.missing()
    logger.info(f"B 站凭证：{'缺少 ' + '、'.join(gaps) if gaps else '已填写'}")

    samples = [
        ("排队 王棋", "阿狸", "open-1", "m1"),
        ("排队王棋", "小满", "open-2", "m2"),
        ("排队", "小满", "open-2", "m3"),
        ("退队", "小满", "open-2", "m4"),
        ("排队情况", "小满", "open-2", "m5"),
        ("今天天气不错", "路人", "open-9", "m6"),
    ]
    for text, uname, open_id, msg_id in samples:
        payload = {
            "cmd": DANMAKU_CMD,
            "data": {
                "msg": text,
                "uname": uname,
                "open_id": open_id,
                "msg_id": msg_id,
                "room_id": 12345,
                "guard_level": 0,
            },
        }
        data = extract_danmaku_data(payload)
        action = build_action(data or {}, matcher)
        if action is None:
            logger.info(f"  {text} -> 放行（不是队列指令）")
        else:
            logger.info(
                f"  {text} -> {action.kind} queue={action.queue or '(空)'}"
                f" explicit={action.queue_explicit} user={action.user_id} event={action.event_id}"
            )
    logger.info("自检完成：没有发起任何网络连接。")
    return 0


async def _run(config: BridgeConfig, *, dry_run: bool) -> int:
    """建好客户端并跑桥。"""

    app = QueueAppClient(
        base_url=config.app.base_url, token=config.app.token, timeout=config.app.timeout
    )
    bridge = BilibiliBridge(config, app=app, dry_run=dry_run)
    _install_signal_handlers(bridge)
    try:
        await bridge.run()
    finally:
        await app.aclose()
    return 0


def _install_signal_handlers(bridge: BilibiliBridge) -> None:
    """把 Ctrl+C / SIGTERM 变成优雅停止。"""

    def _handler(*_args: Any) -> None:
        logger.info("收到退出信号，正在结束会话…")
        bridge.request_stop()

    for name in ("SIGINT", "SIGTERM"):
        sig = getattr(signal, name, None)
        if sig is None:
            continue
        try:
            signal.signal(sig, _handler)
        except Exception:
            pass


def _force_utf8_streams() -> None:
    """尽量把标准输出切成 UTF-8。

    Windows 上双击运行时 Python 会走控制台 API，中文本来就正常；但把日志重定向到
    文件 / 管道时会退回本地代码页（GBK），中文就花了。这里统一切成 UTF-8。
    """

    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
        except Exception:
            pass


def main(argv: list[str] | None = None) -> int:
    """命令行入口。"""

    _force_utf8_streams()
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=getattr(logging, str(args.log_level).upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s | %(message)s",
        datefmt="%H:%M:%S",
    )

    config_path = Path(args.config)
    config = load_config(config_path)
    if args.app_url:
        config.app.base_url = str(args.app_url)
    if args.app_token:
        config.app.token = str(args.app_token)

    if args.selftest:
        return _selftest(config, config_path)

    if httpx is None:
        logger.error("缺少依赖 httpx：pip install httpx（访问 B 站接口与排队姬都要它）")
        return 2

    gaps = config.bilibili.missing()
    if gaps:
        logger.error(f"B 站凭证未填写：{'、'.join(gaps)}。请编辑 {config_path} 后重跑。")
        return 2

    if not config.behavior.enabled:
        logger.error("配置里 behavior.enabled=false，桥已停用。")
        return 2

    logger.info(f"启动独立弹幕桥：排队姬 {config.app.base_url}，凭证 {config_path}")
    if args.dry_run:
        logger.warning("dry-run 模式：只打印解析结果，不会真的调排队姬")
    try:
        return asyncio.run(_run(config, dry_run=bool(args.dry_run)))
    except KeyboardInterrupt:  # pragma: no cover - 交互式中断
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
