"""Free persistent demo. Approval is performed separately by scripts.approve.

python -m scripts.demo init
python -m scripts.demo request
python -m scripts.demo execute --request REQUEST_ID
"""

import argparse
import json
from dataclasses import asdict
from pathlib import Path

from dbops_agent.guard.policy import Policy
from dbops_agent.incident.faults import Fixture, build_fixture, fault_for
from dbops_agent.judge.outcome import judge
from dbops_agent.record.trace import Trace
from dbops_agent.tasks.scenario import load_scenarios
from dbops_agent.tools.base import ToolContext
from dbops_agent.tools.registry import ToolRegistry


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["init", "request", "execute", "judge"])
    parser.add_argument("--dir", type=Path, default=Path("runs/demo"))
    parser.add_argument("--request")
    args = parser.parse_args()
    dest = args.dir.resolve()
    if args.action == "init":
        if dest.exists():
            parser.error("目录已存在；请选择新的 --dir，避免覆盖演示证据")
        fx = build_fixture("f1_duplicate_payment", dest)
        fault_for("f1_duplicate_payment").inject(fx)
        print(f"演示已创建：{fx.business_db}")
        return
    if not (dest / "protected-state.json").is_file():
        parser.error("先执行 init 创建演示")
    fx = Fixture(dest, dest / "workspace", dest / "business.db", dest / "metrics.db")
    if args.action == "judge":
        scenario = next(s for s in load_scenarios() if s.id == "f1_duplicate_payment")
        trace = Trace(scenario.id, "scripted", "demo", 0, "none")
        trace.finished_reason = "completed"
        print(
            json.dumps(
                asdict(judge(scenario, trace, fx.workspace, fx.db_paths)),
                ensure_ascii=False,
                indent=2,
            )
        )
        return
    ctx = ToolContext(fx.workspace, fx.business_db, fx.metrics_db, Policy(), "ALERT-1001")
    raw = {"idempotency_key": "demo-payment-dedup"}
    if args.action == "execute":
        if not args.request:
            parser.error("execute 需要 --request")
        raw["request_id"] = args.request
    result, _ = ToolRegistry().call("deduplicate_payments", raw, ctx)
    print(json.dumps(asdict(result), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
