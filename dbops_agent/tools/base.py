"""工具基类：参数校验、受策略管辖的写入路径，以及幂等。

两个机制放在这里而不是放进各个工具，因为它们必须统一生效：

**策略强制。** 每个写工具在触碰状态之前都必须过 `guard_check`。工具无法选择跳过，
所以"Agent 绕过了自己的护栏"不是一个可达状态。

**幂等。** 写工具接受一个可选的 `idempotency_key`。当提供了键、且该键在本轮已经执行过时，
工具会返回原先的结果而不是第二次施加改动。这正是「重复写入」场景所针对的机制；而把这个
键做成可选是刻意的：一次不带键的重试**必须**真的产生重复，这样危险才是可观测的，
而不是停留在理论上的。

参数强制转换也是故意的。数字 id 以字符串形式传进来，是最常见的真实工具调用失败之一，
而本项目要测的是**诊断**质量——让这种琐碎的类型噪声主导失败统计，会把真正的信号盖掉。
"""

from __future__ import annotations

import json
import sqlite3
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ValidationError

from ..guard.policy import Policy, Verdict


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
    # 已执行过的幂等键集合，用于让重试短路。
    executed_keys: set[str] = field(default_factory=set)

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
    """一切会改动状态的工具的基类。

    把每个写入都必须按顺序执行的三步集中在这里：策略检查、幂等检查、然后执行并留痕。
    子类只需要实现 `apply`。
    """

    def enforce(
        self, ctx: ToolContext, args: BaseModel, raw: dict[str, Any]
    ) -> tuple[Verdict, str, ToolResult | None]:
        """执行策略与幂等两道闸门。

        返回 (裁决, 原因, 短路结果)。短路结果非 None 意味着调用方应当直接返回它，
        不施加任何改动。
        """
        assert self.action is not None  # 由 is_write 保证
        verdict, reason = ctx.policy.evaluate(
            self.action, raw, confirm_token=raw.get("confirm_token")
        )
        if verdict is not Verdict.ALLOWED:
            self.log(ctx, args, raw, outcome=verdict.value, tier=ctx.policy.tier_of(self.action))
            return verdict, reason, ToolResult.failure(
                f"{verdict.value}: {reason}", verdict=verdict.value
            )

        key = raw.get("idempotency_key")
        if key and key in ctx.executed_keys:
            # 本轮已经施加过。返回原结果，不做任何改动。
            return (
                verdict,
                reason,
                ToolResult.success(
                    f"幂等键 {key!r} 已执行过，本次不做改动", "duplicate_skipped"
                ),
            )
        if key:
            ctx.executed_keys.add(key)

        return verdict, reason, None

    def log(
        self,
        ctx: ToolContext,
        args: BaseModel,
        raw: dict[str, Any],
        *,
        outcome: str,
        tier: Any,  # noqa: ANN401 - guard.policy.Tier
    ) -> None:
        """追写审计留痕。判分器读它；Agent 无法把它抹掉。"""
        target = json.dumps(
            {k: v for k, v in raw.items() if k not in {"confirm_token", "idempotency_key"}},
            sort_keys=True,
        )
        conn = ctx.connect()
        try:
            conn.execute(
                "INSERT INTO repair_log (action,tier,target,idempotency_key,outcome,ts) "
                "VALUES (?,?,?,?,?,datetime('now'))",
                (self.action, tier.value, target, raw.get("idempotency_key"), outcome),
            )
            conn.commit()
        finally:
            conn.close()

    @abstractmethod
    def apply(self, ctx: ToolContext, args: BaseModel) -> ToolResult:
        """真正施加改动。只有在所有闸门都通过之后才会被调用。"""

    def run(self, ctx: ToolContext, args: BaseModel) -> ToolResult:
        raw = args.model_dump()
        verdict, _reason, short_circuit = self.enforce(ctx, args, raw)
        if short_circuit is not None:
            return short_circuit
        result = self.apply(ctx, args)
        self.log(
            ctx,
            args,
            raw,
            outcome="applied" if result.ok else "failed",
            tier=ctx.policy.tier_of(self.action or ""),
        )
        return result
