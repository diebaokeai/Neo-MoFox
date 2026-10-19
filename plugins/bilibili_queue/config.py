"""bilibili_queue 插件配置模型。

配置落盘路径：``config/plugins/bilibili_queue/config.toml``，首次加载时由框架自动生成。

**这里只配「弹幕怎么接」和「独立 app 在哪」**：队列语义（默认队名、别名、人数上限、
重复策略……）由独立应用 ``live_queue`` 自己的 ``live_queue/config.json`` 决定，
不再重复一份——两份配置必然漂移。

注意：框架的配置模型是 ``extra="forbid"``，TOML 里未在此声明的键会被静默丢弃，
因此所有可调项都必须在这里定义。
"""

from __future__ import annotations

from typing import ClassVar

from src.app.plugin_system.base import (
    BaseConfig,
    Field,
    SectionBase,
    config_section,
)

from .core.settings import DEFAULT_APP_HOST, DEFAULT_APP_PORT, DEFAULT_START_TIMEOUT


class BilibiliQueueConfig(BaseConfig):
    """直播弹幕排队插件配置。"""

    name: ClassVar[str] = "config"
    description: ClassVar[str] = "B 站直播弹幕排队插件配置"

    # ── 总开关 ────────────────────────────────────────

    @config_section("plugin", title="插件总开关", tag="plugin")
    class PluginSection(SectionBase):
        """插件级开关。"""

        enabled: bool = Field(
            default=True,
            description="是否启用排队插件；关闭后弹幕不再触发排队，独立 app 仍可单独使用",
        )

    plugin: PluginSection = Field(default_factory=PluginSection)

    # ── 关键词 ────────────────────────────────────────

    @config_section("keywords", title="触发关键词", tag="keywords")
    class KeywordSection(SectionBase):
        """弹幕触发词。

        匹配规则：弹幕以关键词开头即命中，关键词后面的内容会被当作队名，
        例如「排队 排位」「排队：排位」「排队排位」都会排进「排位」队。
        队名到底是哪个队、别名怎么映射，由独立 app 决定。
        """

        join: list[str] = Field(
            default_factory=lambda: ["排队", "排个队", "上车", "报名"],
            description="入队关键词；只发关键词（没写队名）会提示「请写明要排哪个队」",
            input_type="list",
            item_type="str",
        )

        leave: list[str] = Field(
            default_factory=lambda: ["退队", "下车", "取消排队", "退出排队"],
            description="退队关键词；只发关键词则退出自己所在的队列",
            input_type="list",
            item_type="str",
        )

        status: list[str] = Field(
            default_factory=lambda: [
                "排队情况",
                "排队查询",
                "查询排队",
                "队列状态",
                "排到哪了",
            ],
            description=(
                "状态查询关键词；命中后回报队列概况。匹配是「最长优先」，"
                "所以写了「排队情况」就不会被入队关键词「排队」抢走"
            ),
            input_type="list",
            item_type="str",
        )

        ignore_case: bool = Field(
            default=True,
            description="匹配时是否忽略英文大小写（中文不受影响）",
        )

    keywords: KeywordSection = Field(default_factory=KeywordSection)

    # ── 管理权限 ──────────────────────────────────────

    @config_section("admin", title="弹幕管理权限", tag="admin")
    class AdminSection(SectionBase):
        """谁能用弹幕管理队列。

        B 站开放平台的弹幕包里只有 ``guard_level``（大航海等级），**没有房管标记**，
        所以只能按舰长等级授权。注意 B 站 ``guard_level`` 数字越小等级越高：
        1=总督、2=提督、3=舰长。
        """

        min_rank: str = Field(
            default="captain",
            description=(
                "允许用弹幕管理队列的最低大航海等级："
                "anyone=任何人都能管理（不推荐），"
                "captain=舰长及以上（1/2/3 级），"
                "admiral=提督及以上（1/2 级），"
                "governor=仅总督（1 级）"
            ),
            input_type="select",
            choices=["anyone", "captain", "admiral", "governor"],
        )

        allow_danmaku_commands: bool = Field(
            default=True,
            description="是否允许房管/舰长用弹幕命令叫号、清空、暂停等",
        )

        command_prefix: str = Field(
            default="",
            description=(
                "房管弹幕命令前缀；留空表示直接发「下一个」这类词即可，"
                "填写后必须发「前缀+命令」，可避免和普通聊天撞词"
            ),
        )

    admin: AdminSection = Field(default_factory=AdminSection)

    # ── 行为细节 ──────────────────────────────────────

    @config_section("behavior", title="行为细节", tag="behavior")
    class BehaviorSection(SectionBase):
        """入队反馈、平台范围等行为开关。"""

        platforms: list[str] = Field(
            default_factory=lambda: ["bilibili_live"],
            description=(
                "允许触发排队的平台标识列表；B 站直播为 bilibili_live，"
                "如需在 QQ 群里也能排队可加上 onebot 等平台名"
            ),
            input_type="list",
            item_type="str",
        )

        block_message_from_llm: bool = Field(
            default=True,
            description=(
                "命中排队关键词的弹幕是否拦截、不再进入 LLM 对话流程。"
                "开启可避免「排队」被 AI 当普通聊天回复，也能省 token"
            ),
        )

        reply_in_stream: bool = Field(
            default=False,
            description=(
                "是否把「已入队」等反馈发回原聊天流。注意 B 站开放平台禁止第三方 bot "
                "发弹幕，该适配器的出站是 no-op，所以对 B 站直播打开这项不会有效果；"
                "对支持出站的平台（如 QQ）才有实际作用"
            ),
        )

        reply_on_join: bool = Field(
            default=True,
            description="入队成功时是否回复确认（需先打开 reply_in_stream）",
        )

        reply_on_leave: bool = Field(
            default=True,
            description="退队成功时是否回复确认（需先打开 reply_in_stream）",
        )

        reply_cooldown: float = Field(
            default=5.0,
            description="同一直播间内两次自动回复之间的最小间隔秒数，防止刷屏",
        )

        queue_status_command: bool = Field(
            default=True,
            description="观众发「排队情况」这类关键词时是否回报队列概况",
        )

    behavior: BehaviorSection = Field(default_factory=BehaviorSection)

    # ── 独立 app 连接 ─────────────────────────────────

    @config_section("app", title="独立排队姬应用", tag="network")
    class AppSection(SectionBase):
        """独立应用 ``live_queue`` 的连接与自动拉起。

        排队姬是一个**能单独运行**的程序（``live_queue/``），队列状态存在它那边；
        本插件只通过 HTTP 控制它。所以这里的地址/端口必须和 ``live_queue/config.json``
        里的 ``server.host`` / ``server.port`` 对上（默认 127.0.0.1:17650）。
        """

        host: str = Field(
            default=DEFAULT_APP_HOST,
            description="独立 app 的地址；通常和 bot 在同一台机器，保持 127.0.0.1 即可",
        )

        port: int = Field(
            default=DEFAULT_APP_PORT,
            description="独立 app 的端口，必须与 live_queue/config.json 的 server.port 一致",
        )

        token: str = Field(
            default="",
            description=(
                "独立 app 的写操作密钥，对应它的 server.token。留空时按「仅本机可写」"
                "处理；跨机器控制时必须两边都配同一个足够长的随机串"
            ),
        )

        auto_start: bool = Field(
            default=True,
            description=(
                "插件加载/需要排队时，如果发现独立 app 没在跑就自动把它拉起来。"
                "关掉后需要自己先启动 live_queue，否则观众排队会无效"
            ),
        )

        start_timeout: float = Field(
            default=DEFAULT_START_TIMEOUT,
            description="拉起后等待独立 app 就绪的秒数（首次启动要加载 Qt，给宽一点）",
        )

        no_window: bool = Field(
            default=False,
            description=(
                "拉起独立 app 时只开服务、不开桌面窗口（--ui off）。"
                "适合用 OBS 浏览器源叠加页、或者 app 跑在没屏幕的机器上的场景"
            ),
        )

        executable: str = Field(
            default="",
            description="启动独立 app 用的 Python 解释器路径；留空表示用当前 bot 的解释器",
        )

        extra_args: list[str] = Field(
            default_factory=list,
            description="附加给 live_queue/app.py 的命令行参数（逃生舱）",
            input_type="list",
            item_type="str",
        )

        stop_on_unload: bool = Field(
            default=False,
            description=(
                "插件卸载/关闭 bot 时是否一并关掉独立 app。默认 false——排队姬是能单独"
                "使用的程序，不该因为 bot 重启就被关掉（而且用户可能自己开着它）"
            ),
        )

    app: AppSection = Field(default_factory=AppSection)

    # ── 调试 ──────────────────────────────────────────

    @config_section("debug", title="调试", tag="debug")
    class DebugSection(SectionBase):
        """排查用开关。"""

        log_danmaku: bool = Field(
            default=False,
            description="是否把每条收到的弹幕都写 debug 日志（很吵，只在排查时打开）",
        )

        log_commands: bool = Field(
            default=True,
            description="是否记录队列管理操作日志（叫号、清空、暂停等）",
        )

    debug: DebugSection = Field(default_factory=DebugSection)


__all__ = ["BilibiliQueueConfig"]
