"""Tool invocation implementation.

本模块提供 ToolUse 类，负责 Tool 组件的执行、历史记录管理和结果缓存。
"""

import inspect
import time
import json
from typing import Any, TYPE_CHECKING

from src.kernel.logger import get_logger
from src.kernel.telemetry import TelemetryEventRecord, get_telemetry_collector
from src.core.components.registry import get_global_registry
from src.core.components.utils import should_strip_auto_reason_argument

if TYPE_CHECKING:
    from src.core.components.base.plugin import BasePlugin
    from src.core.managers.tool_manager.tool_history import ToolHistory
    from src.core.models.message import Message

logger = get_logger("tool_use")


class ToolArgumentError(RuntimeError):
    """工具调用缺少必填参数。

    Attributes:
        tool_name: 触发错误的工具名称。
        missing: 缺失的参数名列表。
    """

    def __init__(self, tool_name: str, missing: list[str]) -> None:
        """初始化异常，并生成面向模型的补参提示。

        Args:
            tool_name: 工具名称。
            missing: 缺失的必填参数名列表。
        """
        self.tool_name = tool_name
        self.missing = missing
        names = "、".join(missing)
        super().__init__(
            f"调用工具 {tool_name} 缺少必要参数【{names}】。请补全参数后重新调用。"
        )


def _required_argument_names(tool_instance: Any) -> list[str]:
    """读取工具声明中必填的参数名。

    优先使用 ``to_schema()`` 的 ``required`` 列表（与模型看到的定义一致），
    读取失败时回退到 ``execute`` 签名中无默认值的参数。

    Args:
        tool_instance: 工具实例。

    Returns:
        list[str]: 必填参数名列表；无法判断时返回空列表。
    """
    schema_getter = getattr(tool_instance, "to_schema", None)
    if callable(schema_getter):
        try:
            schema = schema_getter()
            parameters = schema.get("function", {}).get("parameters", {})
            required = parameters.get("required", [])
            if required:
                return [str(name) for name in required]
        except Exception:
            # schema 读取失败时回退到签名检查，不影响正常执行
            pass

    try:
        signature = inspect.signature(tool_instance.execute)
    except (TypeError, ValueError):
        return []

    required_names: list[str] = []
    for name, parameter in signature.parameters.items():
        if name == "self":
            continue
        if parameter.kind in (
            inspect.Parameter.VAR_POSITIONAL,
            inspect.Parameter.VAR_KEYWORD,
        ):
            continue
        if parameter.default is inspect.Parameter.empty:
            required_names.append(name)
    return required_names


def _missing_required_arguments(
    tool_instance: Any,
    kwargs: dict[str, Any],
) -> list[str]:
    """返回 ``kwargs`` 中缺失的必填参数名。"""
    return [
        name
        for name in _required_argument_names(tool_instance)
        if name not in kwargs
    ]


async def _record_tool_telemetry_event(
    *,
    signature: str,
    tool_name: str,
    execution_time: float,
    cache_hit: bool,
    status: str,
    arg_count: int,
    auto_reason_stripped: bool,
    error: Exception | None = None,
) -> None:
    """记录工具调用摘要遥测。"""
    collector = get_telemetry_collector()
    if not collector.is_domain_enabled("tool"):
        return

    attributes: dict[str, Any] = {
        "signature": signature,
        "tool_name": tool_name,
        "duration_ms": round(execution_time * 1000, 3),
        "cache_hit": cache_hit,
        "status": status,
        "arg_count": arg_count,
        "auto_reason_stripped": auto_reason_stripped,
    }
    if error is not None:
        attributes["error_type"] = type(error).__name__

    await collector.record(
        TelemetryEventRecord(
            domain="tool",
            event_name="tool_invoked",
            severity="info" if error is None and status == "success" else "error",
            summary="tool invoked" if error is None else "tool invocation failed",
            entity_id=signature,
            attributes=attributes,
            detail={"message": str(error)} if error is not None else None,
        )
    )


