"""独立应用（``live_queue``）的进程管理。

插件在加载时、以及每次真正要用队列之前，都会调 :meth:`QueueAppSupervisor.ensure_running`：

1. 先请求 ``/api/health``，通了且确认是 live_queue，就什么都不做（**最常见的情况**：
   用户自己已经把排队姬开着了）；
2. 没通则判断 ``app.auto_start``，为真就用当前解释器把 ``live_queue/app.py`` 拉起来；
3. 拉起后轮询健康检查直到超时。

几个要点：

- **同一个进程只用拉起一次**：并发调用由 :class:`asyncio.Lock` 串行化，否则弹幕高峰
  可能会同时拉出好几个窗口；
- **端口被别人占用时要说清楚**：健康检查通了但 ``app`` 字段对不上，说明那一端口上
  坐的是别的程序，这时绝不能盲目再拉一个（会一直起不来还看不懂为什么）；
- **窗口是 app 的责任**：本模块只负责把 app 跑起来，开不开窗口由 app 自己的
  ``--ui`` 参数决定（``app.no_window`` 为真时传 ``--ui off``）。
"""

from __future__ import annotations

import asyncio
import subprocess
import sys
from pathlib import Path
from typing import Any

from src.app.plugin_system.api import log_api

from .client import QueueAppClient, QueueAppError
from .settings import PluginSettings

logger = log_api.get_logger("bilibili_queue.supervisor")

PROJECT_ROOT = Path(__file__).resolve().parents[3]
"""项目根目录（``plugins/bilibili_queue/core/supervisor.py`` 往上四级）。"""

APP_DIR = PROJECT_ROOT / "live_queue"
"""独立应用所在目录。"""

APP_SCRIPT = APP_DIR / "app.py"
"""独立应用入口脚本。"""

LOG_PATH = PROJECT_ROOT / "logs" / "live_queue_app.log"
"""被插件拉起的 app 的 stdout/stderr 落盘位置。"""

_HEALTH_POLL_INTERVAL = 0.5
"""拉起后轮询健康检查的间隔（秒）。"""


