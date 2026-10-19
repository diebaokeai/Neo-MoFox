"""bilibili_queue 弹幕文本解析器（KeywordMatcher）的单测。"""

from __future__ import annotations

import pytest

from plugins.bilibili_queue.core.keywords import (
    ADMIN_COMMANDS,
    Intent,
    KeywordMatcher,
    looks_like_queue_name,
)


@pytest.fixture
def matcher() -> KeywordMatcher:
    """默认关键词表（与插件配置默认值一致）。"""

    return KeywordMatcher(
        join=["排队", "排个队", "上车", "报名"],
        leave=["退队", "下车", "取消排队", "退出排队"],
        status=["排队情况", "排队查询", "查询排队", "队列状态", "排到哪了"],
    )


# ── 入队 ──────────────────────────────────────────────


def test_plain_join_keyword_uses_default_queue(matcher: KeywordMatcher) -> None:
    """只发关键词时不带队名。"""

    intent = matcher.parse("排队")
    assert intent.intent is Intent.JOIN
    assert intent.keyword == "排队"
    assert intent.queue == ""
    assert intent.is_actionable is True


@pytest.mark.parametrize(
    ("text", "expected_queue"),
    [
        ("排队 排位", "排位"),
        ("排队：排位", "排位"),
        ("排队:排位", "排位"),
        ("排队，排位", "排位"),
        ("排队、排位", "排位"),
        ("排队-排位", "排位"),
        ("排队\u3000排位", "排位"),
        ("上车 娱乐", "娱乐"),
        ("排队排位", "排位"),
    ],
)
def test_join_with_queue_name(
    matcher: KeywordMatcher, text: str, expected_queue: str
) -> None:
    """关键词后面接队名（各种分隔符）都要能解析出来。"""

    intent = matcher.parse(text)
    assert intent.intent is Intent.JOIN
    assert intent.queue == expected_queue


def test_separator_presence_is_reported(matcher: KeywordMatcher) -> None:
    """``queue_explicit`` 用来区分「排队 排位」和「排队一下」。"""

    assert matcher.parse("排队 排位").queue_explicit is True
    assert matcher.parse("排队排位").queue_explicit is False


def test_join_keyword_inside_chatter_is_ignored(matcher: KeywordMatcher) -> None:
    """关键词不在开头就不算排队。"""

    assert matcher.parse("我也要排队").intent is Intent.NONE


def test_chatter_leftover_is_marked_inexplicit(matcher: KeywordMatcher) -> None:
    """「排队的人好多啊」这种闲聊，解析层会把剩余文本标成「不明确」。

    解析层只做语法切分；是否真的当队名用，由 ``QueueRuntime.resolve_intent_queue``
    结合「有没有分隔符 + 这个队是否存在」决定（对应 runtime 测试）。
    """

    intent = matcher.parse("排队的人好多啊")
    assert intent.intent is Intent.JOIN
    assert intent.queue == "的人好多啊"
    assert intent.queue_explicit is False


def test_queue_name_with_space_is_rejected(matcher: KeywordMatcher) -> None:
    """队名里带空白时解析层直接丢掉，避免出现带空格的怪队名。"""

    assert matcher.parse("排队 a b c").queue == ""


# ── 退队 ──────────────────────────────────────────────


def test_leave_without_queue(matcher: KeywordMatcher) -> None:
    """退队关键词。"""

    intent = matcher.parse("退队")
    assert intent.intent is Intent.LEAVE
    assert intent.queue == ""


def test_leave_with_queue(matcher: KeywordMatcher) -> None:
    """退队并指定队名。"""

    intent = matcher.parse("下车 排位")
    assert intent.intent is Intent.LEAVE
    assert intent.queue == "排位"


# ── 状态查询 ──────────────────────────────────────────


def test_status_keyword_wins_over_join_prefix(matcher: KeywordMatcher) -> None:
    """最长优先：「排队情况」不能被入队关键词「排队」抢走。"""

    intent = matcher.parse("排队情况")
    assert intent.intent is Intent.STATUS
    assert intent.queue == ""


def test_status_variants(matcher: KeywordMatcher) -> None:
    """其他状态关键词也要命中。"""

    for word in ("排队查询", "查询排队", "队列状态", "排到哪了"):
        assert matcher.parse(word).intent is Intent.STATUS


# ── 房管命令 ──────────────────────────────────────────


