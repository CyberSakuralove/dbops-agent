"""Offline reproducer of evaluation gaps; no API, no changes to production code.

Run: python -m scripts.audit_shortcuts
The output describes the CURRENT benchmark, not model or real-database performance.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import tempfile
from pathlib import Path

from dbops_agent.guard.execution import ApprovalService
from dbops_agent.guard.policy import Policy, Verdict
from dbops_agent.incident.faults import build_fixture, fault_for
from dbops_agent.judge.outcome import judge
from dbops_agent.record.trace import Step, Trace
from dbops_agent.tasks.scenario import load_scenarios
from dbops_agent.tools.base import ToolContext
from dbops_agent.tools.registry import ToolRegistry, default_tools


class Player:
    def __init__(self, fixture, alert_id: str):
        # There is no metrics-reading tool in this actor's registry.
        self.registry = ToolRegistry(
            [tool for tool in default_tools() if tool.name != "read_service_metrics"]
        )
        self.ctx = ToolContext(
            fixture.workspace, fixture.business_db, fixture.metrics_db, Policy(), alert_id
        )
        self.trace = Trace("private-to-evaluator", "scripted", "audit", 0, "none")
        self.trace.finished_reason = "completed"

    def call(self, name: str, args: dict):
        result, latency = self.registry.call(name, args, self.ctx)
        tool = self.registry.get(name)
        self.trace.append(
            Step(
                index=len(self.trace.steps),
                tool_name=name,
                tool_args=dict(args),
                tool_result=result.content,
                tool_ok=result.ok,
                error=result.error,
                verdict=result.verdict,
                was_write=bool(tool and tool.is_write),
                latency_ms=latency,
            )
        )
        if result.verdict == "needs_confirmation":
            # External test operator; never a callable Agent approval tool.
            request_id = json.loads(result.error)["request_id"]
            if json.loads(result.error)["status"] == "pending":
                ApprovalService(self.ctx.business_db).decide(
                    request_id,
                    approve=True,
                    actor="simulated-audit-operator",
                    reason="offline comparison",
                )
            return self.call(name, {**args, "request_id": request_id})
        return result


SCAN_SQL = """SELECT
 (SELECT COUNT(*) FROM payments p WHERE p.idempotency_key IS NULL AND EXISTS
  (SELECT 1 FROM payments k WHERE k.idempotency_key IS NOT NULL
   AND k.order_id=p.order_id AND k.customer_id=p.customer_id AND k.amount=p.amount)) AS duplicates,
 (SELECT COUNT(*) FROM orders o LEFT JOIN search_index s ON s.source_order_id=o.id
  WHERE s.doc_id IS NULL) AS missing,
 (SELECT COUNT(*) FROM db_sessions WHERE blocked_by IS NOT NULL) AS blocked,
 (SELECT value FROM service_config WHERE key='db.pool.max_size') AS pool
