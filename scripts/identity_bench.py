"""Free identifier/text shortcut counterexamples, not measured model generalization."""

import json
import tempfile
from pathlib import Path

from dbops_agent.incident.cases import ALERTS, build_case
from dbops_agent.judge.outcome import judge
from dbops_agent.tasks.scenario import load_scenarios
from scripts.audit_shortcuts import Player, scanner

LEGACY = {
    "ALERT-1001": ("deduplicate_payments", {"payment_ids": [8, 9]}),
    "ALERT-1002": ("rebuild_search_index", {}),
    "ALERT-1003": ("terminate_session", {"session_id": 101}),
    "ALERT-1004": ("raise_pool_ceiling", {"max_size": 20}),
    "ALERT-1005": ("acknowledge_alert", {"classification": "false_alarm", "rationale": "lookup"}),
}


def main():
    results = []
    scenarios = load_scenarios()
    legacy_alerts = {s.alert: LEGACY[f"ALERT-100{i + 1}"] for i, s in enumerate(scenarios)}
    with tempfile.TemporaryDirectory(prefix="dbops-identity-") as temp:
        for seed in (17, 83, 211):
            for template in range(len(ALERTS)):
                for scenario in scenarios:
                    for actor in (
                        "state_scanner",
                        "legacy_id_lookup",
                        "legacy_text_lookup",
                        "blind_ack",
                        "blind_pool",
                    ):
                        case, fx = build_case(
                            scenario,
                            Path(temp) / f"{seed}-{template}-{scenario.id}-{actor}",
                            variant_seed=seed,
                            template=template,
                        )
                        player = Player(fx, fx.alert_id)
                        if actor == "state_scanner":
                            scanner(player)
                        else:
                            choice = (
                                LEGACY.get(fx.alert_id)
                                if actor == "legacy_id_lookup"
                                else legacy_alerts.get(case.alert)
                                if actor == "legacy_text_lookup"
                                else LEGACY["ALERT-1005"]
                                if actor == "blind_ack"
                                else LEGACY["ALERT-1004"]
                            )
                            if choice:
                                name, args = choice
                                player.call(name, {**args, "idempotency_key": "blind"})
                        verdict = judge(case, player.trace, fx.workspace, fx.db_paths)
                        results.append(
                            {
                                "seed": seed,
                                "template": template,
                                "scenario": case.id,
                                "actor": actor,
                                "passed": verdict.passed,
                                "reads": sum(
                                    1 for s in player.trace.steps if s.tool_name and not s.was_write
                                ),
                                "blocker_id": fx.blocker_id,
                                "waiter_count": len(fx.waiter_ids),
                            }
                        )
    output = Path("docs/results/identity-results.json")
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(
            {
                "scope": "scripted actors; shared templates, varied sessions; no model API",
                "random_identity_not_a_generalization_proof": True,
                "results": results,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    for actor in sorted({r["actor"] for r in results}):
        rows = [r for r in results if r["actor"] == actor]
        print(f"{actor}: {sum(r['passed'] for r in rows)}/{len(rows)}")
    assert all(r["passed"] for r in results if r["actor"] == "state_scanner")
    assert not any(
        r["passed"] for r in results if r["actor"] in {"legacy_id_lookup", "legacy_text_lookup"}
    )


if __name__ == "__main__":
    main()
