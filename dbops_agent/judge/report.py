"""聚合报告：pass^k、bootstrap 置信区间，以及失败归因表。

两个刻意的选择，都值得辩护：

**报 pass^k，不报 pass@1。** pass@k 问的是"它有没有成功过"，这会给一个不稳定的系统贴金。
pass^k 问的是"它是不是每次都成功了"，这才是运维人员真实的体验。k=3 时，75% 的单次成功率
意味着只有约 42% 的情况能连续三次干净地处置故障。

**报失败归因，而不只是报一个比率。** "62% 成功率"不 actionable。而"62% 成功率，且主要损失
来自 escalation_skipped"能告诉你该修什么——这也是面试官真正会追问的那部分。
"""

from __future__ import annotations

import random
from collections import defaultdict
from dataclasses import dataclass, field
from statistics import mean

from ..record.trace import Trace

ATTRIBUTION_BUCKETS = (
    "ok",
    "no_remedial_action",
    "acted_on_healthy_system",
    "escalation_skipped",
    "duplicate_side_effect",
    "wrong_fix_collateral",
    "wrong_fix",
    "triage_missing",
)

BUCKET_CN = {
    "ok": "正常",
    "no_remedial_action": "诊断后未动手",
    "acted_on_healthy_system": "对健康系统动手",
    "escalation_skipped": "跳过签核流程",
    "duplicate_side_effect": "修复重复执行",
    "wrong_fix_collateral": "修复伤及无辜",
    "wrong_fix": "修复无效",
    "triage_missing": "缺少分诊",
    "unknown": "未归类",
}


def pass_at_k(successes: int, trials: int, k: int) -> float:
    """k 次里至少成功一次的概率（能力上限）。"""
    if trials < k:
        return float("nan")
    if trials - successes < k:
        return 1.0
    ratio = 1.0
    for i in range(k):
        ratio *= (trials - successes - i) / (trials - i)
    return round(1.0 - ratio, 4)


def pass_pow_k(successes: int, trials: int, k: int) -> float:
    """k 次全部成功的概率（可靠性承诺）。"""
    if trials < k:
        return float("nan")
    return round((successes / trials) ** k, 4)


def bootstrap_ci(
    values: list[float], *, iterations: int = 2000, alpha: float = 0.05, seed: int = 0
) -> tuple[float, float]:
    """均值的百分位 bootstrap 置信区间。永远不要只报一个光秃秃的百分比。"""
    if not values:
        return (float("nan"), float("nan"))
    if len(values) == 1:
        return (values[0], values[0])
    rng = random.Random(seed)
    n = len(values)
    means = sorted(mean(rng.choices(values, k=n)) for _ in range(iterations))
    return (
        round(means[int((alpha / 2) * iterations)], 4),
        round(means[int((1 - alpha / 2) * iterations) - 1], 4),
    )


@dataclass
class GroupStats:
    """一个运行时的聚合结果。"""

    runtime: str
    trials: int = 0
    passes: int = 0
    by_scenario: dict[str, list[bool]] = field(default_factory=lambda: defaultdict(list))
    by_cause: dict[str, list[bool]] = field(default_factory=lambda: defaultdict(list))
    attribution: dict[str, int] = field(default_factory=lambda: defaultdict(int))
    mean_cny: float = 0.0
    mean_steps: float = 0.0
    illegitimate: int = 0
    duplicates: int = 0

    @property
    def success_rate(self) -> float:
        return round(self.passes / self.trials, 4) if self.trials else 0.0

    def ci(self) -> tuple[float, float]:
        per_scenario = [sum(v) / len(v) for v in self.by_scenario.values() if v]
        return bootstrap_ci(per_scenario)


def group_traces(traces: list[Trace]) -> dict[str, GroupStats]:
    buckets: dict[str, GroupStats] = {}
    for trace in traces:
        stats = buckets.setdefault(trace.runtime, GroupStats(runtime=trace.runtime))
        stats.trials += 1
        stats.passes += int(trace.passed)
        stats.by_scenario[trace.scenario_id].append(trace.passed)
        stats.by_cause[trace.cause].append(trace.passed)
        stats.attribution[
            getattr(trace, "attribution", None) or ("ok" if trace.passed else "unknown")
        ] += 1
        stats.illegitimate += trace.illegitimate_writes
        stats.duplicates += trace.duplicate_side_effects
        stats.mean_cny += (trace.spent_cny - stats.mean_cny) / stats.trials
        stats.mean_steps += (len(trace.steps) - stats.mean_steps) / stats.trials
    return buckets


def render(stats: dict[str, GroupStats], k: int = 3) -> str:
    """主报告：总体、按根因切片，以及失败归因表。"""
    lines: list[str] = []

    header = (
        f"{'运行时':<10} {'试验数':>6} {'通过':>5} {'成功率':>7} {'95% 置信区间':>18} "
        f"{'pass^' + str(k):<9} {'越权':>5} {'重复':>5} {'步数':>6} {'元/次':>9}"
    )
    lines.append(header)
    lines.append("-" * len(header))
    for runtime, s in sorted(stats.items()):
        lo, hi = s.ci()
        pk = pass_pow_k(s.passes, s.trials, k)
        lines.append(
            f"{runtime:<10} {s.trials:>6} {s.passes:>5} {s.success_rate:>7.3f} "
            f"{f'[{lo:.3f}, {hi:.3f}]':>18} {pk:<9.3f} {s.illegitimate:>5} "
            f"{s.duplicates:>5} {s.mean_steps:>6.1f} {s.mean_cny:>9.5f}"
        )

    # --- 按根因切片 ---------------------------------------------------------------------
    #
    # 总体数字会把一切有意思的东西藏起来。一次运行可能总体在进步，却在最要命的那类故障上
    # 严格变差——只有切片能把它显示出来。
    lines.append("")
    causes = sorted({c for s in stats.values() for c in s.by_cause})
    lines.append("按根因切片：")
    lines.append(f"  {'根因':<22} " + " ".join(f"{r:>16}" for r in sorted(stats)))
    for cause in causes:
        cells = []
        for runtime in sorted(stats):
            values = stats[runtime].by_cause.get(cause, [])
            cells.append(
                f"{(sum(values) / len(values)):>9.3f} (n={len(values):<3})"
                if values
                else f"{'-':>16}"
            )
        lines.append(f"  {cause:<22} " + " ".join(f"{c:>16}" for c in cells))

    # --- 失败归因 -----------------------------------------------------------------------
    lines.append("")
    lines.append("失败归因（每次运行的主导失败原因）：")
    lines.append(f"  {'归因':<28} " + " ".join(f"{r:>8}" for r in sorted(stats)))
    for bucket in ATTRIBUTION_BUCKETS:
        if bucket == "ok":
            continue
        counts = [stats[r].attribution.get(bucket, 0) for r in sorted(stats)]
        if not any(counts):
            continue
        label = f"{BUCKET_CN.get(bucket, bucket)}"
        lines.append(f"  {label:<28} " + " ".join(f"{c:>8}" for c in counts))

    return "\n".join(lines)
