"""Free decision/target controls. Synthetic instances, simulated operator, no LLM."""

import json
import tempfile
from dataclasses import asdict
from pathlib import Path

from dbops_agent.incident.cases import ALERTS, build_case
from dbops_agent.incident.variants import LOCK_VARIANTS, PAYMENT_VARIANTS
from dbops_agent.judge.outcome import judge
from dbops_agent.tasks.scenario import load_scenarios
from dbops_agent.tools.base import ToolResult
from dbops_agent.tools.library import DeduplicatePayments
from dbops_agent.tools.registry import ToolRegistry, default_tools
from scripts.audit_shortcuts import Player, scanner
from scripts.evidence_policy import observe, triage


class OldNullBackend(DeduplicatePayments):
    """Trusted faulty-backend injection, never exposed as Agent raw SQL capability."""

    def apply(self, ctx, args, conn):
        conn.execute(
            "DELETE FROM payments WHERE idempotency_key IS NULL AND EXISTS "
            "(SELECT 1 FROM payments k WHERE k.idempotency_key IS NOT NULL "
            "AND k.order_id=payments.order_id AND k.customer_id=payments.customer_id "
            "AND k.amount=payments.amount)"
        )
        return ToolResult.success("legacy NULL rule executed")


def act(player, actor):
    if actor == "evidence_rule":
        scanner(player)
    elif actor in {"always_inconclusive", "always_false_alarm"}:
        triage(player, "inconclusive" if actor == "always_inconclusive" else "false_alarm")
    elif actor in {"old_null_backend", "blind_cleanup"}:
        if actor == "old_null_backend":
            tools = [
                OldNullBackend() if t.name == "deduplicate_payments" else t for t in default_tools()
            ]
            player.registry = ToolRegistry(tools)
            targets = [p["id"] for p in observe(player, "payments") if p["idempotency_key"] is None]
        else:
            targets = [8, 9]
        player.call("deduplicate_payments", {"payment_ids": targets, "idempotency_key": "blind"})
        triage(player, "true_incident")
    else:
        if actor == "fixed101":
            target = 101
        else:
            sessions = observe(player, "db_sessions")
            if actor == "oldest_transaction":
                target = min(sessions, key=lambda r: r["started_at"])["id"]
            elif actor == "unique_idle":
                idle = [s for s in sessions if s["state"] == "idle in transaction"]
                target = idle[0]["id"] if len(idle) == 1 else None
            else:
                raise ValueError(actor)
        if target is not None:
            player.call("terminate_session", {"session_id": target, "idempotency_key": "kill"})


def main():
    scenarios = {s.id: s for s in load_scenarios()}
    matrix = {
        "f1_duplicate_payment": (
            PAYMENT_VARIANTS,
            (
                "evidence_rule",
                "old_null_backend",
                "blind_cleanup",
                "always_inconclusive",
                "always_false_alarm",
            ),
        ),
        "f3_lock_contention": (
            LOCK_VARIANTS,
            ("evidence_rule", "fixed101", "oldest_transaction", "unique_idle"),
        ),
    }
    results = []
    with tempfile.TemporaryDirectory(prefix="dbops-variants-") as temp:
        for family, (variants, actors) in matrix.items():
            for variant in variants:
                for seed in (17, 83, 211):
                    for template in range(len(ALERTS)):
                        for actor in actors:
                            case, fx = build_case(
                                scenarios[family],
                                Path(temp) / str(len(results)),
                                variant_seed=seed,
                                template=template,
                                variant=variant,
                            )
                            player = Player(fx, fx.alert_id)
                            act(player, actor)
                            verdict = judge(case, player.trace, fx.workspace, fx.db_paths)
                            verdict_data = asdict(verdict)
                            # Property booleans carry the evidence; avoid repeating prose
                            # for every synthetic row in the public result artifact.
                            verdict_data.pop("details")
                            results.append(
                                {
                                    "family": family,
                                    "variant": variant,
                                    "seed": seed,
                                    "template": template,
                                    "actor": actor,
                                    "disposition": verdict.disposition,
                                    "verdict": verdict_data,
                                    "tool_calls": [
                                        {"name": s.tool_name, "args": s.tool_args, "ok": s.tool_ok}
                                        for s in player.trace.steps
                                    ],
                                }
                            )
    report = {
        "scope": "synthetic SQLite; scripted actors; simulated external approval; no model API",
        "protocol": "payment receipts v1; targeted deletion; blocker relation v1",
        "planned_runs": len(results),
        "results": results,
    }
    output = Path("docs/results/variant-results.json")
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    for actor in sorted({r["actor"] for r in results}):
        subset = [r for r in results if r["actor"] == actor]
        print(f"{actor}: {sum(r['verdict']['passed'] for r in subset)}/{len(subset)}")
    assert all(r["verdict"]["passed"] for r in results if r["actor"] == "evidence_rule")
    for actor in (
        "old_null_backend",
        "blind_cleanup",
        "fixed101",
        "oldest_transaction",
        "unique_idle",
    ):
        assert not all(r["verdict"]["passed"] for r in results if r["actor"] == actor)
    print(f"Saved {len(results)} separate variant runs: {output}")


if __name__ == "__main__":
    main()
