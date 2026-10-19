"""live_queue 的本地持久化。

状态放在**应用自己目录下**的 JSON 文件里（默认 ``live_queue/data/queues.json``），
而不是任何机器人框架的数据目录——这样整个文件夹拷到别的机器上就能接着用。

写入采用「先写临时文件再 ``os.replace``」的原子替换：即使写到一半断电，也不会
留下半截 JSON 把队列彻底弄坏。
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any

DEFAULT_STATE_FILENAME = "queues.json"
"""默认状态文件名。"""


class QueueStore:
    """队列状态的本地 JSON 读写。

    只负责 IO，不持有队列状态；调用方传入 / 取得完整快照 dict。
    不需要内部加锁：所有调用都发生在 :class:`live_queue.hub.QueueHub` 的临界区里。
    """

    def __init__(self, path: Path) -> None:
        """记录状态文件路径。

        Args:
            path: 状态文件路径（``.json``）。
        """

        self._path = Path(path)

    @property
    def path(self) -> Path:
        """状态文件的绝对路径。"""

        return self._path

    def load(self) -> dict[str, Any] | None:
        """读取状态快照。

        Returns:
            快照 dict；文件不存在、内容损坏或不是对象时返回 ``None``。
        """

        try:
            raw = self._path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise StoreError(f"读取状态文件失败：{exc}") from exc

        if not raw.strip():
            return None

        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise StoreError(f"状态文件不是合法 JSON：{exc}") from exc

        if not isinstance(data, dict):
            raise StoreError(f"状态文件顶层不是对象：{type(data).__name__}")
        return data

    def save(self, payload: dict[str, Any]) -> None:
        """原子地把快照写盘。

        Args:
            payload: :meth:`QueueManager.to_dict` 产出的结构。

        Raises:
            StoreError: 写盘失败（磁盘满、权限不足等）。
        """

        directory = self._path.parent
        try:
            directory.mkdir(parents=True, exist_ok=True)
            # 同目录下建临时文件，保证 os.replace 是同一文件系统内的原子操作
            handle, temp_name = tempfile.mkstemp(
                prefix=".queues-", suffix=".tmp", dir=str(directory)
            )
        except OSError as exc:
            raise StoreError(f"创建临时状态文件失败：{exc}") from exc

        temp_path = Path(temp_name)
        try:
            with os.fdopen(handle, "w", encoding="utf-8") as stream:
                json.dump(payload, stream, ensure_ascii=False, indent=2)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temp_path, self._path)
        except OSError as exc:
            temp_path.unlink(missing_ok=True)
            raise StoreError(f"写入状态文件失败：{exc}") from exc

    def exists(self) -> bool:
        """状态文件是否存在。"""

        return self._path.exists()

    def delete(self) -> bool:
        """删除状态文件。

        Returns:
            确实删掉了返回 ``True``。
        """

        try:
            self._path.unlink()
        except FileNotFoundError:
            return False
        except OSError as exc:
            raise StoreError(f"删除状态文件失败：{exc}") from exc
        return True


class StoreError(RuntimeError):
    """状态文件读写错误。"""


__all__ = ["DEFAULT_STATE_FILENAME", "QueueStore", "StoreError"]
