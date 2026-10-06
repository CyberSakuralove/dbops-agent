"""哈希链式 trace。

**为什么把哈希串起来，而不是直接 dump JSON**：报告里的数字必须可核对。如果一个数字无法
沿着一条未断的链追溯到源头，那它就是一句声称，而不是一次测量——而本项目的全部前提就是
「关于 Agent 行为的声称价值很低」。

这条链还让重放变得便宜，这才使得一个失败的场景能变成回归测试，而不是一段轶事。
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

GENESIS = "0" * 64


def _canonical(payload: Any) -> str:
    return json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def _digest(prev: str, payload: Any) -> str:
    return hashlib.sha256(f"{prev}|{_canonical(payload)}".encode()).hexdigest()


@dataclass
class Step:
    """Agent 单轮的可见效果。

    `verdict` 记录护栏当时做了什么裁决，所以一次运行可以被还原成"它在这里申请了确认，
    在那里被拒绝"。`was_write` 区分诊断与动手，而失败归因正是按这个轴切片的。
    """

    index: int
    tool_name: str | None = None
    tool_args: dict[str, Any] | None = None
    tool_result: str | None = None
    tool_ok: bool = True
    error: str | None = None
    verdict: str | None = None
    was_write: bool = False
    latency_ms: int = 0
    model_text: str | None = None
    input_cache_hit: int = 0
    input_cache_miss: int = 0
    output_tokens: int = 0
    local_replay: bool = False


@dataclass
class Trace:
    """一次故障处置的记录。seed 是兼容保留的试次标签，不控制模型采样。"""

    scenario_id: str
    cause: str
    runtime: str
    seed: int
    model: str
    steps: list[Step] = field(default_factory=list)
    provider_parameters: dict[str, Any] = field(default_factory=dict)
    billing_unknown: bool = False

    passed: bool = False
    properties: list[str] = field(default_factory=list)
    finished_reason: str = "unknown"

    # 归因计数，由判分器填写。
    reads_before_first_write: int = 0
    confirmations_requested: int = 0
    refusals: int = 0
    illegitimate_writes: int = 0
    duplicate_side_effects: int = 0
    attribution: str = "unknown"
    disposition: str = "unknown"

    wall_ms: int = 0
    spent_cny: float = 0.0
    tokens: dict[str, int] = field(default_factory=dict)

    _prev_hash: str = GENESIS
    hashes: list[str] = field(default_factory=list)

    @property
    def head(self) -> str:
        return self.hashes[-1] if self.hashes else GENESIS

    def append(self, step: Step) -> str:
        self.steps.append(step)
        digest = _digest(self._prev_hash, asdict(step))
        self.hashes.append(digest)
        self._prev_hash = digest
        return digest

    def verify(self) -> bool:
        """重算整条链。返回 False 说明某一步在事后被改过。"""
        prev = GENESIS
        if len(self.steps) != len(self.hashes):
            return False
        for step, recorded in zip(self.steps, self.hashes, strict=True):
            prev = _digest(prev, asdict(step))
            if prev != recorded:
                return False
        return True

    def to_dict(self) -> dict[str, Any]:
        return {
            "scenario_id": self.scenario_id,
            "cause": self.cause,
            "runtime": self.runtime,
            "seed": self.seed,
            "trial_label": self.seed,
            "provider_parameters": self.provider_parameters,
            "billing_unknown": self.billing_unknown,
            "model": self.model,
            "passed": self.passed,
            "properties": self.properties,
            "finished_reason": self.finished_reason,
            "reads_before_first_write": self.reads_before_first_write,
            "confirmations_requested": self.confirmations_requested,
            "refusals": self.refusals,
            "illegitimate_writes": self.illegitimate_writes,
            "duplicate_side_effects": self.duplicate_side_effects,
            "attribution": self.attribution,
            "disposition": self.disposition,
            "wall_ms": self.wall_ms,
            "spent_cny": self.spent_cny,
            "tokens": self.tokens,
            "head": self.head,
            "steps": [asdict(s) for s in self.steps],
            "hashes": self.hashes,
        }

    def dump(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w", encoding="utf-8", dir=path.parent, delete=False
            ) as stream:
                temporary = stream.name
                json.dump(self.to_dict(), stream, indent=2, ensure_ascii=False)
            os.replace(temporary, path)
        finally:
            if temporary and os.path.exists(temporary):
                os.unlink(temporary)

    def summary_line(self) -> str:
        return (
            f"{self.scenario_id:<22} {self.cause:<20} "
            f"{'通过' if self.passed else '失败':<4} "
            f"步数={len(self.steps):<3} 读={self.reads_before_first_write:<2} "
            f"写={sum(1 for s in self.steps if s.was_write):<2} "
            f"确认={self.confirmations_requested:<2} 拒绝={self.refusals:<2} "
            f"越权={self.illegitimate_writes:<2} {self.spent_cny:.5f} 元"
        )
