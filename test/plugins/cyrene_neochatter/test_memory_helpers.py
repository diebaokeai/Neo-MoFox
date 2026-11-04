"""Tests for cyrene_neochatter memory compression helpers."""

from __future__ import annotations

from plugins.cyrene_neochatter.chatter import _extract_memory_summary
from plugins.cyrene_neochatter.memory import (
    ensure_l2_event_ids,
    empty_store,
    select_l2_compression_snapshot,
)


def test_ensure_l2_event_ids_is_stable_after_first_repair() -> None:
    data = empty_store()
    data["l2"] = [{"date": "2026-01-01", "summary": "old"}, {"id": "fixed", "summary": "new"}]

    assert ensure_l2_event_ids(data) is True
    first_ids = [item["id"] for item in data["l2"]]
    assert first_ids[0].startswith("l2_")
    assert first_ids[1] == "fixed"
    assert ensure_l2_event_ids(data) is False
    assert [item["id"] for item in data["l2"]] == first_ids


def test_ensure_l2_event_ids_repairs_duplicate_ids() -> None:
    data = empty_store()
    data["l2"] = [{"id": "dup", "summary": "first"}, {"id": "dup", "summary": "second"}]

    assert ensure_l2_event_ids(data) is True
    first, second = (item["id"] for item in data["l2"])
    assert first == "dup"
    assert second != "dup"


def test_select_l2_snapshot_respects_watermark_and_recent_events() -> None:
    data = empty_store()
    data["l2"] = [{"id": str(index), "summary": f"event-{index}"} for index in range(8)]

    snapshot, watermark = select_l2_compression_snapshot(
        data,
        watermark=1,
        keep_recent=2,
        max_events=3,
    )

    assert [item["id"] for item in snapshot] == ["1", "2", "3"]
    assert watermark == 4


def test_select_l2_snapshot_does_not_consume_recent_events() -> None:
    data = empty_store()
    data["l2"] = [{"id": str(index), "summary": f"event-{index}"} for index in range(5)]

    snapshot, watermark = select_l2_compression_snapshot(data, keep_recent=3)

    assert [item["id"] for item in snapshot] == ["0", "1"]
    assert watermark == 2


def test_extract_memory_summary_rejects_json_but_keeps_summary() -> None:
    # 思考/分析段会被剥离，之后仍能取到 summary
    assert _extract_memory_summary("<analysis>do not save</analysis><summary>bad</summary>") == "bad"
    assert _extract_memory_summary('{"summary": "not a plain summary"}') == ""


def test_extract_memory_summary_accepts_wrapped_plain_text() -> None:
    assert _extract_memory_summary("<summary>长期偏好：喜欢安静。</summary>") == "长期偏好：喜欢安静。"
    assert (
        _extract_memory_summary("```xml\n<summary>代码围栏内的摘要</summary>\n```")
        == "代码围栏内的摘要"
    )


def test_extract_memory_summary_falls_back_to_plain_text() -> None:
    """缺少 summary 节点时接受纯文本兜底，避免压缩长期失败。"""
    assert _extract_memory_summary("先分析一下：这段对话讲了长期偏好。") == "先分析一下：这段对话讲了长期偏好。"
