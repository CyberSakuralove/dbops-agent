"""Offline paired cost comparison; add --llm only for explicit paid experiments."""

import argparse
import json
import tempfile
from pathlib import Path

from dbops_agent.config import CONFIG
from dbops_agent.incident.index_pair import ALERT, PairEnvironment, baseline
from dbops_agent.record.ledger import Ledger
from dbops_agent.runtimes.bare_loop import BareLoop
from dbops_agent.tasks.scenario import RootCause, Scenario

POLICIES = ("blind_rebuild", "initial_snapshot_rule", "wait_recheck_rule", "metrics_rule")
INSTANCES = ((9, 2, 0), (24, 6, 0), (60, 12, 0), (24, 6, 3))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, default=Path("docs/pair-results.json"))
    parser.add_argument("--llm", action="store_true", help="产生 API 调用和费用")
    parser.add_argument("--budget", type=float, default=3)
    args = parser.parse_args()
    if args.llm and not CONFIG.api_key:
        parser.error("DEEPSEEK_API_KEY 未设置")
    results = []
    policies = (*POLICIES, "llm") if args.llm else POLICIES
    ledger = Ledger(budget_cny=args.budget, model=CONFIG.model)
    with tempfile.TemporaryDirectory(prefix="dbops-pair-") as root:
        for instance, (size, speed, lag) in enumerate(INSTANCES):
            for policy in policies:
                for world in ("progressing", "stalled"):
                    env = PairEnvironment(
                        Path(root) / f"{instance}-{policy}-{world}",
                        progressing=world == "progressing",
                        size=size,
                        speed=speed,
                        metric_lag=lag,
                    )
                    if policy == "llm":
                        scenario = Scenario(
                            id="index-pair",
                            cause=RootCause.INDEX_DRIFT,
                            fixture="index-pair",
                            alert=ALERT,
                            max_steps=14,
                        )
                        runtime = BareLoop(
                            registry=env.registry,
                            system_prompt=(
                                "你负责索引告警。截止12 tick，先读工具说明中的时间口径。"
                                "后台同步可能自行推进，也可能停止。监控可能滞后；可调查、等待复查、重建或升级。"
                                "重建安全，但改写全部源行；等待不触发 Agent 改写。"
                                "目标是满足内容正确、数据保护与截止时间，比较调查与干预成本；"
                                "少改写和快完成存在取舍，没有隐藏的指定动作答案。"
                                "重建必须带稳定 idempotency_key，同一逻辑操作重试复用该键。"
                                "最后通过业务查询核验完整覆盖，输出简短总结并停止。"
                            ),
                        )
                        result = runtime.run(
                            scenario,
                            env.fx.workspace,
                            env.fx.business_db,
                            env.fx.metrics_db,
                            ledger=ledger,
                        )
                        outcome = env.outcome()
                        outcome.update(
                            model_tokens=result.trace.tokens,
                            estimated_cny=result.trace.spent_cny,
                            finished_reason=result.trace.finished_reason,
                        )
                    else:
                        outcome = baseline(env, policy)
                        outcome["model_tokens"] = 0
                        outcome["estimated_cny"] = 0
                    row = {
                        "instance": instance,
                        "size": size,
                        "speed": speed,
                        "metric_lag": lag,
                        "policy": policy,
                        "world": world,
                        **outcome,
                        "trace": env.history,
                    }
                    results.append(row)
    report = {
        "scope": "deterministic SQLite simulation; ticks/rows are not production cost",
        "llm_status": "measured" if args.llm else "pending_not_run",
        "five_case_regression_is_separate": True,
        "results": results,
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    for policy in policies:
        rows = [r for r in results if r["policy"] == policy]
        print(
            f"{policy}: pass={sum(r['passed'] for r in rows)}/{len(rows)}, "
            f"agent_inserted_rows={sum(r['agent_inserted_rows'] for r in rows)}, "
            f"completion_ticks={sum(r['completion_ticks'] for r in rows)}"
        )
    print(f"Saved: {args.out}; LLM: {report['llm_status']}")


if __name__ == "__main__":
    main()
