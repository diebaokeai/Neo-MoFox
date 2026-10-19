"""正文文本策略：剥离内联 emoji/贴纸式符号。

cyrene 系人格要求「文字里不出现 emoji，情绪图片走专门的图片通道
（emoji_sender / send_emoji_meme 等）」。本模块提供统一剥离函数，
供各 Chatter 与 send_text 动作在发送前强制清洗，避免模型把 emoji
内联进文本消息。
"""

from __future__ import annotations

import re


def _chr_range(start: int, end: int) -> str:
    """构造正则字符区间片段（用 chr 生成，避免源码里的反斜杠转义）。"""
    return chr(start) + "-" + chr(end)


# 常见 Unicode emoji 区间（表情符号、装饰、杂项符号、箭头、旗帜等）。
# 注意：只匹配字形本身；ASCII 颜文字（XD、(・ω・) 等）由人格提示约束，
# 这里不做硬剥离以免误伤正文符号。
_EMOJI_SYMBOLS = (
    _chr_range(0x1F000, 0x1FAFF)   # 表情符号与扩展象形（含大部分 ZWJ 序列成员）
    + _chr_range(0x1F1E6, 0x1F1FF)  # 区域指示符（旗帜）
    + _chr_range(0x2600, 0x27BF)  # 杂项符号/装饰符号/表情符号
    + _chr_range(0x2B00, 0x2BFF)  # 杂项符号和箭头（星星、方向等 emoji 字形）
    + _chr_range(0x2190, 0x21FF)  # 箭头
    + chr(0xFE0E) + chr(0xFE0F)   # 变体选择符（文本/表情呈现）
    + chr(0x200D)                 # ZWJ（组合序列成员）
    + chr(0x20E3)                 # 键帽
)

_EMOJI_RE = re.compile("[" + _EMOJI_SYMBOLS + "]+")

# 人设允许的装饰性符号（如昔涟的「♪」「♫」），不当作 emoji 剥离
_KEEP_RE = re.compile("[" + chr(0x266A) + chr(0x266B) + "]")


_ELEMENT_RE = re.compile("<[^<>]+>[^<>]*?</[^<>]+>", re.DOTALL)
_TAG_ONLY_RE = re.compile("<[^<>]+>")


def _cut_trailing_paren_group(text: str) -> tuple[str, bool]:
    """去掉结尾的整对中文括号「（…）」，返回 (新文本, 是否有删减)。"""
    t = text.rstrip()
    if not t.endswith("）"):
        return text, False
    open_idx = t.rfind("（")
    if open_idx < 0 or open_idx == len(t) - 1:
        return text, False
    inner = t[open_idx + 1:-1]
    if "（" in inner or "）" in inner:
        return text, False
    if len(inner) > 80 or "://" in inner or "http" in inner:
        return text, False
    if not inner.strip():
        return text, False
    return t[:open_idx].rstrip(), True


def _cut_leading_paren_group(text: str) -> tuple[str, bool]:
    """去掉开头的整对中文括号「（…）」，返回 (新文本, 是否有删减)。"""
    t = text.lstrip()
    if not t.startswith("（"):
        return text, False
    close_idx = t.find("）")
    if close_idx <= 0:
        return text, False
    inner = t[1:close_idx]
    if "（" in inner or "）" in inner:
        return text, False
    if len(inner) > 80 or "://" in inner or "http" in inner:
        return text, False
    if not inner.strip():
        return text, False
    return t[close_idx + 1:].lstrip(), True


def strip_stage_parens(text: object) -> str:
    """去掉位于句首/句尾的整对中文括号旁白（如「（虽然这么说，但…）」）。

    只处理整句边界上的全角括号组，避免误伤正文里的半角括号与说明性内容；
    多个连续（如 正文（a）（b）结尾）会逐个裁掉。
    """
    if not text:
        return str(text or "")
    s = str(text)
    for _ in range(4):
        s, changed1 = _cut_trailing_paren_group(s)
        s, changed2 = _cut_leading_paren_group(s)
        if not changed1 and not changed2:
            break
    return s.strip()


def strip_tool_markup(text: object) -> str:
    """剥离模型误当作工具调用输出的尖括号标记（如 <invoke name=...>）。

    这类内容不是给人看的正文（本仓库没有 send_sticker/tx-sticker 等动作），
    成对元素连同内部内容一起删除，孤立标记单独删除，避免把工具语法发给用户；
    移除后若为空则不应发送。
    """
    if not text:
        return str(text or "")
    cleaned = _ELEMENT_RE.sub(" ", str(text))
    cleaned = _TAG_ONLY_RE.sub(" ", cleaned)
    return _collapse_hspace(cleaned).strip()


def contains_emoji(text: object) -> bool:
    """判断文本是否包含内联 emoji 字形（人设允许的 ♪/♫ 不计）。"""
    if not text:
        return False
    return _EMOJI_RE.search(_KEEP_RE.sub("", str(text))) is not None


def _collapse_hspace(text: str) -> str:
    """按行压缩多余空格/Tab（保留换行），用于删除 emoji 后的排版整理。"""
    separator = chr(10)
    return separator.join(" ".join(line.split()) for line in text.split(separator))


def strip_emoji(text: object) -> str:
    """剥离文本中的 emoji 字形并整理多余空白；无 emoji 时原样返回。

    人设允许的装饰符号（♪/♫）先暂存再还原，不被剥离。
    """
    if not text:
        return str(text or "")
    source = str(text)
    saved: list[str] = []

    def _stash(match: "re.Match[str]") -> str:
        saved.append(match.group(0))
        return chr(0xE000 + len(saved) - 1)

    protected = _KEEP_RE.sub(_stash, source)
    cleaned = _EMOJI_RE.sub("", protected)
    for index, value in enumerate(saved):
        cleaned = cleaned.replace(chr(0xE000 + index), value)
    return _collapse_hspace(cleaned).strip()
