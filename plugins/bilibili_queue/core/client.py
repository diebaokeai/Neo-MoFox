"""bilibili_queue 访问独立应用（``live_queue``）的 HTTP 客户端。

插件不再自己持有队列状态：所有读写都通过这个客户端打到 ``live_queue`` 的 ``/api/*``。
好处是**队列状态只有一个数据源**，不会出现「bot 里一份、独立 app 里一份」的双写
不一致；代价是 app 没在跑时排队不可用（由 :mod:`plugins.bilibili_queue.core.supervisor`
负责自动拉起）。

两个刻意的小决定：

- **每次请求新建 httpx 客户端**：调用频率就是弹幕频率（每秒几次），开销可忽略，
  换来的好处是不用管连接池的生命周期，也不会因为事件循环重建而拿到失效的客户端。
- **``trust_env=False``**：否则系统/环境变量里的 HTTP 代理会把 ``127.0.0.1`` 的请求
  也劫持走，排查起来非常痛苦。
"""

from __future__ import annotations

from typing import Any

import httpx

from src.app.plugin_system.api import log_api

logger = log_api.get_logger("bilibili_queue.client")

DEFAULT_TIMEOUT = 5.0
"""单次请求超时（秒）。本地调用，5 秒已经很宽裕。"""


class QueueAppError(RuntimeError):
    """调用独立 app 失败。

    Attributes:
        kind: 错误类别，便于上层决定怎么提示（``offline`` / ``http`` / ``bad_response``）。
        status: HTTP 状态码（如果拿得到）。
    """

    def __init__(self, message: str, *, kind: str = "error", status: int | None = None) -> None:
        """构造错误。

        Args:
            message: 可读的错误描述。
            kind: 错误类别。
            status: HTTP 状态码。
        """

        super().__init__(message)
        self.kind = kind
        self.status = status


