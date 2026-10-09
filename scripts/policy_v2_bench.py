"""V1/V2 comparison on ALREADY OBSERVED cases. Never call this held-out generalization."""

import hashlib
import json
import tempfile
from pathlib import Path

from dbops_agent.guard.execution import ApprovalService
from dbops_agent.incident.diversity import (
    PAYMENT_SCALES,
    SERVICE_CONFIGS,
    build_payments,
    build_service,
    compose_service,
)
from dbops_agent.policies.access import Access
from dbops_agent.policies.payment_v2 import run as run_payment
from dbops_agent.policies.service_v2 import run as run_service
from dbops_agent.runtimes.bare_loop import SYSTEM_PROMPT
from dbops_agent.runtimes.context import WORKFLOW_V2
from scripts.audit_shortcuts import Player
from scripts.evidence_policy import payment_decision, triage
from scripts.service_policy import run_policy, simulated_approval
from scripts.structural_bench import (
    AUDIT_SEEDS,
    FROZEN_POLICIES,
    logical_input,
    payment_outcome,
)
from scripts.validate import ROOT, source_fingerprints

SERVICE_TABLES = (
    "service_requests",
    "service_progress",
    "service_history",
    "service_clock",
    "service_capacity_plan",
)


def approve_payment(ctx, request_id):
    ApprovalService(ctx.business_db).decide(
        request_id,
        approve=True,
        actor="simulated-v2-comparison-operator",
        reason="offline known-case regression; never model self-approval",
    )
    return True


def main():
    frozen = source_fingerprints()
    assert all(frozen[path] == digest for path, digest in FROZEN_POLICIES.items())
    historical = ROOT / "docs/results/structural-results.json"
    old = json.loads(historical.read_text(encoding="utf-8"))
    inputs = {(r["family"], r["config"], r["seed"]): r["initial_sha256"] for r in old["results"]}
    protocol = {
        "scope": "development/regression on previously observed structures and seeds, no API",
        "source_sha256": frozen,
        "v1_policy_sha256": FROZEN_POLICIES,
        "known_seeds": AUDIT_SEEDS,
        "max_calls": 40,
        "service_deadline_ticks": 32,
        "initial_state_reference": "structural-results.json",
        "initial_state_reference_sha256": hashlib.sha256(
            historical.read_bytes().replace(b"\r\n", b"\n")
        ).hexdigest(),
        "initial_state_reference_hash_normalization": "UTF-8 bytes, CRLF normalized to LF",
        "approval": "simulated independent operator",
        "new_generalization_evidence": False,
        "v2_files": [
            "dbops_agent/policies/access.py",
            "dbops_agent/policies/service_v2.py",
            "dbops_agent/policies/payment_v2.py",
            "dbops_agent/runtimes/context.py",
        ],
        "real_llm_v2_status": "not_run",
        "proposed_llm_prompt_sha256": hashlib.sha256(
            (SYSTEM_PROMPT + WORKFLOW_V2).encode("utf-8")
        ).hexdigest(),
        "postgresql_status": "not_run",
    }
    results = []
    temp_root = ROOT / "runs/validation-temp"
    temp_root.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="v2-regression-", dir=temp_root) as temp:
        for config in SERVICE_CONFIGS:
            for seed in AUDIT_SEEDS:
                spec = compose_service(config, seed)
                for policy in ("v1", "v2"):
                    env = build_service(Path(temp) / str(len(results)), spec)
                    _, digest = logical_input(env.ctx, SERVICE_TABLES)
                    assert digest == inputs[("service", config[0], seed)]

                    def approve(c, r, e=env):
                        return simulated_approval(e, c, r)

                    if policy == "v1":
                        run_policy(env.registry, env.ctx, "evidence_rule", approve)
                        status = {
                            "status": "completed",
                            "execution_complete": True,
                            "calls": sum("tool" in r for r in env.history),
                        }
                    else:
                        access = Access(env.registry, env.ctx, approve, deadline=32)
                        status = run_service(access)
                    env.trace.finished_reason = (
                        "completed" if status["execution_complete"] else "limit"
                    )
                    results.append(
                        {
                            "family": "service",
                            "config": config[0],
                            "seed": seed,
                            "policy": policy,
                            "initial_sha256": digest,
                            "policy_result": status,
                            **env.outcome(),
                            "trace": env.history,
                        }
                    )
        for orders, width in PAYMENT_SCALES:
            for seed in AUDIT_SEEDS:
                for policy in ("v1", "v2"):
                    fx, spec = build_payments(
                        Path(temp) / str(len(results)), orders=orders, per_order=width, seed=seed
                    )
                    player = Player(fx, fx.alert_id)
                    _, digest = logical_input(player.ctx)
                    assert digest == inputs[("payment", f"orders_{orders}", seed)]
                    status = {"status": "completed", "execution_complete": True}
                    if policy == "v1":
                        try:
                            triage(player, payment_decision(player))
                        except RuntimeError as exc:
                            if (
                                str(exc)
                                != "Incomplete observation; narrow the query before deciding"
                            ):
                                raise
                            status = {
                                "status": "policy_abstention_due_to_observation_limit",
                                "execution_complete": False,
                            }
                        status["calls"] = len(player.trace.steps)
                    else:
                        access = Access(
                            player.registry,
                            player.ctx,
                            approve_payment,
                            execution_trace=player.trace,
                        )
                        status = run_payment(access)
                    player.trace.finished_reason = (
                        "completed" if status["execution_complete"] else "observation_limit"
                    )
                    results.append(
                        {
                            "family": "payment",
                            "config": f"orders_{orders}",
                            "seed": seed,
                            "policy": policy,
                            "initial_sha256": digest,
                            "policy_result": status,
                            **payment_outcome(fx, player, spec),
                            "trace": player.trace.to_dict(),
                        }
                    )
    source_unchanged = source_fingerprints() == frozen
    bounded = all(r["policy_result"]["calls"] <= 40 for r in results)
    integrity = (
        source_unchanged and bounded and len(results) == 44 and all(r["safety"] for r in results)
    )
    report = {
        "protocol": protocol,
        "source_unchanged": source_unchanged,
        "checks_passed": integrity,
        "planned_runs": 44,
        "recorded_runs": len(results),
        "results": results,
    }
    output = ROOT / "docs/results/policy-v2-results.json"
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    (ROOT / "docs/results/policy-v2-freeze.json").write_text(
        json.dumps(protocol, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    for policy in ("v1", "v2"):
        service = [r for r in results if r["family"] == "service" and r["policy"] == policy]
        payment = [r for r in results if r["family"] == "payment" and r["policy"] == policy]
        print(
            f"{policy}: service verified={sum(r['recovery_verified'] for r in service)}/16 "
            f"disposition={sum(r['disposition_correct'] for r in service)}/16; "
            f"payment targets cleared={sum(r['ledger_targets_cleared'] for r in payment)}/6",
            flush=True,
        )
    print(f"Integrity checks={integrity}; these are known-case regression results")
    return 0 if integrity else 1


if __name__ == "__main__":
    raise SystemExit(main())
