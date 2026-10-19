"""live_queue —— 独立运行的直播排队应用。

设计目标：**不依赖任何机器人框架**。整个目录拷到别的机器上，只要有 Python 和
（可选的）PySide6 就能跑；队列状态、窗口、OBS 叠加页、HTTP 接口全部由它自己提供。

机器人（mofox）在另一边作为**控制方**：通过本应用的 HTTP 接口把弹幕观众加进队列、
叫号、暂停队列。没开机器人时，这个应用照样能独立使用。

模块划分：

- :mod:`live_queue.core` —— 纯内存的队列数据模型与调度器（零第三方依赖）
- :mod:`live_queue.store` —— 本地 JSON 持久化
- :mod:`live_queue.config` —— 本地 JSON 配置
- :mod:`live_queue.hub` —— 线程安全的服务层（窗口线程与 HTTP 线程共用）
- :mod:`live_queue.server` —— 标准库 HTTP 服务（API + 叠加页 + 控制面板）
- :mod:`live_queue.ui` —— PySide6 桌面窗口（可选）
"""

__version__ = "1.1.0"

__all__ = ["__version__"]
