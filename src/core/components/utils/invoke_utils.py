"""组件调用辅助工具。

本模块提供调用前参数预处理函数，用于安全处理 LLM 自动注入参数。
"""

from __future__ import annotations

import inspect
from collections.abc import Callable
from typing import Any


def should_strip_auto_reason_argument(
    execute_func: Callable[..., Any],
    kwargs: dict[str, Any],
) -> bool:
    """判断是否应剥离自动注入的 ``reason`` 参数。

    规则：
    1. 若调用参数中没有 ``reason``，不剥离。
    2. 若 ``execute`` 显式声明了 ``reason`` 参数，不剥离。
    3. 若 ``execute`` 含 ``**kwargs``，可兼容 ``reason``，不剥离。
    4. 其余情况视为自动注入，剥离 ``reason`` 以避免签名不匹配。

    Args:
        execute_func: 目标组件的 execute 可调用对象。
        kwargs: 即将传递给 execute 的参数字典。

    Returns:
        bool: True 表示应剥离 ``reason``，False 表示应保留。
    """
    if "reason" not in kwargs:
        return False

    try:
        sig = inspect.signature(execute_func)
    except (TypeError, ValueError):
        # 无法可靠分析签名时，保持兼容旧行为，优先避免调用时报错。
        return True

    for param in sig.parameters.values():
        if param.kind == inspect.Parameter.VAR_KEYWORD:
            return False

    return "reason" not in sig.parameters


def filter_unsupported_kwargs(
    execute_func: Callable[..., Any],
    kwargs: dict[str, Any],
) -> list[str]:
    """丢弃 execute 签名不接受的参数（例如模型幻觉出的多余字段）。

    规则：
    1. execute 含 **kwargs 时全部保留（返回空列表）。
    2. 无法分析签名时全部保留，避免误删导致缺参报错。
    3. 其余情况原地删除签名里没有的键，返回被丢弃的参数名列表。

    与 should_strip_auto_reason_argument 的区别：后者只处理框架自动注入的
    reason；本函数处理 LLM 在 tool call 里多给的任意字段，避免
    TypeError: execute() got an unexpected keyword argument 直接打断调用。

    Args:
        execute_func: 目标组件的 execute 可调用对象。
        kwargs: 即将传递给 execute 的参数字典（会被原地修改）。

    Returns:
        list[str]: 被丢弃的参数名（保持原顺序）。
    """
    if not kwargs:
        return []

    try:
        sig = inspect.signature(execute_func)
    except (TypeError, ValueError):
        return []

    for param in sig.parameters.values():
        if param.kind == inspect.Parameter.VAR_KEYWORD:
            return []

    accepted = {
        name
        for name, param in sig.parameters.items()
        if param.kind
        in (inspect.Parameter.POSITIONAL_OR_KEYWORD, inspect.Parameter.KEYWORD_ONLY)
    }
    dropped = [key for key in kwargs if key not in accepted]
    for key in dropped:
        kwargs.pop(key, None)
    return dropped
