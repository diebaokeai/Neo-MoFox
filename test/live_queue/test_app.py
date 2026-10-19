"""live_queue 应用入口（app.py）的单测。

``--selftest`` 与 ``--ui off --exit-after`` 这两条路径能在测试里直接跑通，
也算是对「整机能起来」的最小验证。
"""

from __future__ import annotations

import argparse
import json
import socket
from pathlib import Path

import pytest

from live_queue import app as app_module
from live_queue.app import (
    DEFAULT_DATA_PATH,
    apply_overrides,
    build_parser,
    main,
    resolve_data_path,
)
from live_queue.config import AppConfig


def _args(**kwargs: object) -> argparse.Namespace:
    """构造命令行参数命名空间。

    Args:
        **kwargs: 要覆盖的字段。

    Returns:
        命名空间。
    """

    base = {
        "config": "",
        "data": "",
        "host": "",
        "port": 0,
        "token": "",
        "ui": "auto",
        "log_level": "INFO",
        "no_confirm_close": False,
        "exit_after": 0.0,
        "selftest": False,
    }
    base.update(kwargs)
    return argparse.Namespace(**base)


# ── 参数解析 ──────────────────────────────────────────


def test_parser_defaults() -> None:
    """默认参数适合直接双击运行。"""

    args = build_parser().parse_args([])

    assert args.ui == "auto"
    assert args.port == 0
    assert args.data == ""
    assert args.exit_after == 0.0
    assert args.selftest is False
    assert args.config.endswith("config.json")


def test_parser_accepts_overrides() -> None:
    """常用参数都能覆盖。"""

    args = build_parser().parse_args(
        [
            "--port",
            "18080",
            "--host",
            "0.0.0.0",
            "--token",
            "abc",
            "--ui",
            "off",
            "--log-level",
            "DEBUG",
            "--no-confirm-close",
            "--exit-after",
            "1.5",
        ]
    )

    assert (args.port, args.host, args.token) == (18080, "0.0.0.0", "abc")
    assert args.ui == "off"
    assert args.log_level == "DEBUG"
    assert args.no_confirm_close is True
    assert args.exit_after == 1.5


def test_ui_choices_are_restricted() -> None:
    """``--ui`` 只接受三个值。"""

    with pytest.raises(SystemExit):
        build_parser().parse_args(["--ui", "window"])


# ── 覆盖逻辑 ──────────────────────────────────────────


def test_apply_overrides_sets_fields() -> None:
    """命令行覆盖写进配置。"""

    config = apply_overrides(
        AppConfig(), _args(host="0.0.0.0", port=19999, token="tok")
    )
    assert config.server.host == "0.0.0.0"
    assert config.server.port == 19999
    assert config.server.token == "tok"


def test_apply_overrides_keeps_config_when_args_empty() -> None:
    """没传的参数不要覆盖配置文件里的值。"""

    config = AppConfig()
    config.server.port = 12345
    apply_overrides(config, _args())
    assert config.server.port == 12345


def test_apply_overrides_ui_off_disables_window() -> None:
    """``--ui off`` 等于关掉窗口。"""

    config = apply_overrides(AppConfig(), _args(ui="off"))
    assert config.window.enabled is False

    config = apply_overrides(AppConfig(), _args(ui="on"))
    assert config.window.enabled is True


def test_resolve_data_path() -> None:
    """默认状态文件与 ``--data`` 覆盖。"""

    assert resolve_data_path(_args()) == DEFAULT_DATA_PATH

    custom = resolve_data_path(_args(data="D:/tmp/queue.json"))
    assert custom == Path("D:/tmp/queue.json")


# ── 整机路径 ──────────────────────────────────────────


