"""B 站直播长连接协议（独立版，完全不依赖 MoFox）。

从 MoFox 插件的长连实现里移植出来的最小可用子集，供 live_queue 的独立弹幕桥使用：

- proto    —— 长连二进制协议：打包 / 解包 / 解压（纯标准库）
- api      —— 开放平台 HTTP 签名与 start / heartbeat / end
- client   —— 长连客户端：鉴权 + 双心跳 + recv 循环
- keywords —— 弹幕关键词解析：排队 / 退队 / 状态 / 房管命令（纯标准库）

移植时只做了两类适配：

1. MoFox 的 get_logger -> 标准库 logging（logger 名分别为
   live_queue.bilibili.api / live_queue.bilibili.client）；
2. client 的 task_manager -> asyncio.create_task（Task 存成实例属性避免被 GC，
   stop() 逐个 cancel 并 await 收尾，吞掉 CancelledError）。

长连协议本身、公开类名 / 函数名 / 构造参数 / 返回值语义都与原实现一致。
重连（断线后重新 start_app 取新 game_id）不在本包内，由调用方负责。
"""

from . import api, client, keywords, proto
from .api import BilibiliApi, BilibiliApiError, StartResponse
from .client import BilibiliClient, BilibiliClientError, EventCallback
from .keywords import (
    ADMIN_COMMANDS,
    Intent,
    KeywordMatcher,
    ParsedIntent,
    looks_like_queue_name,
)
from .proto import (
    HEADER_LEN,
    Header,
    OP_AUTH,
    OP_AUTH_REPLY,
    OP_BUSINESS,
    OP_HEARTBEAT,
    OP_HEARTBEAT_REPLY,
    VER_BROTLI,
    VER_HEARTBEAT,
    VER_PLAIN,
    VER_ZLIB,
    decompress,
    iter_packets,
    pack,
    unpack,
    unpack_header,
)

__all__ = [
    "ADMIN_COMMANDS",
    "BilibiliApi",
    "BilibiliApiError",
    "BilibiliClient",
    "BilibiliClientError",
    "EventCallback",
    "HEADER_LEN",
    "Header",
    "Intent",
    "KeywordMatcher",
    "OP_AUTH",
    "OP_AUTH_REPLY",
    "OP_BUSINESS",
    "OP_HEARTBEAT",
    "OP_HEARTBEAT_REPLY",
    "ParsedIntent",
    "StartResponse",
    "VER_BROTLI",
    "VER_HEARTBEAT",
    "VER_PLAIN",
    "VER_ZLIB",
    "api",
    "client",
    "decompress",
    "iter_packets",
    "keywords",
    "looks_like_queue_name",
    "pack",
    "proto",
    "unpack",
    "unpack_header",
]
