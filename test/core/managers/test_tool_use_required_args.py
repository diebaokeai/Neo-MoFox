"""测试 ToolUse 对必填参数缺失的校验。"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from src.core.components.base.tool import BaseTool
from src.core.managers.tool_manager.tool_use import ToolArgumentError, ToolUse


class RequiredArgTool(BaseTool):
    """声明必填参数 command 的测试工具。"""

    name = "required_arg_tool"
    description = "需要必填参数的工具"

    async def execute(self, command: str) -> tuple[bool, str]:
        """原样返回 command，用于断言参数是否传入。"""
        return True, command


class OptionalArgTool(BaseTool):
    """所有参数都有默认值的测试工具。"""

    name = "optional_arg_tool"
    description = "参数均可省略的工具"

    async def execute(self, command: str = "default") -> tuple[bool, str]:
        """返回 command 或默认值。"""
        return True, command


class KwargsOnlyTool(BaseTool):
    """只接收可变关键字参数的测试工具。"""

    name = "kwargs_only_tool"
    description = "只接收 **kwargs 的工具"

    async def execute(self, **kwargs: object) -> tuple[bool, str]:
        """返回参数个数，验证不会被误判缺参。"""
        return True, str(len(kwargs))


@pytest.fixture()
def manager_with_registry(monkeypatch: pytest.MonkeyPatch) -> ToolUse:
    """构造注入了测试工具的 ToolUse。"""

    class _FakeRegistry:
        def get(self, signature: str):
            mapping = {
                "demo:tool:required_arg_tool": RequiredArgTool,
                "demo:tool:optional_arg_tool": OptionalArgTool,
                "demo:tool:kwargs_only_tool": KwargsOnlyTool,
            }
            return mapping.get(signature)

    monkeypatch.setattr(
        "src.core.managers.tool_manager.tool_use.get_global_registry",
        lambda: _FakeRegistry(),
    )
    return ToolUse()


@pytest.mark.asyncio
async def test_missing_required_argument_raises_actionable_error(
    manager_with_registry: ToolUse,
) -> None:
    """缺少必填参数时应抛出带补参提示的 ToolArgumentError。"""

    with pytest.raises(ToolArgumentError) as exc_info:
        await manager_with_registry.execute_tool(
            "demo:tool:required_arg_tool",
            MagicMock(),
            MagicMock(),
        )

    assert "command" in str(exc_info.value)
    assert "补全参数" in str(exc_info.value)
    # 保持 RuntimeError 语义，兼容既有调用方
    assert isinstance(exc_info.value, RuntimeError)


@pytest.mark.asyncio
async def test_present_required_argument_executes(
    manager_with_registry: ToolUse,
) -> None:
    """参数完整时正常执行。"""

    ok, result = await manager_with_registry.execute_tool(
        "demo:tool:required_arg_tool",
        MagicMock(),
        MagicMock(),
        command="search -topn 3",
    )

    assert ok is True
    assert result == "search -topn 3"


@pytest.mark.asyncio
async def test_optional_arguments_do_not_trigger_validation(
    manager_with_registry: ToolUse,
) -> None:
    """有默认值的参数不参与必填校验。"""

    ok, result = await manager_with_registry.execute_tool(
        "demo:tool:optional_arg_tool",
        MagicMock(),
        MagicMock(),
    )

    assert ok is True
    assert result == "default"


@pytest.mark.asyncio
async def test_kwargs_only_tool_does_not_trigger_validation(
    manager_with_registry: ToolUse,
) -> None:
    """只声明 **kwargs 的工具不应被判定为缺参。"""

    ok, result = await manager_with_registry.execute_tool(
        "demo:tool:kwargs_only_tool",
        MagicMock(),
        MagicMock(),
    )

    assert ok is True
    assert result == "0"
