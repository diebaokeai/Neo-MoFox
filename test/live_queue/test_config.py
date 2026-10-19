"""live_queue 配置与持久化的单测。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from live_queue.config import (
    AppConfig,
    OverlayConfig,
    config_from_dict,
    load_config,
    save_config,
)
from live_queue.store import QueueStore, StoreError

# ── 配置 ──────────────────────────────────────────────


def test_default_config_is_sane() -> None:
    """默认值要能直接跑起来。"""

    config = AppConfig()
    assert config.server.host == "127.0.0.1"
    assert 1024 < config.server.port < 65535
    assert config.server.token == ""
    assert config.queue.default_queue == "默认"
    assert config.queue.duplicate_policy == "reject"
    assert config.window.enabled is True
    assert config.overlay.max_rows == 8
    assert any("=" in item for item in config.queue.aliases)


def test_config_round_trip(tmp_path: Path) -> None:
    """写盘再读回应完全一致。"""

    path = tmp_path / "config.json"
    config = AppConfig()
    config.server.port = 18080
    config.window.title = "我的排队姬"
    config.overlay.queues = ["排位"]
    save_config(path, config)

    loaded, state = load_config(path)
    assert state == "loaded"
    assert loaded.server.port == 18080
    assert loaded.window.title == "我的排队姬"
    assert loaded.overlay.queues == ["排位"]


def test_load_creates_default_file(tmp_path: Path) -> None:
    """文件不存在时生成默认配置并落盘。"""

    path = tmp_path / "nested" / "config.json"
    config, state = load_config(path)

    assert state == "created"
    assert path.exists()
    assert json.loads(path.read_text(encoding="utf-8"))["version"] == 1
    assert isinstance(config, AppConfig)


def test_load_recovers_from_broken_file(tmp_path: Path) -> None:
    """文件损坏时重置为默认值，而不是让应用起不来。"""

    path = tmp_path / "config.json"
    path.write_text("{ 这不是 JSON", encoding="utf-8")

    config, state = load_config(path)
    assert state == "recovered"
    assert isinstance(config, AppConfig)
    # 重新写回一份合法配置
    assert json.loads(path.read_text(encoding="utf-8"))["version"] == 1


def test_load_recovers_from_non_object(tmp_path: Path) -> None:
    """顶层不是对象时同样重置。"""

    path = tmp_path / "config.json"
    path.write_text("[1, 2, 3]", encoding="utf-8")
    _, state = load_config(path)
    assert state == "recovered"


def test_partial_config_keeps_other_defaults(tmp_path: Path) -> None:
    """只写一部分字段时，其余保持默认值。"""

    path = tmp_path / "config.json"
    path.write_text('{"server": {"port": 19999}}', encoding="utf-8")

    config, state = load_config(path)
    assert state == "loaded"
    assert config.server.port == 19999
    assert config.server.host == "127.0.0.1"
    assert config.window.title == "排队姬"


@pytest.mark.parametrize(
    ("payload", "attr", "expected"),
    [
        ({"server": {"port": "不是数字"}}, "port", 17650),
        ({"server": {"host": 123}}, "host", "127.0.0.1"),
        ({"window": {"enabled": "yes"}}, "enabled", True),  # 字符串不当真值用
        ({"window": {"width": 480}}, "width", 480),
        ({"window": {"width": 480.0}}, "width", 480),  # 整数上来的浮点可以收
        ({"overlay": {"show_title": True}}, "show_title", True),
    ],
)
def test_type_mismatch_falls_back_to_default(
    payload: dict, attr: str, expected: object
) -> None:
    """类型明显对不上时保留默认值，不做隐式硬转。"""

    config = config_from_dict(payload)

    if "server" in payload and attr in ("port", "host"):
        assert getattr(config.server, attr) == expected
    elif "window" in payload and attr in ("enabled", "width"):
        assert getattr(config.window, attr) == expected
    else:
        assert getattr(config.overlay, attr) == expected


def test_string_false_is_not_coerced_to_true() -> None:
    """``"false"`` 这种字符串不能变成 True（``bool("false")`` 是陷阱）。"""

    config = config_from_dict({"window": {"enabled": "false"}})
    assert config.window.enabled is True  # 回退默认值 True，而不是被字符串左右


def test_unknown_keys_are_dropped() -> None:
    """不认识的键被丢弃，不会越积越乱。"""

    config = config_from_dict({"server": {"port": 1234}, "不认识的节": {"x": 1}})
    payload = config.to_dict()
    assert "不认识的节" not in payload
    assert "x" not in payload["server"]


def test_alias_list_is_sanitised() -> None:
    """别名列表里的空白项会被过滤。"""

    config = config_from_dict({"queue": {"aliases": ["排位赛=排位", "  ", ""]}})
    assert config.queue.aliases == ["排位赛=排位"]


def test_overlay_queues_list_accepts_empty() -> None:
    """显示队列白名单可以是空列表（表示全部）。"""

    config = config_from_dict({"overlay": {"queues": []}})
    assert config.overlay.queues == []


def test_to_dict_is_json_serialisable() -> None:
    """导出的配置必须能直接 json.dumps。"""

    payload = AppConfig().to_dict()
    assert json.loads(json.dumps(payload))["version"] == 1
    assert isinstance(payload["overlay"], dict)


def test_overlay_config_dataclass_is_independent() -> None:
    """两个实例的列表字段不能共享同一个对象。"""

    first = OverlayConfig()
    second = OverlayConfig()
    first.queues.append("排位")
    assert second.queues == []


# ── 持久化 ────────────────────────────────────────────


def test_store_round_trip(tmp_path: Path) -> None:
    """写完读回来要一致，并且目录会自动创建。"""

    store = QueueStore(tmp_path / "deep" / "nested" / "queues.json")
    payload = {"version": 1, "queues": [{"name": "排位"}], "revision": 3}

    store.save(payload)
    assert store.exists() is True
    assert store.load() == payload


def test_store_load_missing_returns_none(tmp_path: Path) -> None:
    """文件不存在返回 None（首次运行）。"""

    assert QueueStore(tmp_path / "nope.json").load() is None


def test_store_load_empty_file_returns_none(tmp_path: Path) -> None:
    """空文件按「没有状态」处理。"""

    path = tmp_path / "queues.json"
    path.write_text("   \n", encoding="utf-8")
    assert QueueStore(path).load() is None


def test_store_load_broken_json_raises(tmp_path: Path) -> None:
    """损坏的 JSON 要抛 StoreError（由上层决定怎么降级）。"""

    path = tmp_path / "queues.json"
    path.write_text("{ 坏掉的", encoding="utf-8")
    with pytest.raises(StoreError):
        QueueStore(path).load()


def test_store_load_non_object_raises(tmp_path: Path) -> None:
    """顶层不是对象也要报错。"""

    path = tmp_path / "queues.json"
    path.write_text("[1,2,3]", encoding="utf-8")
    with pytest.raises(StoreError):
        QueueStore(path).load()


def test_store_save_leaves_no_temp_files(tmp_path: Path) -> None:
    """原子替换后不应残留临时文件。"""

    directory = tmp_path / "data"
    store = QueueStore(directory / "queues.json")
    store.save({"queues": []})
    store.save({"queues": [], "revision": 2})

    leftovers = [item.name for item in directory.iterdir() if item.name != "queues.json"]
    assert leftovers == []


def test_store_delete(tmp_path: Path) -> None:
    """删除存在与不存在的文件。"""

    store = QueueStore(tmp_path / "queues.json")
    assert store.delete() is False
    store.save({"queues": []})
    assert store.delete() is True
    assert store.exists() is False


def test_store_save_failure_raises_store_error(tmp_path: Path) -> None:
    """父目录不可创建时抛 StoreError。"""

    blocker = tmp_path / "blocker"
    blocker.write_text("我是文件，不是目录", encoding="utf-8")
    store = QueueStore(blocker / "queues.json")

    with pytest.raises(StoreError):
        store.save({"queues": []})


def test_store_path_is_absolute(tmp_path: Path) -> None:
    """路径属性可直接用于日志展示。"""

    store = QueueStore(tmp_path / "queues.json")
    assert store.path.is_absolute()
