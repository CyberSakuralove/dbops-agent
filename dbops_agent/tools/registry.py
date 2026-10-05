"""工具注册表：schema 导出、分发，以及所有调用的唯一咽喉。

模型调用经过 `call` 校验；数据库写工具统一进入事务执行器。
能够导入 Python 实现或访问原始文件的本地操作者属于信任边界，不由工具注册表隔离。
"""

from __future__ import annotations

import time
from typing import Any

from .base import Tool, ToolContext, ToolResult
from .library import (
    AcknowledgeAlert,
    DeduplicatePayments,
    DescribeConfig,
    IndexStatus,
    ListSessions,
    QueryBusinessDb,
    RaisePoolCeiling,
    ReadServiceMetrics,
    RebuildSearchIndex,
    SetConfig,
    TerminateSession,
    WriteReport,
)


class ToolRegistry:
    def __init__(self, tools: list[Tool] | None = None) -> None:
        self._tools: dict[str, Tool] = {}
        for tool in default_tools() if tools is None else tools:
            self.register(tool)

    def register(self, tool: Tool) -> None:
        if not tool.name:
            raise ValueError(f"{type(tool).__name__} 必须定义 `name`")
        self._tools[tool.name] = tool

    def get(self, name: str) -> Tool | None:
        return self._tools.get(name)

    def names(self) -> list[str]:
        return sorted(self._tools)

    def write_names(self) -> list[str]:
        return sorted(n for n, t in self._tools.items() if t.is_write)

    def schemas(self) -> list[dict[str, Any]]:
        return [t.openai_schema() for t in self._tools.values()]

    def call(
        self, name: str, raw_args: dict[str, Any] | str, ctx: ToolContext
    ) -> tuple[ToolResult, int]:
        """校验、执行、计时。返回 (结果, 耗时毫秒)。

        永不抛异常：工具内部出错会变成一条被记录下来的失败。因为在一次故障处置过程中崩溃，
        对 Agent 来说和工具报错没有区别；我们想让 Agent 的恢复行为成为被测量的对象，
        而不是让评测框架自己崩掉。
        """
        started = time.perf_counter()
        tool = self.get(name)
        if tool is None:
            known = ", ".join(self.names())
            return ToolResult.failure(f"未知工具 {name!r}。可用工具：{known}"), 0

        args, err = tool.validate(raw_args)
        if args is None:
            return ToolResult.failure(err or "参数不合法"), 0

        try:
            result = tool.run(ctx, args)
        except Exception as exc:  # noqa: BLE001 - 工具抛异常不能终止整次处置
            result = ToolResult.failure(f"{type(exc).__name__}: {exc}")

        return result, int((time.perf_counter() - started) * 1000)


def default_tools() -> list[Tool]:
    """读写分离是刻意的，而且在工具清单里一眼可见。

    5 个读工具、7 个写工具，跨越三个层级。一个在执行写操作之前从不调用读工具的 Agent
    是在猜，而 trace 会把这件事显示出来。
    """
    return [
        # --- 诊断（只读）---
        ReadServiceMetrics(),
        QueryBusinessDb(),
        ListSessions(),
        DescribeConfig(),
        IndexStatus(),
        # --- 修复（分级）---
        RebuildSearchIndex(),  # L0
        RaisePoolCeiling(),  # L0
        AcknowledgeAlert(),  # L0
        WriteReport(),  # L0
        DeduplicatePayments(),  # L1
        TerminateSession(),  # L1
        SetConfig(),  # L1
    ]
