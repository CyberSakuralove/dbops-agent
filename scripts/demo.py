"""Free persistent demo. Approval is performed separately by scripts.approve.

python -m scripts.demo init
python -m scripts.demo request
python -m scripts.demo execute --request REQUEST_ID
"""

import argparse
import json
import sqlite3
from contextlib import closing
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
    ctx = ToolContext(fx.workspace, fx.business_db, fx.metrics_db, Policy(), fx.alert_id)
    registry = ToolRegistry()
    if args.action == "execute":
        if not args.request:
            parser.error("execute 需要 --request")
        # Reuse the approved immutable target set, even after the first execution.
        # Operator metadata is used only by this host demo, never by a model tool.
        with closing(sqlite3.connect(fx.business_db)) as conn:
            row = conn.execute(
                "SELECT arguments FROM approvals WHERE request_id=?", (args.request,)
            ).fetchone()
        if row is None:
            parser.error("审批申请不存在")
        raw = {**json.loads(row[0]), "idempotency_key": "demo-payment-dedup"}
    else:
        # Public evidence, rather than evaluator-only allowed-delete IDs.
        from dbops_agent.incident.payment import verified_duplicates

        # Show both observations to the operator before computing candidates.
        print(registry.call("query_business_db", {"sql": "SELECT * FROM payments"}, ctx)[0].content)
        print(
            registry.call("query_business_db", {"sql": "SELECT * FROM payment_receipts"}, ctx)[
                0
            ].content
        )
        with closing(ctx.connect()) as conn:
            targets = sorted(verified_duplicates(conn))
        raw = {"payment_ids": targets, "idempotency_key": "demo-payment-dedup"}
    if args.action == "execute":
        raw["request_id"] = args.request
    result, _ = registry.call("deduplicate_payments", raw, ctx)
    print(json.dumps(asdict(result), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
