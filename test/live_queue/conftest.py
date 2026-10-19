"""live_queue 测试共用夹具。

这些测试**不需要 mofox**：live_queue 是被设计成能独立运行的，测试也照这个前提写。
"""

from __future__ import annotations

import logging
import os
from collections.abc import Iterator
from pathlib import Path

import pytest

# 必须在任何 PySide6 导入之前设置：让窗口测试跑在离屏后端，
# 既不会在开发机上弹窗，也能在无显示环境（CI）里跑
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from live_queue.config import AppConfig
from live_queue.hub import QueueHub
from live_queue.store import QueueStore


class FakeClock:
    """可手动推进的时钟，替代 ``time.time``，让排队超时逻辑可测。"""

    def __init__(self, start: float = 1_700_000_000.0) -> None:
        """记录起始时间戳。

        Args:
            start: 起始 epoch 秒。
        """

        self.now = float(start)

    def __call__(self) -> float:
        """返回当前时间戳。"""

        return self.now

    def advance(self, seconds: float) -> None:
        """把时间往前推。

        Args:
            seconds: 推进的秒数。
        """

        self.now += float(seconds)


@pytest.fixture
def clock() -> FakeClock:
    """提供可推进的假时钟。"""

    return FakeClock()


def _legacy_config() -> AppConfig:
    """旧的宽松队列语义：允许自动建队、没写队名可回落默认队。

    产品默认已经是「必须写明一个已存在的队名、且不自动建队」；严格语义由
    test_strict_queue.py 专门覆盖。其它用例这里保留宽松语义，方便各自
    聚焦自己的主题（线程安全、持久化、窗口联动等）。
    """

    instance = AppConfig()
    instance.queue.auto_create_queue = True
    instance.queue.require_explicit_queue = False
    return instance


@pytest.fixture
def legacy_config() -> AppConfig:
    """旧宽松语义的配置（自己造 hub 的用例直接要这个夹具）。"""

    return _legacy_config()


@pytest.fixture
def config() -> AppConfig:
    """把维护间隔压短的配置，便于测试落盘与超时清理。"""

    instance = _legacy_config()
    instance.server.flush_interval = 1.0
    instance.server.purge_interval = 1.0
    return instance


@pytest.fixture
def store(tmp_path: Path) -> QueueStore:
    """指向临时目录的状态存储。"""

    return QueueStore(tmp_path / "data" / "queues.json")


@pytest.fixture
def logger() -> logging.Logger:
    """不会往控制台吐东西的日志器。"""

    instance = logging.getLogger("live_queue_test")
    instance.addHandler(logging.NullHandler())
    return instance


@pytest.fixture
def hub(config: AppConfig, store: QueueStore, logger: logging.Logger) -> Iterator[QueueHub]:
    """已启动的队列服务层（会自动停掉维护线程）。"""

    instance = QueueHub(config=config, store=store, logger=logger)
    instance.start()
    yield instance
    instance.stop()
