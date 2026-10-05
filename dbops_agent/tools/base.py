"""Validated tool interface and a trusted local transactional write path."""

from __future__ import annotations

import json
import sqlite3
from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ValidationError

from ..guard.policy import Policy


@dataclass
class ToolResult:
    ok: bool
    content: str
    error: str | None = None
    # 当一次写入被拒绝或被挂起时填充，让 trace 记录下**为什么**。
    verdict: str | None = None

    @staticmethod
    def success(content: str, verdict: str | None = None) -> ToolResult:
        return ToolResult(ok=True, content=content, verdict=verdict)

    @staticmethod
    def failure(error: str, verdict: str | None = None) -> ToolResult:
        return ToolResult(ok=False, content=f"ERROR: {error}", error=error, verdict=verdict)


@dataclass
class ToolContext:
    """一个工具被允许触碰的一切，作用域限定在单次故障处置之内。"""

    workspace: Path
    business_db: Path
    metrics_db: Path
    policy: Policy
    alert_id: str
    approval_ttl_seconds: float = 300.0
    observation_time: str = "2026-01-14T09:10:00Z"
    fault_hook: Callable[[str], None] | None = None

    def checkpoint(self, point: str) -> None:
        # Host-only injection for subprocess crash tests; never a tool parameter.
        if self.fault_hook:
            self.fault_hook(point)

    def connect(self, which: str = "business") -> sqlite3.Connection:
        path = self.business_db if which == "business" else self.metrics_db
        conn = sqlite3.connect(path)
        conn.row_factory = sqlite3.Row
        return conn


class Tool(ABC):
    name: str = ""
    description: str = ""
    args_model: type[BaseModel] = BaseModel
    # 写工具声明一个动作名，策略据此映射到层级。读工具留 None，永不经过策略检查。
    action: str | None = None

    def validate(self, raw_args: dict[str, Any] | str) -> tuple[BaseModel | None, str | None]:
        if isinstance(raw_args, str):
            try:
                raw_args = json.loads(raw_args or "{}")
            except json.JSONDecodeError as exc:
                return None, f"参数不是合法 JSON: {exc}"
        if not isinstance(raw_args, dict):
            return None, f"参数必须是一个对象，实际是 {type(raw_args).__name__}"
        try:
            return self.args_model.model_validate(raw_args), None
        except ValidationError as exc:
            return None, f"参数校验失败: {exc.errors()}"

    @property
    def is_write(self) -> bool:
        return self.action is not None

    @abstractmethod
    def run(self, ctx: ToolContext, args: BaseModel) -> ToolResult:  # noqa: ANN401
        ...

    def openai_schema(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.args_model.model_json_schema(),
            },
        }


# --- 写工具支持 ---------------------------------------------------------------------


class WriteTool(Tool):
    """Database effects only. Subclasses use the executor's connection and never commit."""

    @abstractmethod
    def apply(self, ctx: ToolContext, args: BaseModel, conn: sqlite3.Connection) -> ToolResult: ...

    def run(self, ctx: ToolContext, args: BaseModel) -> ToolResult:
        from ..guard.execution import execute

        return execute(self, ctx, args)