@pytest.mark.parametrize(
    ("text", "command"),
    [
        ("下一个", "next"),
        ("下一位", "next"),
        ("叫号", "next"),
        ("结束", "finish"),
        ("结束当前", "finish"),
        ("放回", "requeue"),
        ("清空", "clear"),
        ("清空队列", "clear"),
        ("暂停", "pause"),
        ("暂停排队", "pause"),
        ("恢复", "resume"),
        ("恢复排队", "resume"),
        ("开队 排位", "create"),
        ("删队 排位", "delete"),
        ("查看队列", "list"),
    ],
)
def test_admin_commands(matcher: KeywordMatcher, text: str, command: str) -> None:
    """内置房管命令表要能正确命中。"""

    intent = matcher.parse(text)
    assert intent.intent is Intent.ADMIN
    assert intent.command == command


def test_admin_command_with_queue_argument(matcher: KeywordMatcher) -> None:
    """房管命令后面可以带队名。"""

    intent = matcher.parse("下一个 排位")
    assert intent.intent is Intent.ADMIN
    assert intent.command == "next"
    assert intent.queue == "排位"


def test_admin_longest_match_for_pause_and_resume(matcher: KeywordMatcher) -> None:
    """「暂停排队」不会被更短的「暂停」抢先，但效果一致。"""

    assert matcher.parse("暂停排队").command == "pause"
    assert matcher.parse("恢复排队").command == "resume"


def test_admin_commands_table_is_overrideable() -> None:
    """可以完全替换房管命令表。"""

    matcher = KeywordMatcher(join=["排队"], admin_commands={"next": ("开饭",)})
    intent = matcher.parse("开饭")
    assert intent.intent is Intent.ADMIN
    assert intent.command == "next"


def test_builtin_admin_table_not_empty() -> None:
    """内置命令表至少覆盖这几个常用动作。"""

    for command in ("next", "finish", "requeue", "clear", "pause", "resume", "list"):
        assert command in ADMIN_COMMANDS
        assert ADMIN_COMMANDS[command]


# ── 前缀与大小写 ──────────────────────────────────────


def test_command_prefix_required_for_admin() -> None:
    """配了前缀后，不带前缀的弹幕不再命中房管命令。"""

    matcher = KeywordMatcher(join=["排队"], prefix="姬姬")
    assert matcher.parse("下一个").intent is Intent.NONE
    assert matcher.parse("姬姬下一个").intent is Intent.ADMIN


def test_command_prefix_also_applies_to_join() -> None:
    """前缀对入队关键词同样生效（保持一致的心智模型）。"""

    matcher = KeywordMatcher(join=["排队"], prefix="#")
    assert matcher.parse("排队").intent is Intent.NONE
    assert matcher.parse("#排队").intent is Intent.JOIN


def test_ignore_case_for_english_keywords() -> None:
    """英文关键词默认忽略大小写。"""

    matcher = KeywordMatcher(join=["QUEUE"])
    assert matcher.parse("queue 排位").intent is Intent.JOIN

    strict = KeywordMatcher(join=["QUEUE"], ignore_case=False)
    assert strict.parse("queue").intent is Intent.NONE
    assert strict.parse("QUEUE").intent is Intent.JOIN


# ── 边界 ──────────────────────────────────────────────


def test_blank_and_noise_text_is_none(matcher: KeywordMatcher) -> None:
    """空文本、纯空白、无关闲聊都不产生意图。"""

    assert matcher.parse("").intent is Intent.NONE
    assert matcher.parse("   ").intent is Intent.NONE
    assert matcher.parse("主播好可爱").intent is Intent.NONE


def test_none_like_input_does_not_crash(matcher: KeywordMatcher) -> None:
    """None 输入按空处理，不抛异常。"""

    assert matcher.parse(None).intent is Intent.NONE  # type: ignore[arg-type]


def test_parsed_intent_keeps_original_text(matcher: KeywordMatcher) -> None:
    """解析结果里保留原始文本，便于日志排查。"""

    intent = matcher.parse("  排队 排位  ")
    assert intent.text == "排队 排位"


def test_join_words_property_lists_join_keywords() -> None:
    """``join_words`` 只列出入队关键词，供提示文案复用。"""

    matcher = KeywordMatcher(join=["排队", "上车"], leave=["退队"])
    assert set(matcher.join_words) == {"排队", "上车"}


def test_empty_keyword_entries_are_skipped() -> None:
    """关键词表里的空串会被忽略，不会变成「所有弹幕都命中」。"""

    matcher = KeywordMatcher(join=["", "排队"])
    assert matcher.parse("随便说点什么").intent is Intent.NONE


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("排位", True),
        ("a" * 16, True),
        ("a" * 17, False),
        ("", False),
        ("带 空格", False),
    ],
)
def test_looks_like_queue_name(text: str, expected: bool) -> None:
    """队名合法性判断与调度器的约束保持一致。"""

    assert looks_like_queue_name(text) is expected