class QueueAppSupervisor:
    """独立 app 的探测 / 拉起 / 关闭。"""

    def __init__(
        self,
        *,
        settings: PluginSettings,
        client: QueueAppClient,
        app_script: Path | None = None,
    ) -> None:
        """记录连接与启动参数。

        Args:
            settings: 插件运行参数。
            client: 用来做健康检查的客户端。
            app_script: 覆盖 app 脚本路径（测试用）。
        """

        self._settings = settings
        self._client = client
        self._app_script = Path(app_script) if app_script else APP_SCRIPT
        self._process: subprocess.Popen[bytes] | None = None
        self._log_handle: Any = None
        self._lock = asyncio.Lock()

    # ── 状态 ──────────────────────────────────────────

    @property
    def app_script(self) -> Path:
        """app 入口脚本路径。"""

        return self._app_script

    @property
    def spawned(self) -> bool:
        """当前进程是否由本插件拉起。"""

        return self._process is not None

    @property
    def is_alive(self) -> bool:
        """被拉起的进程是否还活着。"""

        return self._process is not None and self._process.poll() is None

    @property
    def log_path(self) -> Path:
        """子进程日志路径。"""

        return LOG_PATH

    # ── 命令构造 ──────────────────────────────────────

    def build_command(self) -> list[str]:
        """组装启动 app 的命令行。

        Returns:
            可直接交给 :class:`subprocess.Popen` 的参数列表。
        """

        executable = self._settings.app_executable or sys.executable
        command: list[str] = [
            executable,
            str(self._app_script),
            "--host",
            self._settings.app_host,
            "--port",
            str(self._settings.app_port),
            "--ui",
            "off" if self._settings.app_no_window else "auto",
        ]
        if self._settings.app_token:
            command += ["--token", self._settings.app_token]
        command += [str(item) for item in self._settings.app_extra_args if str(item).strip()]
        return command

    # ── 探测 ──────────────────────────────────────────

    async def probe(self) -> tuple[str, str]:
        """探测 app 当前状态。

        Returns:
            ``(状态, 说明)``，状态取值：

            - ``running``：live_queue 正在跑；
            - ``offline``：连不上（大概率没启动）；
            - ``occupied``：端口上有响应，但不是 live_queue；
            - ``unauthorized``：连上了但密钥不对（``app.token`` 配错了）。
        """

        try:
            health = await self._client.health()
        except QueueAppError as exc:
            if exc.kind == "http" and exc.status in (401, 403):
                return "unauthorized", str(exc)
            return "offline", str(exc)

        if str(health.get("app") or "") != "live_queue":
            return "occupied", (
                f"{self._client.base_url} 上有服务在响应，但它不是 live_queue"
                f"（返回 {health.get('app')!r}）"
            )

        # 只读接口不鉴权，所以还要单独问一次「我这把钥匙能写吗」——
        # 否则密钥配错要等第一条弹幕进来才暴露
        try:
            whoami = await self._client.whoami()
        except QueueAppError as exc:
            logger.debug(f"写权限探测失败（按可用处理）：{exc}")
        else:
            if whoami.get("can_write") is False:
                return "unauthorized", (
                    "密钥不匹配：排队姬要求写操作带密钥，但它不接受当前插件配置里的 app.token"
                    "（两边必须一致，或都留空只用本机访问）"
                )

        return "running", f"live_queue {health.get('version', '?')}"

    async def is_running(self) -> bool:
        """app 是否可用。

        Returns:
            可用返回 ``True``。
        """

        state, _ = await self.probe()
        return state == "running"

    # ── 拉起 ──────────────────────────────────────────

    async def ensure_running(self) -> tuple[bool, str]:
        """确保 app 可用，必要时把它拉起来。

        Returns:
            ``(是否可用, 说明)``。
        """

        async with self._lock:
            state, detail = await self.probe()

            if state == "running":
                return True, f"独立 app 已在运行（{detail}）"

            if state == "occupied":
                logger.error(detail)
                logger.error(
                    "请把插件配置里的 app.port 换成 live_queue 真正使用的端口"
                    "（或先关掉占用该端口的程序）"
                )
                return False, detail

            if state == "unauthorized":
                logger.error(
                    f"独立 app 拒绝访问：{detail}。请让插件配置里的 app.token "
                    "与 live_queue 的 server.token 保持一致"
                )
                return False, detail

            if not self._settings.app_auto_start:
                message = (
                    f"独立 app 未运行（{detail}），且配置里关闭了自动拉起；"
                    "请手动启动：python live_queue/app.py"
                )
                logger.warning(message)
                return False, message

            if not self._spawn():
                return False, "启动独立 app 失败，详见日志"

            ready = await self._wait_until_ready()
            if ready:
                return True, "已自动拉起独立 app"
            return False, f"独立 app 启动后 {self._settings.app_start_timeout} 秒内未就绪"

    def _spawn(self) -> bool:
        """用子进程拉起 app。

        Returns:
            拉起来了返回 ``True``。
        """

        if self.is_alive:
            return True

        if not self._app_script.exists():
            logger.error(f"找不到独立应用入口：{self._app_script}")
            logger.error("请确认 live_queue/ 目录与插件在同一仓库下，或手动启动它")
            return False

        command = self.build_command()
        creation_flags = 0
        if sys.platform == "win32":
            # 不开新控制台窗口：直播时突然弹个黑框很难看
            creation_flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)

        try:
            self._log_handle = self._open_log()
            self._process = subprocess.Popen(
                command,
                cwd=str(PROJECT_ROOT),
                stdout=self._log_handle,
                stderr=subprocess.STDOUT,
                creationflags=creation_flags,
            )
        except Exception as exc:
            logger.error(f"启动独立 app 失败: {exc}", exc_info=True)
            self._close_log()
            self._process = None
            return False

        logger.info(
            f"已拉起独立 app（pid={self._process.pid}），"
            f"叠加页 http://{self._settings.app_host}:{self._settings.app_port}/overlay，"
            f"输出见 {LOG_PATH}"
        )
        return True

    async def _wait_until_ready(self) -> bool:
        """轮询健康检查直到 app 就绪或超时。

        Returns:
            就绪返回 ``True``。
        """

        deadline = asyncio.get_running_loop().time() + max(1.0, self._settings.app_start_timeout)
        while asyncio.get_running_loop().time() < deadline:
            if self._process is not None and self._process.poll() is not None:
                logger.error(
                    f"独立 app 进程已退出（返回码 {self._process.returncode}），"
                    f"请查看日志 {LOG_PATH}"
                )
                self._process = None
                self._close_log()
                return False
            if await self.is_running():
                logger.info("独立 app 已就绪")
                return True
            await asyncio.sleep(_HEALTH_POLL_INTERVAL)
        return False

    def stop(self, *, timeout: float = 5.0) -> bool:
        """关闭**由本插件拉起**的 app。

        用户自己启动的 app 不会被关掉（插件管不着，也不该管）。

        Args:
            timeout: 先礼后兵的等待秒数。

        Returns:
            确实关掉了返回 ``True``。
        """

        process = self._process
        if process is None:
            self._close_log()
            return False

        if process.poll() is None:
            try:
                process.terminate()
                process.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                logger.warning("独立 app 未在超时内退出，强制结束")
                process.kill()
                try:
                    process.wait(timeout=timeout)
                except subprocess.TimeoutExpired:
                    logger.error("独立 app 无法结束，已放弃")
            except Exception as exc:
                logger.warning(f"关闭独立 app 时出错: {exc}")

        self._process = None
        self._close_log()
        logger.info("独立 app 已关闭")
        return True

    # ── 日志文件 ──────────────────────────────────────

    @staticmethod
    def _open_log() -> Any:
        """打开 app 的输出日志。

        Returns:
            追加模式的文件对象；打不开时返回 ``None``。
        """

        try:
            LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
            return LOG_PATH.open("ab")
        except OSError as exc:
            logger.warning(f"无法创建 live_queue 日志文件（{exc}），子进程输出将被丢弃")
            return None

    def _close_log(self) -> None:
        """关闭日志文件句柄。"""

        if self._log_handle is None:
            return
        try:
            self._log_handle.close()
        except OSError:
            pass
        self._log_handle = None


__all__ = [
    "APP_DIR",
    "APP_SCRIPT",
    "LOG_PATH",
    "PROJECT_ROOT",
    "QueueAppSupervisor",
]