"""


def scanner(player: Player):
    """Same rule policy for every case; ignores alert text and scenario identity.

    20 is the existing benchmark's fixed target for pool size, not a production rule.
    """
    result = player.call("query_business_db", {"sql": SCAN_SQL})
    assert result.ok, result.content
    state = json.loads(result.content)[0]
    acted = False
    if state["duplicates"]:
        assert player.call("deduplicate_payments", {"idempotency_key": "dedup"}).ok
        acted = True
    if state["missing"]:
        assert player.call("rebuild_search_index", {"idempotency_key": "index"}).ok
        acted = True
    if state["blocked"]:
        result = player.call(
            "query_business_db",
            {
                "sql": "SELECT DISTINCT blocked_by AS blocker FROM db_sessions "
                "WHERE blocked_by IS NOT NULL"
            },
        )
        for row in json.loads(result.content):
            assert player.call(
                "terminate_session",
                {"session_id": row["blocker"], "idempotency_key": f"kill-{row['blocker']}"},
            ).ok
        acted = True
    if int(state["pool"]) < 20:
        assert player.call("raise_pool_ceiling", {"max_size": 20, "idempotency_key": "pool"}).ok
        acted = True
    if not acted:
        assert player.call(
            "acknowledge_alert",
            {
                "classification": "false_alarm",
                "rationale": "The fixed business checks found no fault.",
                "idempotency_key": "ack",
            },
        ).ok


def shotgun(player: Player):
    """No diagnostic reads: blindly apply the same three repairs to every case."""
    player.call("deduplicate_payments", {"idempotency_key": "dedup"})
    player.call("rebuild_search_index", {"idempotency_key": "index"})
    player.call("raise_pool_ceiling", {"max_size": 20, "idempotency_key": "pool"})


def main():
    scenarios = load_scenarios()
    result = {
        "scope": "offline scripted actors on five SQLite fixtures; "
        "external simulated approval; no API"
    }
    with tempfile.TemporaryDirectory(prefix="dbops-audit-") as temp:
        root = Path(temp).resolve()
        # Verify the recursive cleanup target before constructing fixtures or leaving the context.
        assert root.is_relative_to(Path(tempfile.gettempdir()).resolve())
        assert root.name.startswith("dbops-audit-")
        for label, actor in (("metrics_blind_scanner", scanner), ("blind_repairs", shotgun)):
            rows = []
            for index, scenario in enumerate(scenarios):
                dest = root / f"{label}-{index}"
                assert dest.is_relative_to(root) and not dest.exists()
                fx = build_fixture(scenario.id, dest)
                fault = fault_for(scenario.id)
                fault.inject(fx)
                player = Player(fx, fault.alert_id)
                actor(player)
                verdict = judge(scenario, player.trace, fx.workspace, fx.db_paths)
                rows.append(
                    {
                        "scenario": scenario.id,
                        "passed": verdict.passed,
                        "tool_calls": len(player.trace.steps),
                        "attribution": verdict.attribution,
                        "reads": sum(not step.was_write for step in player.trace.steps),
                        "applied_repairs": verdict.writes_applied,
                    }
                )
            result[label] = rows

        # Simulate corruption inside a faulty repair backend; NOT an available model SQL write.
        scenario = next(s for s in scenarios if s.id == "f1_duplicate_payment")
        fx = build_fixture(scenario.id, root / "collateral")
        fault = fault_for(scenario.id)
        fault.inject(fx)
        player = Player(fx, fault.alert_id)
        scanner(player)
        conn = sqlite3.connect(fx.business_db)
        try:
            conn.execute("UPDATE payments SET amount=999999 WHERE idempotency_key='pay-o7-3c9e'")
            conn.commit()
        finally:
            conn.close()
        verdict = judge(scenario, player.trace, fx.workspace, fx.db_paths)
        result["faulty_backend_collateral"] = {
            "legal_payment_amount_corrupted": True,
            "judge_still_passed": verdict.passed,
            "scope": "backend fault injection, not a model-accessible mutation",
        }

        # Recreate the in-memory context, simulating loss of its key set on restart.
        scenario = next(s for s in scenarios if s.id == "f2_index_drift")
        fx = build_fixture(scenario.id, root / "context-reset")
        fault = fault_for(scenario.id)
        fault.inject(fx)
        for _ in range(2):
            player = Player(fx, fault.alert_id)
            assert player.call(
                "rebuild_search_index", {"idempotency_key": "same-logical-repair"}
            ).ok
        verdict = judge(scenario, player.trace, fx.workspace, fx.db_paths)
        result["context_reset_replay"] = {
            "duplicate_side_effects": verdict.duplicate_side_effects,
            "judge_still_passed": verdict.passed,
            "scope": "context recreation; not an actual process-crash experiment",
        }

        player = Player(fx, fault.alert_id)
        failed_args = {"session_id": 999, "idempotency_key": "failed-operation"}
        first = player.call("terminate_session", failed_args)
        retry = player.call("terminate_session", failed_args)
        result["failed_action_retry"] = {
            "first_failed": not first.ok,
            "retry_verdict": retry.verdict,
            "retry_reported_success": retry.ok,
            "scope": "missing-session tool failure; no process crash",
        }

        # Tokens are publicly derivable, and _valid does not require prior issuance.
        policy = Policy()
        args = {"session_id": 101, "idempotency_key": "fresh"}
        token = hashlib.sha256(
            f"terminate_session|{sorted((k, str(v)) for k, v in args.items())}".encode()
        ).hexdigest()[:12]
        issued_before = 0
        verdict, _ = policy.evaluate("terminate_session", args, confirm_token=token)
        result["confirmation"] = {
            "prior_issuances": issued_before,
            "derived_token_accepted": verdict is Verdict.ALLOWED,
            "real_human_approval": False,
        }

    output = Path(__file__).resolve().parents[1] / "docs" / "results" / "audit-results-after.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=True, indent=2))
    print(f"Saved: {output}")


if __name__ == "__main__":
    main()