class QueueAppClient:
    """独立 app 的 HTTP 客户端。"""

    def __init__(
        self,
        *,
        base_url: str,
        token: str = "",
        timeout: float = DEFAULT_TIMEOUT,
    ) -> None:
        """记录连接参数。

        Args:
            base_url: 形如 ``http://127.0.0.1:17650``。
            token: 写操作密钥；app 未配置时留空。
            timeout: 单次请求超时秒数。
        """

        self._base_url = base_url.rstrip("/")
        self._token = (token or "").strip()
        self._timeout = max(0.5, float(timeout))

    @property
    def base_url(self) -> str:
        """接口根地址。"""

        return self._base_url

    # ── 底层请求 ──────────────────────────────────────

    async def request(
        self, method: str, path: str, payload: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """发一次请求并返回 JSON。

        Args:
            method: ``GET`` / ``POST``。
            path: 以 ``/`` 开头的路径。
            payload: 请求体。

        Returns:
            解析后的 JSON dict。

        Raises:
            QueueAppError: 连不上、超时、非 2xx 或响应不是 JSON 对象。
        """

        url = f"{self._base_url}{path}"
        headers: dict[str, str] = {"Accept": "application/json"}
        if self._token:
            headers["X-API-Key"] = self._token

        try:
            async with httpx.AsyncClient(
                timeout=self._timeout, trust_env=False
            ) as client:
                if method.upper() == "GET":
                    response = await client.get(url, headers=headers)
                else:
                    response = await client.post(url, json=payload or {}, headers=headers)
        except httpx.TimeoutException as exc:
            raise QueueAppError(
                f"独立 app 响应超时（{self._base_url}）", kind="offline"
            ) from exc
        except httpx.HTTPError as exc:
            raise QueueAppError(
                f"连不上独立 app（{self._base_url}）：{exc}", kind="offline"
            ) from exc

        if response.status_code >= 400:
            raise QueueAppError(self._describe_status(response), kind="http", status=response.status_code)

        try:
            data = response.json()
        except ValueError as exc:
            raise QueueAppError(
                f"独立 app 返回的不是 JSON：{response.text[:120]}", kind="bad_response"
            ) from exc

        if not isinstance(data, dict):
            raise QueueAppError(
                f"独立 app 返回结构异常：{type(data).__name__}", kind="bad_response"
            )
        return data

    @staticmethod
    def _describe_status(response: httpx.Response) -> str:
        """把 HTTP 错误响应翻译成可读文案。

        Args:
            response: httpx 响应。

        Returns:
            错误描述。
        """

        detail = ""
        try:
            body = response.json()
            if isinstance(body, dict):
                detail = str(body.get("message") or body.get("detail") or "")
        except ValueError:
            detail = ""

        if response.status_code == 403:
            return f"独立 app 拒绝了写操作（403）：{detail or '管理密钥不匹配，检查 app.token 与插件 app.token'}"
        return f"独立 app 返回 HTTP {response.status_code}：{detail or response.text[:120]}"

    async def get(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        """发 GET 请求。

        Args:
            path: 路径。
            params: 查询参数（会被拼到 URL 上）。

        Returns:
            响应 dict。
        """

        if params:
            from urllib.parse import urlencode

            cleaned = {key: value for key, value in params.items() if value not in (None, "")}
            if cleaned:
                path = f"{path}?{urlencode(cleaned)}"
        return await self.request("GET", path)

    async def post(self, path: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        """发 POST 请求。

        Args:
            path: 路径。
            payload: 请求体。

        Returns:
            响应 dict。
        """

        return await self.request("POST", path, payload)

    # ── 便捷方法 ──────────────────────────────────────

    async def health(self) -> dict[str, Any]:
        """健康检查。

        Returns:
            app 的健康信息。
        """

        return await self.get("/api/health")

    async def whoami(self) -> dict[str, Any]:
        """探测当前密钥是否有写权限。

        排队姬的只读接口不做鉴权（OBS 浏览器源没法加请求头），所以光靠健康检查
        发现不了「密钥配错了」；这个接口会带上密钥去问 app「我能写吗」。

        Returns:
            含 ``can_write`` / ``token_required`` 的 dict。
        """

        return await self.post("/api/whoami", {})

    async def state(self, *, queue: str = "", limit: int = 0) -> dict[str, Any]:
        """取队列状态快照。

        Args:
            queue: 只取某个队列。
            limit: 每队最多返回多少条目。

        Returns:
            状态 dict。
        """

        return await self.get("/api/state", {"queue": queue, "limit": limit or None})

    async def queues(self) -> dict[str, Any]:
        """取队列名列表（原始返回）。

        Returns:
            ``{"ok": True, "queues": [...]}``。所有客户端方法都返回 dict，方便统一转发。
        """

        return await self.get("/api/queues")

    async def queue_names(self) -> list[str]:
        """取队列名列表（便捷形式）。

        Returns:
            队列名列表。
        """

        data = await self.queues()
        names = data.get("queues")
        return [str(name) for name in names] if isinstance(names, list) else []

    async def join(
        self,
        *,
        user_id: str,
        nickname: str = "",
        platform: str = "",
        queue: str = "",
        queue_explicit: bool = True,
        extra: dict[str, Any] | None = None,
        guild: str = "",
        event_id: str = "",
    ) -> dict[str, Any]:
        """把观众加进队列。

        Args:
            user_id: 平台用户 ID。
            nickname: 昵称。
            platform: 平台标识。
            queue: 队名（app 侧负责别名与默认队解析）。
            queue_explicit: 队名是否是弹幕里明确写出来的。
            extra: 平台附加信息。
            guild: 归属标识（直播间号）。
            event_id: 平台事件 ID（B 站弹幕是 msg_id），用于幂等去重。

        Returns:
            app 的结果 dict。
        """

        return await self.post(
            "/api/join",
            {
                "user_id": user_id,
                "nickname": nickname,
                "platform": platform,
                "queue": queue,
                "queue_explicit": queue_explicit,
                "extra": extra or {},
                "guild": guild,
                "event_id": event_id,
            },
        )

    async def leave(
        self,
        *,
        user_id: str,
        queue: str = "",
        queue_explicit: bool = False,
        event_id: str = "",
    ) -> dict[str, Any]:
        """让观众退队。

        Args:
            user_id: 平台用户 ID。
            queue: 队名；留空表示退出全部队列。
            queue_explicit: 队名是否明确指定。
            event_id: 平台事件 ID，用于幂等去重。

        Returns:
            app 的结果 dict。
        """

        return await self.post(
            "/api/leave",
            {
                "user_id": user_id,
                "queue": queue,
                "queue_explicit": queue_explicit,
                "event_id": event_id,
            },
        )

    async def call_next(self, queue: str = "") -> dict[str, Any]:
        """叫号。

        Args:
            queue: 队名。

        Returns:
            app 的结果 dict。
        """

        return await self.post("/api/call_next", {"queue": queue})

    async def finish(self, queue: str = "") -> dict[str, Any]:
        """结束当前。

        Args:
            queue: 队名。

        Returns:
            app 的结果 dict。
        """

        return await self.post("/api/finish", {"queue": queue})

    async def requeue(self, queue: str = "") -> dict[str, Any]:
        """把当前放回队首。

        Args:
            queue: 队名。

        Returns:
            app 的结果 dict。
        """

        return await self.post("/api/requeue", {"queue": queue})

    async def clear(self, queue: str = "") -> dict[str, Any]:
        """清空队列。

        Args:
            queue: 队名。

        Returns:
            app 的结果 dict。
        """

        return await self.post("/api/clear", {"queue": queue})

    async def set_paused(self, queue: str = "", *, paused: bool) -> dict[str, Any]:
        """暂停 / 恢复队列。

        Args:
            queue: 队名。
            paused: ``True`` 表示暂停。

        Returns:
            app 的结果 dict。
        """

        return await self.post("/api/pause", {"queue": queue, "paused": paused})

    async def set_capacity(self, queue: str = "", capacity: int = 0) -> dict[str, Any]:
        """改队列人数上限。

        Args:
            queue: 队名。
            capacity: 上限，``0`` 表示不限。

        Returns:
            app 的结果 dict。
        """

        return await self.post("/api/capacity", {"queue": queue, "capacity": capacity})

    async def create_queue(
        self, queue: str, *, display_name: str = "", capacity: int = 0
    ) -> dict[str, Any]:
        """建队。

        Args:
            queue: 队名。
            display_name: 展示名。
            capacity: 人数上限。

        Returns:
            app 的结果 dict。
        """

        return await self.post(
            "/api/create",
            {"queue": queue, "display_name": display_name, "capacity": capacity},
        )

    async def delete_queue(self, queue: str) -> dict[str, Any]:
        """删队。

        Args:
            queue: 队名。

        Returns:
            app 的结果 dict。
        """

        return await self.post("/api/delete", {"queue": queue})

    async def remove_user(self, user_id: str, queue: str = "") -> dict[str, Any]:
        """把指定观众移出队列。

        Args:
            user_id: 平台用户 ID。
            queue: 队名。

        Returns:
            app 的结果 dict。
        """

        return await self.post("/api/remove", {"queue": queue, "user_id": user_id})

    async def move_front(self, user_id: str, queue: str = "") -> dict[str, Any]:
        """把指定观众插到队首。

        Args:
            user_id: 平台用户 ID。
            queue: 队名。

        Returns:
            app 的结果 dict。
        """

        return await self.post("/api/move_front", {"queue": queue, "user_id": user_id})

    async def add_manual(self, nickname: str, queue: str = "") -> dict[str, Any]:
        """手动加人（没有平台 ID，app 侧生成占位 ID）。

        Args:
            nickname: 昵称。
            queue: 队名。

        Returns:
            app 的结果 dict。
        """

        return await self.post("/api/add", {"queue": queue, "nickname": nickname})

    async def reset(self, *, delete_queues: bool = False) -> dict[str, Any]:
        """重置全部队列。

        Args:
            delete_queues: 为真时连队列本身一起删掉。

        Returns:
            app 的结果 dict。
        """

        return await self.post("/api/reset", {"delete": delete_queues})

    async def flush(self) -> dict[str, Any]:
        """让 app 立刻落盘。

        Returns:
            app 的结果 dict。
        """

        return await self.post("/api/flush", {})


__all__ = ["DEFAULT_TIMEOUT", "QueueAppClient", "QueueAppError"]