def test_selftest_creates_config_and_exits(tmp_path: Path, capsys) -> None:
    """``--selftest`` 生成配置、不开监听、正常退出。"""

    config_path = tmp_path / "config.json"
    data_path = tmp_path / "queues.json"

    code = main(
        [
            "--selftest",
            "--config",
            str(config_path),
            "--data",
            str(data_path),
            "--log-level",
            "ERROR",
        ]
    )

    assert code == 0
    assert config_path.exists()
    payload = json.loads(config_path.read_text(encoding="utf-8"))
    assert payload["server"]["port"] > 1024
    assert "SELFTEST OK" in capsys.readouterr().out


def test_selftest_on_corrupt_config_recovers(tmp_path: Path, capsys) -> None:
    """配置损坏时能自愈并跑通。"""

    config_path = tmp_path / "config.json"
    config_path.write_text("{ 坏的", encoding="utf-8")

    code = main(
        [
            "--selftest",
            "--config",
            str(config_path),
            "--data",
            str(tmp_path / "queues.json"),
            "--log-level",
            "ERROR",
        ]
    )

    assert code == 0
    assert json.loads(config_path.read_text(encoding="utf-8"))["version"] == 1


def test_headless_run_starts_and_stops_server(tmp_path: Path, capsys) -> None:
    """``--ui off`` 真的会起 HTTP 服务，并在到点后干净退出。"""

    # 显式取一个空闲端口：本机可能正跑着一个排队姬（占着配置里的默认端口）
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        free_port = int(probe.getsockname()[1])

    code = main(
        [
            "--ui",
            "off",
            "--port",
            str(free_port),
            "--config",
            str(tmp_path / "config.json"),
            "--data",
            str(tmp_path / "queues.json"),
            "--exit-after",
            "0.3",
            "--log-level",
            "ERROR",
        ]
    )

    assert code == 0
    captured = capsys.readouterr().out
    assert "OBS 浏览器源" in captured
    assert "/overlay" in captured
    # 退出时应把状态写盘
    assert (tmp_path / "queues.json").exists()


def test_port_conflict_returns_code_2(tmp_path: Path, monkeypatch, capsys) -> None:
    """端口被占用时返回明确退出码，而不是抛异常。"""

    from live_queue import server as server_module

    def fake_start(_self) -> None:
        raise server_module.ServerStartError("端口已被占用：测试替身")

    monkeypatch.setattr(server_module.QueueHTTPServer, "start", fake_start)

    code = main(
        [
            "--ui",
            "off",
            "--config",
            str(tmp_path / "config.json"),
            "--data",
            str(tmp_path / "queues.json"),
            "--log-level",
            "ERROR",
        ]
    )

    assert code == 2


def test_ui_on_without_pyside_exits_with_code_3(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``--ui on`` 但没装 PySide6 时给出明确退出码。"""

    def fake_loader(_mode: str, _logger: object) -> tuple[None, None]:
        raise SystemExit(3)

    monkeypatch.setattr(app_module, "_load_window_class", fake_loader)

    with pytest.raises(SystemExit) as excinfo:
        main(
            [
                "--ui",
                "on",
                "--selftest",
                "--config",
                str(tmp_path / "config.json"),
                "--data",
                str(tmp_path / "queues.json"),
                "--log-level",
                "ERROR",
            ]
        )
    assert excinfo.value.code == 3


def test_load_window_class_off_returns_none() -> None:
    """``--ui off`` 不导入 PySide6（headless 环境也能跑）。"""

    import logging

    window_class, factory = app_module._load_window_class("off", logging.getLogger("t"))
    assert window_class is None
    assert factory is None


def test_web_dir_contains_pages() -> None:
    """内置页面必须随应用一起分发。"""

    assert (app_module.WEB_DIR / "overlay.html").is_file()
    assert (app_module.WEB_DIR / "control.html").is_file()


def test_default_config_path_is_next_to_app() -> None:
    """配置与数据默认放在应用目录下，方便整包拷走。"""

    assert app_module.DEFAULT_CONFIG_PATH.parent == app_module.APP_DIR
    assert DEFAULT_DATA_PATH.parent == app_module.APP_DIR / "data"
