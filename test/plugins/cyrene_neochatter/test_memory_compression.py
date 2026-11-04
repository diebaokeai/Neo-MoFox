"""Tests for the persistent L2 to L1 compression path."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Callable

from plugins.cyrene_neochatter import user_scope
from plugins.cyrene_neochatter.chatter import CyreneNeoChatter
from plugins.cyrene_neochatter.config import CyreneNeoConfig
from plugins.cyrene_neochatter.memory import empty_store, load_store, save_store
from plugins.cyrene_neochatter.plugin import CyreneNeoPlugin


def _make_chatter(
    tmp_path: Path, every: int = 1
) -> tuple[CyreneNeoChatter, CyreneNeoConfig]:
    cfg = CyreneNeoConfig()
    cfg.memory.data_dir = str(tmp_path / "memory")
    cfg.memory_write.enabled = True
    cfg.memory_write.compress_every_n_rounds = every
    cfg.memory_write.min_interval_seconds = 0
    cfg.memory_write.compress_task = "utils_small"
    plugin = CyreneNeoPlugin(cfg)
    return CyreneNeoChatter("stream-1", plugin), cfg


def _base(cfg: CyreneNeoConfig, user_id: str = "user-1") -> str:
    return str(Path(cfg.memory.data_dir) / "users" / user_id)


def _events(count: int) -> list[dict[str, object]]:
    return [
        {"id": f"event-{index}", "date": "2026-01-01", "summary": f"事件 {index}"}
        for index in range(count)
    ]


def _model() -> dict[str, object]:
    return {
        "api_provider": "openai",
        "base_url": "https://example.invalid/v1",
        "model_identifier": "test-model",
        "api_key": "test-key",
        "client_type": "openai",
        "max_retry": 0,
        "timeout": 20.0,
        "retry_interval": 0.0,
        "price_in": 0.0,
        "cache_hit_price_in": 0.0,
        "price_out": 0.0,
        "temperature": 0.0,
        "max_tokens": 1000,
        "max_context": 8000,
        "tool_call_compat": False,
        "force_stream_mode": False,
        "extra_params": {},
    }


def _install_llm(
    monkeypatch,
    message: str,
    on_send: Callable[[], None] | None = None,
) -> None:
    class FakeResponse:
        def __init__(self, text: str) -> None:
            self.message = text

    class FakeRequest:
        def __init__(self) -> None:
            self.model_set = [_model()]
            self.context_manager = None
            self.payloads: list[object] = []

        def add_payload(self, payload: object) -> None:
            self.payloads.append(payload)

        async def send(self, **_kwargs: object) -> FakeResponse:
            if on_send is not None:
                on_send()
            return FakeResponse(message)

    monkeypatch.setattr(
        "src.app.plugin_system.api.llm_api.get_model_set_by_task",
        lambda _task: [_model()],
    )
    monkeypatch.setattr(
        "src.app.plugin_system.api.llm_api.create_llm_request",
        lambda *_args, **_kwargs: FakeRequest(),
    )


def _forbid_llm(monkeypatch) -> None:
    def _fail(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("LLM must not be called in this scenario")

    monkeypatch.setattr(
        "src.app.plugin_system.api.llm_api.create_llm_request",
        _fail,
    )


async def test_l2_compression_updates_l1_and_consumes_only_old_snapshot(
    tmp_path, monkeypatch
) -> None:
    chatter, cfg = _make_chatter(tmp_path)
    monkeypatch.setattr(user_scope, "resolve_user_id", lambda _stream: _resolved_user())

    data = empty_store()
    data["l1"] = "旧摘要"
    data["l2"] = _events(22)
    data["meta"] = {"memory_success_rounds": 1}
    base = _base(cfg)
    save_store(base, data)

    _install_llm(monkeypatch, "<summary>新的长期摘要</summary>")

    current = load_store(base)
    assert await chatter._compress_l2_to_l1(current) is True

    saved = load_store(base)
    assert saved["l1"] == "新的长期摘要"
    assert len(saved["l2"]) == 20
    assert {item["id"] for item in saved["l2"]} == {f"event-{i}" for i in range(2, 22)}
    assert saved["meta"]["l1_compressed_round"] == 1
    assert saved["meta"]["l1_compressed_l2_watermark"] == 0


async def test_l2_compression_failure_keeps_l1_and_l2(tmp_path, monkeypatch) -> None:
    chatter, cfg = _make_chatter(tmp_path)
    monkeypatch.setattr(user_scope, "resolve_user_id", lambda _stream: _resolved_user())

    data = empty_store()
    data["l1"] = "保留摘要"
    data["l2"] = _events(21)
    data["meta"] = {"memory_success_rounds": 1}
    base = _base(cfg)
    save_store(base, data)

    _install_llm(monkeypatch, "{}")

    current = load_store(base)
    before = json.dumps(current, ensure_ascii=False, sort_keys=True)
    assert await chatter._compress_l2_to_l1(current) is False
    after = json.dumps(load_store(base), ensure_ascii=False, sort_keys=True)
    assert before == after


async def test_l2_compression_timeout_keeps_l1_and_l2(tmp_path, monkeypatch) -> None:
    chatter, cfg = _make_chatter(tmp_path)
    monkeypatch.setattr(user_scope, "resolve_user_id", lambda _stream: _resolved_user())

    data = empty_store()
    data["l1"] = "超时前摘要"
    data["l2"] = _events(21)
    data["meta"] = {"memory_success_rounds": 1}
    base = _base(cfg)
    save_store(base, data)

    def _timeout() -> None:
        raise TimeoutError("compression timeout")

    _install_llm(monkeypatch, "<summary>不应写入</summary>", on_send=_timeout)

    current = load_store(base)
    before = json.dumps(current, ensure_ascii=False, sort_keys=True)
    assert await chatter._compress_l2_to_l1(current) is False
    after = json.dumps(load_store(base), ensure_ascii=False, sort_keys=True)
    assert before == after


async def test_l2_compression_preserves_other_layers_and_new_events(
    tmp_path, monkeypatch
) -> None:
    chatter, cfg = _make_chatter(tmp_path)
    monkeypatch.setattr(user_scope, "resolve_user_id", lambda _stream: _resolved_user())

    data = empty_store()
    data["l1"] = "旧摘要"
    data["l2"] = _events(22)
    data["relationship"] = [{"userText": "关系记录"}]
    data["reflection"] = [{"text": "反思记录"}]
    data["conflict"] = [{"old": "旧", "new": "新"}]
    data["evidence"] = [{"text": "证据"}]
    data["entries"] = [{"id": "entry-1", "text": "语义记忆"}]
    data["meta"] = {"memory_success_rounds": 1}
    base = _base(cfg)
    save_store(base, data)

    def _add_concurrent_event() -> None:
        latest = load_store(base)
        latest["l2"].append({"id": "concurrent", "summary": "压缩期间的新事件"})
        save_store(base, latest)

    _install_llm(
        monkeypatch,
        "<summary>合并后的摘要</summary>",
        on_send=_add_concurrent_event,
    )

    current = load_store(base)
    assert await chatter._compress_l2_to_l1(current) is True

    saved = load_store(base)
    assert saved["l1"] == "合并后的摘要"
    ids = {item["id"] for item in saved["l2"]}
    assert "concurrent" in ids
    assert "event-0" not in ids
    assert "event-1" not in ids
    assert "event-2" in ids
    assert saved["relationship"] == data["relationship"]
    assert saved["reflection"] == data["reflection"]
    assert saved["conflict"] == data["conflict"]
    assert saved["evidence"] == data["evidence"]
    assert saved["entries"] == data["entries"]


async def test_compress_every_zero_disables_l2_compression(tmp_path, monkeypatch) -> None:
    chatter, cfg = _make_chatter(tmp_path, every=0)
    monkeypatch.setattr(user_scope, "resolve_user_id", lambda _stream: _resolved_user())
    _forbid_llm(monkeypatch)

    data = empty_store()
    data["l2"] = _events(21)
    data["meta"] = {"memory_success_rounds": 99}
    base = _base(cfg)
    save_store(base, data)

    assert await chatter._compress_l2_to_l1(data) is False
    assert len(load_store(base)["l2"]) == 21


async def test_compression_waits_for_persisted_round_threshold(
    tmp_path, monkeypatch
) -> None:
    chatter, cfg = _make_chatter(tmp_path, every=5)
    monkeypatch.setattr(user_scope, "resolve_user_id", lambda _stream: _resolved_user())
    _forbid_llm(monkeypatch)

    data = empty_store()
    data["l2"] = _events(21)
    data["meta"] = {"memory_success_rounds": 3, "l1_compressed_round": 0}
    base = _base(cfg)
    save_store(base, data)

    assert await chatter._compress_l2_to_l1(data) is False


async def test_no_snapshot_does_not_call_llm(tmp_path, monkeypatch) -> None:
    chatter, cfg = _make_chatter(tmp_path)
    monkeypatch.setattr(user_scope, "resolve_user_id", lambda _stream: _resolved_user())
    _forbid_llm(monkeypatch)

    data = empty_store()
    data["l2"] = _events(5)
    data["meta"] = {"memory_success_rounds": 1}
    base = _base(cfg)
    save_store(base, data)

    assert await chatter._compress_l2_to_l1(data) is False


async def test_round_counter_survives_new_chatter_instance(tmp_path, monkeypatch) -> None:
    chatter, cfg = _make_chatter(tmp_path, every=2)
    monkeypatch.setattr(user_scope, "resolve_user_id", lambda _stream: _resolved_user())

    data = empty_store()
    base = _base(cfg)
    save_store(base, data)

    assert await chatter._record_memory_round(data) == 1
    assert await chatter._record_memory_round(data) == 2

    persisted = load_store(base)
    persisted["l2"] = _events(22)
    save_store(base, persisted)

    restarted = CyreneNeoChatter("stream-1", CyreneNeoPlugin(cfg))
    _install_llm(monkeypatch, "<summary>重启后的摘要</summary>")
    assert await restarted._compress_l2_to_l1(load_store(base)) is True
    assert load_store(base)["l1"] == "重启后的摘要"


async def test_shared_scope_is_not_l2_compressed(tmp_path, monkeypatch) -> None:
    chatter, cfg = _make_chatter(tmp_path)
    monkeypatch.setattr(user_scope, "resolve_user_id", lambda _stream: _unknown_user())
    _forbid_llm(monkeypatch)

    data = empty_store()
    data["l2"] = _events(21)
    data["meta"] = {"memory_success_rounds": 1}

    assert await chatter._compress_l2_to_l1(data) is False
    assert data["l1"] == ""


async def _resolved_user() -> str:
    return "user-1"


async def _unknown_user() -> str:
    return ""