class ToolUse:
    """Tool 调用管理器。

    负责 Tool 组件的执行、历史记录管理和结果缓存。
    类似于 action_manager 的 execute_action，但专门用于 Tool 组件。
    支持 ToolHistory 集成和结果缓存机制。

    Attributes:
        _tool_history: 工具调用历史记录器
        _tool_cache: 结果缓存字典（可选）

    Examples:
        >>> tool_use = ToolUse()
        >>> success, result = await tool_use.execute_tool(
        ...     "my_plugin:tool:calculator",
        ...     plugin,
        ...     message,
        ...     expression="2+2"
        ... )
    """

    def __init__(self) -> None:
        """初始化 Tool 调用管理器。"""
        # 导入并初始化 ToolHistory
        from .tool_history import ToolHistory

        self._tool_history = ToolHistory()
        self._cache_enabled: bool = False
        logger.debug("Tool 调用管理器初始化完成")

    async def execute_tool(
        self,
        signature: str,
        plugin: "BasePlugin",
        message: "Message",
        **kwargs: Any,
    ) -> tuple[bool, Any]:
        """执行 Tool 并记录历史。

        创建 Tool 实例并调用其 execute 方法，同时记录执行历史。
        支持结果缓存机制，避免重复调用相同参数的工具。

        Args:
            signature: Tool 组件签名
            plugin: 所属插件实例
            message: 触发的消息
            **kwargs: 传递给 execute 方法的参数

        Returns:
            tuple[bool, Any]: (是否成功, 返回结果)

        Raises:
            ValueError: 如果 Tool 类未找到
            RuntimeError: 如果 Tool 执行失败

        Examples:
            >>> success, result = await tool_use.execute_tool(
            ...     "my_plugin:tool:calculator",
            ...     plugin,
            ...     message,
            ...     expression="2+2"
            ... )
            >>> True, "4"
        """
        start_time = time.time()
        stripped_auto_reason = False

        # 从注册表获取 Tool 类
        registry = get_global_registry()
        tool_cls = registry.get(signature)

        from src.core.components.base.tool import BaseTool

        if not tool_cls or not issubclass(tool_cls, BaseTool):
            raise ValueError(f"Tool 类未找到: {signature}")

        # 创建 Tool 实例
        tool_instance = tool_cls(plugin)

        # 检查缓存
        cached_result: dict[str, Any] | None = None
        if self._cache_enabled:
            args_dict = kwargs.copy()
            cached_result = self._tool_history.get_cached(
                tool_instance.name,
                args_dict
            )

            if cached_result is not None:
                # 记录缓存的调用
                execution_time = time.time() - start_time
                self._tool_history.add_call(
                    tool_name=tool_instance.name,
                    args=args_dict,
                    result=cached_result,
                    execution_time=execution_time,
                    cache_hit=True
                )
                await _record_tool_telemetry_event(
                    signature=signature,
                    tool_name=tool_instance.name,
                    execution_time=execution_time,
                    cache_hit=True,
                    status="success",
                    arg_count=len(args_dict),
                    auto_reason_stripped=False,
                )
                logger.debug(f"工具执行缓存命中: {tool_instance.name}")
                return True, cached_result

        # 执行 Tool
        try:
            # 仅剥离系统自动注入的 reason；组件原生声明 reason 时必须保留。
            if should_strip_auto_reason_argument(tool_instance.execute, kwargs):
                kwargs.pop("reason", None)
                stripped_auto_reason = True

            # 记录开始执行
            logger.debug(f"开始执行工具: {tool_instance.name}, 参数: {kwargs}")

            # 触发工具调用执行前事件
            try:
                from src.core.components.types import EventType
                from src.kernel.event import get_event_bus

                event_bus = get_event_bus()
                if event_bus.get_subscribers(EventType.BEFORE_TOOL_CALL):
                    _, modified_params = await event_bus.publish(
                        EventType.BEFORE_TOOL_CALL,
                        {
                            "signature": signature,
                            "tool_name": tool_instance.name,
                            "tool_description": getattr(
                                tool_instance, "description", ""
                            ),
                            "args": dict(kwargs),
                            "message": message,
                        },
                    )
                    # 应用事件处理器对参数的修改
                    if "args" in modified_params:
                        new_args = modified_params["args"]
                        if isinstance(new_args, dict):
                            kwargs = new_args
                    if "message" in modified_params:
                        modified_message = modified_params["message"]
                        if modified_message is not None:
                            message = modified_message
            except Exception:
                # 事件触发失败不中断工具执行，静默降级
                pass

            # 调用前校验必填参数：模型偶发返回空参数（如 {}），
            # 直接调用会抛难以自愈的 TypeError，这里转成明确的补参提示。
            missing_arguments = _missing_required_arguments(tool_instance, kwargs)
            if missing_arguments:
                raise ToolArgumentError(tool_instance.name, missing_arguments)

            # 调用 Tool 的 execute 方法
            from src.kernel.llm.usable_execution import execute_usable_instance

            success, result = await execute_usable_instance(tool_instance, kwargs=kwargs)

            # 格式化结果（确保是字典形式以兼容 ToolHistory）
            if isinstance(result, str):
                formatted_result = {"content": result}
            elif isinstance(result, dict):
                formatted_result = result
            else:
                formatted_result = {"content": str(result)}

            # 计算执行时间
            execution_time = time.time() - start_time

            # 记录到历史
            self._tool_history.add_call(
                tool_name=tool_instance.name,
                args={k: v for k, v in kwargs.items()},
                result=formatted_result,
                status="success" if success else "error",
                error_message=None if success else str(result),
                execution_time=execution_time,
                cache_hit=False
            )

            # 如果成功且开启了缓存，则缓存结果
            if success and self._cache_enabled:
                self._tool_history.cache_result(
                    tool_name=tool_instance.name,
                    args={k: v for k, v in kwargs.items()},
                    result=formatted_result
                )
                logger.debug(f"工具结果已缓存: {tool_instance.name}")

            # 记录执行完成
            status_emoji = "✅" if success else "❌"
            logger.info(f"{status_emoji} 工具执行完成: {tool_instance.name}, 耗时: {execution_time:.2f}s")

            await _record_tool_telemetry_event(
                signature=signature,
                tool_name=tool_instance.name,
                execution_time=execution_time,
                cache_hit=False,
                status="success" if success else "error",
                arg_count=len(kwargs),
                auto_reason_stripped=stripped_auto_reason,
                error=None if success else RuntimeError(str(result)),
            )

            # 触发工具调用执行后事件
            try:
                from src.core.components.types import EventType
                from src.kernel.event import get_event_bus

                event_bus = get_event_bus()
                if event_bus.get_subscribers(EventType.AFTER_TOOL_CALL):
                    _, modified_params = await event_bus.publish(
                        EventType.AFTER_TOOL_CALL,
                        {
                            "signature": signature,
                            "tool_name": tool_instance.name,
                            "tool_description": getattr(
                                tool_instance, "description", ""
                            ),
                            "args": dict(kwargs),
                            "result": result,
                            "success": success,
                            "execution_time": execution_time,
                            "message": message,
                        },
                    )
                    # 应用事件处理器对结果和消息的修改
                    if "result" in modified_params:
                        new_result = modified_params["result"]
                        if new_result is not None:
                            result = new_result
            except Exception:
                # 事件触发失败不中断工具执行，静默降级
                pass

            return success, result

        except Exception as e:
            # 记录异常
            execution_time = time.time() - start_time
            error_result = {"content": f"工具执行失败: {str(e)}"}

            self._tool_history.add_call(
                tool_name=tool_instance.name,
                args={k: v for k, v in kwargs.items()},
                result=error_result,
                status="error",
                error_message=str(e),
                execution_time=execution_time,
                cache_hit=False
            )

            await _record_tool_telemetry_event(
                signature=signature,
                tool_name=tool_instance.name,
                execution_time=execution_time,
                cache_hit=False,
                status="error",
                arg_count=len(kwargs),
                auto_reason_stripped=stripped_auto_reason,
                error=e,
            )

            # 触发工具调用失败事件
            try:
                from src.core.components.types import EventType
                from src.kernel.event import get_event_bus

                event_bus = get_event_bus()
                if event_bus.get_subscribers(EventType.ON_TOOL_CALL_FAILED):
                    await event_bus.publish(
                        EventType.ON_TOOL_CALL_FAILED,
                        {
                            "signature": signature,
                            "tool_name": tool_instance.name,
                            "tool_description": getattr(
                                tool_instance, "description", ""
                            ),
                            "args": dict(kwargs),
                            "error": e,
                            "error_type": type(e).__name__,
                            "error_message": str(e),
                            "execution_time": execution_time,
                            "message": message,
                        },
                    )
            except Exception:
                # 事件触发失败不中断异常传播，静默降级
                pass

            if isinstance(e, ToolArgumentError):
                logger.warning(f"工具参数缺失 ({tool_instance.name}): {e}")
                raise
            logger.error(f"工具执行失败 ({tool_instance.name}): {e}")
            raise RuntimeError(f"Tool 执行失败: {e}") from e

    def get_tool_history(self) -> "ToolHistory":
        """获取工具调用历史记录器。

        Returns:
            ToolHistory: 工具调用历史记录器实例

        Examples:
            >>> history = tool_use.get_tool_history()
            >>> records = history.get_recent_history(count=10)
        """
        return self._tool_history

    def enable_caching(self, enabled: bool = True) -> None:
        """启用或禁用结果缓存。

        Args:
            enabled: 是否启用缓存

        Examples:
            >>> tool_use.enable_caching(True)
        """
        if self._cache_enabled != enabled:
            self._cache_enabled = enabled
            status = "启用" if enabled else "禁用"
            logger.debug(f"工具结果缓存已{status}")

    def clear_cache(self) -> None:
        """清除所有缓存。

        Examples:
            >>> tool_use.clear_cache()
        """
        self._tool_history.clear_cache()
        logger.debug("工具缓存已清除")

    def get_cache_stats(self) -> dict[str, Any]:
        """获取缓存统计信息。

        Returns:
            dict[str, Any]: 缓存统计信息

        Examples:
            >>> stats = tool_use.get_cache_stats()
        """
        return self._tool_history.get_stats()

    def _build_cache_key(self, tool_name: str, args: dict[str, Any]) -> str:
        """构建缓存键。

        Args:
            tool_name: 工具名称
            args: 调用参数

        Returns:
            str: 缓存键
        """
        args_str = json.dumps(args, sort_keys=True)
        return f"{tool_name}:{args_str}"


# 全局 Tool 调用管理器实例
_global_tool_use: ToolUse | None = None


def get_tool_use() -> ToolUse:
    """获取全局 Tool 调用管理器实例。

    Returns:
        ToolUse: 全局 Tool 调用管理器单例

    Examples:
        >>> tool_use = get_tool_use()
        >>> success, result = await tool_use.execute_tool(...)
    """
    global _global_tool_use
    if _global_tool_use is None:
        _global_tool_use = ToolUse()
    return _global_tool_use
