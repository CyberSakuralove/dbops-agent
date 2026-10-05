"""付费运行器：在模型上执行（场景 × 种子）矩阵。

    # 永远先跑免费的 dry run。
    python -m scripts.smoke

    # 校准：两个场景、一个种子、很紧的预算。用来确认真实 token 消耗。
    python -m scripts.pilot --limit 2 --seeds 1 --budget 3

    # 全量矩阵。
    python -m scripts.pilot --seeds 3 --budget 25

这个脚本试图强制两个习惯，因为它们正是一次「结果」和一段「轶事」之间的分界：

* **先校准，再放量。** `--budget` 默认值很低，而账本会中止整个运行而不是悄悄超支，
  所以一个失控的循环只花掉几块钱，而不是全部预算。
* **永远不要只报一个光秃秃的百分比。** 每个单元格都会打印 bootstrap 置信区间和 pass^k。

部分结果会边产生边落盘，所以一次被中止或崩溃的运行仍然留下可用的 trace。
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dbops_agent.config import CONFIG, PATHS  # noqa: E402
from dbops_agent.incident.faults import build_fixture, fault_for  # noqa: E402
from dbops_agent.judge.outcome import judge  # noqa: E402
from dbops_agent.judge.report import group_traces, render  # noqa: E402
from dbops_agent.record.ledger import BudgetExceeded, Ledger  # noqa: E402
from dbops_agent.runtimes.bare_loop import BareLoop  # noqa: E402
from dbops_agent.tasks.scenario import load_scenarios  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--runtime", default="bare", help="使用的运行时适配器")
    parser.add_argument("--scenarios", default="", help="逗号分隔的场景 id（默认全部）")
    parser.add_argument("--limit", type=int, default=0, help="只跑前 N 个场景")
    parser.add_argument("--seeds", type=int, default=1, help="每个场景重试几次")
    parser.add_argument("--budget", type=float, default=3.0, help="本次运行的硬性人民币上限")
    parser.add_argument("--model", default=CONFIG.model)
    parser.add_argument("--out", default="", help="输出目录（默认 runs/<时间戳>）")
    parser.add_argument("--yes", action="store_true", help="跳过确认提示")
    args = parser.parse_args(argv)

    CONFIG.model = args.model

    scenarios = load_scenarios()
    if args.scenarios:
        wanted = {s.strip() for s in args.scenarios.split(",")}
        scenarios = [s for s in scenarios if s.id in wanted]
    if args.limit:
        scenarios = scenarios[: args.limit]
    if not scenarios:
        print("没有选中任何场景", file=sys.stderr)
        return 2

    trials = len(scenarios) * args.seeds
    run_dir = Path(args.out) if args.out else PATHS.runs / time.strftime("%Y%m%d-%H%M%S")

    print(f"运行时  : {args.runtime}")
    print(f"模型    : {CONFIG.model}")
    print(f"场景    : {len(scenarios)} 个（{', '.join(s.id for s in scenarios)}）")
    print(f"种子    : {args.seeds}  ->  共 {trials} 次故障处置")
    print(f"预算    : {args.budget:.2f} 元")
    print(f"输出    : {run_dir}")
    print()

    if not args.yes:
        if input("确认开始？[y/N] ").strip().lower() not in {"y", "yes"}:
            print("已取消")
            return 1

    if not CONFIG.api_key:
        print("DEEPSEEK_API_KEY 未设置，无法运行付费矩阵。", file=sys.stderr)
        print("你是不是想先跑 `python -m scripts.smoke`？", file=sys.stderr)
        return 2

    if args.runtime != "bare":
        print(f"运行时 {args.runtime!r} 尚未实现（属于路线图条目）", file=sys.stderr)
        return 2

    runtime = BareLoop()
    ledger = Ledger(budget_cny=args.budget, model=CONFIG.model)
    traces: list = []
    run_dir.mkdir(parents=True, exist_ok=True)
    work_root = run_dir / "work"

    aborted = False
    for seed in range(args.seeds):
        for scenario in scenarios:
            dest = work_root / f"{scenario.id}-s{seed}"
            try:
                fx = build_fixture(scenario.id, dest)
                fault_for(scenario.id).inject(fx)
            except Exception as exc:  # noqa: BLE001
                print(f"  {scenario.id} 的 fixture 构建失败：{exc}")
                continue

            try:
                result = runtime.run(
                    scenario,
                    fx.workspace,
                    fx.business_db,
                    fx.metrics_db,
                    seed=seed,
                    ledger=ledger,
                )
            except BudgetExceeded as exc:
                print(f"\n预算熔断：{exc}")
                aborted = True
                break
            except Exception as exc:  # noqa: BLE001
                print(f"  {scenario.id}：运行时错误：{type(exc).__name__}: {exc}")
                continue

            verdict = judge(scenario, result.trace, fx.workspace, fx.db_paths)
            result.trace.passed = verdict.passed
            result.trace.properties = [f"{k}={v}" for k, v in verdict.property_results.items()]
            result.trace.illegitimate_writes = len(verdict.illegitimate_writes)
            result.trace.duplicate_side_effects = verdict.duplicate_side_effects
            result.trace.attribution = verdict.attribution
            traces.append(result.trace)

            print("  " + result.trace.summary_line())
            if not verdict.passed:
                for line in verdict.details:
                    if line.startswith("[FAIL]"):
                        print(f"       {line}")
            result.trace.dump(run_dir / "traces" / f"{scenario.id}-s{seed}.json")

        if aborted:
            break

    # --- 报告 -------------------------------------------------------------------------
    print()
    stats = group_traces(traces)
    print(render(stats, k=min(3, args.seeds)))
    print()

    totals = ledger.totals()
    breakdown = ledger.cost_breakdown()
    print(f"花费      : {totals['cny']:.4f} 元 / 预算 {args.budget:.2f} 元")
    print(f"模型调用  : {totals['calls']} 次")
    print(
        f"token     : 缓存命中 {totals['input_cache_hit']:,} | "
        f"未命中 {totals['input_cache_miss']:,} | 输出 {totals['output']:,}"
    )
    share = breakdown["output_share"]
    hint = "符合预期 —— 输出占主导" if share > 0.5 else "请核对定价假设"
    print(f"成本结构  : 输出占花费 {share:.1%}（{hint}）")
    print(f"回放缓存  : {runtime.cassette.stats}")
    if aborted:
        print("\n运行被提前中止 —— 以上是部分结果，不是完整矩阵。")

    ledger.dump(run_dir / "ledger.json")
    (run_dir / "summary.json").write_text(
        json.dumps(
            {
                "runtime": args.runtime,
                "model": CONFIG.model,
                "seeds": args.seeds,
                "scenarios": [s.id for s in scenarios],
                "cells": {
                    runtime_name: {
                        "trials": s.trials,
                        "passes": s.passes,
                        "success_rate": s.success_rate,
                        "ci": s.ci(),
                        "by_cause": {k: v for k, v in s.by_cause.items()},
                        "attribution": dict(s.attribution),
                        "illegitimate_writes": s.illegitimate,
                        "duplicate_side_effects": s.duplicates,
                        "mean_cny": s.mean_cny,
                    }
                    for runtime_name, s in stats.items()
                },
                "aborted": aborted,
                "ledger": totals,
            },
            indent=2,
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    print(f"\n已写入 {run_dir / 'summary.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
