"""场景定义与它们的成功判据。

一个场景就是一次故障：一个初始的**损坏**状态、一条 Agent 收到的告警，以及一组事后必须
成立的、机器可验证的性质。

`read_only` 这个标志值得解释，因为它编码了一条诚实性约束。有些场景是陷阱——正确动作是
什么都不做，或者拒绝执行。这类场景无法用"Agent 有没有说对话"来判分，因为本项目从不判文字。
它们改用结果来判：**状态必须未被改动**。这比"Agent 推理正确"是更弱的声称，而这里把它
如实标注出来，而不是藏起来。
"""

from __future__ import annotations

from enum import Enum
from pathlib import Path

import yaml
from pydantic import BaseModel, Field

from ..contract.assertions import AssertionSet
from ..guard.policy import Tier


class RootCause(str, Enum):
    """故障类别。每个场景恰好对应一个真实原因。

    这是 Agent 试图还原的标签，也是报告做切片所依据的维度。之所以能知道真实原因，
    是因为故障是**注入**的而不是观测来的——标准答案是构造出来的。
    """

    DUPLICATE_WRITE = "duplicate_write"          # 重试丢了幂等键，导致重复写入
    INDEX_DRIFT = "index_drift"                  # 派生索引与事实来源脱节
    LOCK_CONTENTION = "lock_contention"          # 长事务阻塞写入
    RESOURCE_EXHAUSTION = "resource_exhaustion"  # 连接池饱和
    FALSE_ALARM = "false_alarm"                  # 其实什么都没坏


class Scenario(BaseModel):
    id: str
    cause: RootCause
    alert: str = Field(description="交给 Agent 的告警文本。**可能是误导性的。**")
    fixture: str
    max_steps: int | None = None
    max_tokens: int | None = None
    read_only: bool = Field(
        default=False,
        description=(
            "为 True 表示正确的结果是不改动任何状态。这类场景按状态完整性判分，"
            "而不是按 Agent 的推理过程——因为本项目不判文字。这一点如实标注，而非隐藏。"
        ),
    )
    expected_tier: Tier | None = Field(
        default=None,
        description="正确修复所需的最高层级。通过审计日志判分。",
    )
    properties: list[AssertionSet] = Field(
        default_factory=list,
        description=(
            "在加载时从故障类填充，而不是从 YAML 读。故障和验证它的断言是一个整体，"
            "共同放在 incident/faults.py 里；把它们拆开正是「注入得进去但判不出来」的成因。"
        ),
    )
    notes: str = ""

    def grade(self, workspace: Path, db_paths: dict[str, Path]) -> tuple[bool, list[str]]:
        """对每个性质组求值。返回 (是否全部通过, 逐条断言明细)。"""
        details: list[str] = []
        all_passed = True
        for group in self.properties:
            for assertion in group.assertions:
                passed, detail = assertion.check(workspace, db_paths)
                label = f"{group.property} :: {assertion.description or assertion.kind.value}"
                details.append(f"[{'PASS' if passed else 'FAIL'}] {label}: {detail}")
                all_passed = all_passed and passed
        return all_passed, details


_SCENARIO_DIR = Path(__file__).resolve().parent


def load_scenarios(path: Path | str = _SCENARIO_DIR / "seed.yaml") -> list[Scenario]:
    """加载场景索引，并把每个场景的成功判据从它的故障类挂上去。

    这里的 import 放在函数内，是因为 `incident.faults` 反过来要从本模块导入 `RootCause`，
    顶层导入会踩到这个环。
    """
    from ..incident.faults import fault_for

    raw = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    scenarios: list[Scenario] = []
    for item in raw["scenarios"]:
        scenario = Scenario.model_validate(item)
        fault = fault_for(scenario.id)
        # 身份信息来自 YAML，行为来自故障类，这样两者不可能漂移。
        scenario.properties = fault.properties()
        scenario.expected_tier = fault.tier()
        scenario.read_only = fault.read_only()
        scenarios.append(scenario)
    return scenarios
