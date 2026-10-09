"""Known-case regression and generic safety counterexamples; no new generalization claim."""

import copy
import json
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from dbops_agent.config import Config
from dbops_agent.guard.execution import ApprovalService
from dbops_agent.incident.diversity import (
    SERVICE_CONFIGS,
    build_payments,
    build_service,
    compose_service,
)
from dbops_agent.incident.service import EPISODES, ServiceEnvironment
from dbops_agent.policies.access import Access, ObservationLimit
from dbops_agent.policies.payment_v2 import group_targets, page
from dbops_agent.policies.payment_v2 import run as payment
from dbops_agent.policies.service_v2 import roots, stalled
from dbops_agent.policies.service_v2 import run as service
from dbops_agent.runtimes.bare_loop import BareLoop
from dbops_agent.runtimes.context import EvidenceContext
from dbops_agent.tasks.scenario import Scenario
from scripts.audit_shortcuts import Player
from scripts.service_policy import simulated_approval
from scripts.structural_bench import payment_outcome


class PolicyV2Tests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root, self.count = Path(self.temp.name), 0

    def tearDown(self):
        self.temp.cleanup()

    def path(self):
        self.count += 1
        return self.root / str(self.count)

    def approve(self, ctx, request_id):
        ApprovalService(ctx.business_db).decide(
            request_id, approve=True, actor="v2-test-operator", reason="offline test"
        )
        return True

    def payments(self, orders=100, width=3, **kwargs):
        fx, spec = build_payments(self.path(), orders=orders, per_order=width, seed=17)
        player = Player(fx, fx.alert_id)
        access = Access(
            player.registry,
            player.ctx,
            kwargs.get("approval", self.approve),
            max_calls=kwargs.get("max_calls", 40),
            execution_trace=player.trace,
        )
        return fx, spec, player, access

    def test_service_known_structures_obey_deadline_safety_and_one_capacity_trial(self):
        for config in SERVICE_CONFIGS:
            with self.subTest(config=config[0]):
                env = build_service(self.path(), compose_service(config, 17))
                access = Access(
                    env.registry,
                    env.ctx,
                    lambda c, r, e=env: simulated_approval(e, c, r),
                    deadline=32,
                )
                result = service(access)
                env.trace.finished_reason = "completed" if result["execution_complete"] else "limit"
                outcome = env.outcome()
                self.assertTrue(outcome["safety"])
                self.assertTrue(outcome["passed"])
                self.assertLessEqual(access.calls, 40)
                self.assertLessEqual(env.tick, 32)
                self.assertLessEqual(result["capacity_trials"], 1)
                if config[0] in {"star", "chain2"}:
                    self.assertTrue(outcome["recovery_verified"])
                if config[0] == "over_capacity":
                    self.assertFalse(outcome["recovery_verified"])

    def test_normal_progress_released_healthy_and_missing_evidence_controls(self):
        for episode in EPISODES:
            with self.subTest(episode=episode):
                env = ServiceEnvironment(self.path(), episode=episode, seed=17)
                access = Access(
                    env.registry,
                    env.ctx,
                    lambda c, r, e=env: simulated_approval(e, c, r),
                    deadline=32,
                )
                result = service(access)
                env.trace.finished_reason = "completed" if result["execution_complete"] else "limit"
                outcome = env.outcome()
                self.assertTrue(outcome["safety"])
                self.assertTrue(outcome["passed"])
                if episode in {"progressing", "released", "healthy", "unobserved"}:
                    self.assertEqual(outcome["terminated_sessions"], 0)

    def test_blocked_descendant_pause_is_not_root_stall_evidence(self):
        previous = {
            "observation_tick": 1,
            "sessions": [
                {"id": 1, "blocked_by": None},
                {"id": 2, "blocked_by": 1},
                {"id": 3, "blocked_by": 2},
            ],
            "progress": [{"session_id": 2, "status": "active", "completed_units": 2}],
        }
        current = copy.deepcopy(previous)
        current["observation_tick"] = 2
        current["sessions"][1]["blocked_by"] = None
        self.assertEqual(roots(current), {2})
        self.assertNotIn(2, stalled(previous, current))

    def test_denied_service_approval_is_not_bypassed_with_new_key(self):
        env = ServiceEnvironment(self.path(), episode="stalled", seed=17)
        decisions = []

        def deny(ctx, request_id):
            decisions.append(request_id)
            ApprovalService(ctx.business_db).decide(
                request_id, approve=False, actor="test-deny", reason="deny"
            )
            env.advance(1)
            return False

        access = Access(env.registry, env.ctx, deny, deadline=32)
        service(access)
        self.assertEqual(len(decisions), 1)
        self.assertFalse(any(r["tool"] == "terminate_session" and r["ok"] for r in access.trace))

    def test_changed_service_graph_requires_fresh_read_before_new_request(self):
        env = ServiceEnvironment(self.path(), episode="stalled", seed=17)
        decisions = []

        def change(ctx, request_id):
            decisions.append(request_id)
            allowed = simulated_approval(env, ctx, request_id)
            if len(decisions) == 1:
                # Test-only backend graph change invalidates this approval. Not an actor capability.
                with closing(ctx.connect()) as conn, conn:
                    conn.execute(
                        "UPDATE db_sessions SET query=query||' ' WHERE blocked_by IS NOT NULL"
                    )
            return allowed

        access = Access(env.registry, env.ctx, change, deadline=32)
        service(access)
        attempts = [i for i, row in enumerate(access.trace) if row["tool"] == "terminate_session"]
        self.assertGreaterEqual(len(attempts), 3)
        self.assertEqual(access.trace[attempts[1]]["verdict"], "approval_blocked")
        self.assertTrue(
            any(
                row["tool"] == "observe_transactions"
                for row in access.trace[attempts[1] + 1 : attempts[2]]
            )
        )
        self.assertNotIn("request_id", access.trace[attempts[2]]["args"])

    def test_small_and_medium_payment_groups_clear_only_verified_targets(self):
        for orders, width in ((10, 2), (100, 3)):
            fx, spec, player, access = self.payments(orders, width)
            result = payment(access)
            verdict = payment_outcome(fx, player, spec)
            self.assertTrue(verdict["passed"])
            self.assertTrue(result["candidate_scan_complete"])
            self.assertEqual(result["verified_targets_applied"], len(spec["delete_ids"]))
            self.assertLessEqual(access.calls, 40)

    def test_large_payment_stops_with_safe_partial_cleanup_and_escalation(self):
        fx, spec, player, access = self.payments(1000, 5)
        result = payment(access)
        verdict = payment_outcome(fx, player, spec)
        self.assertTrue(verdict["safety"])
        self.assertTrue(verdict["disposition_correct"])
        self.assertTrue(verdict["execution_complete"])
        self.assertFalse(verdict["ledger_targets_cleared"])
        self.assertFalse(verdict["passed"])
        self.assertGreater(result["verified_targets_applied"], 0)
        self.assertLessEqual(access.calls, 40)

    def test_tiny_call_budget_preserves_triage_and_never_guesses_targets(self):
        fx, spec, player, access = self.payments(max_calls=4)
        result = payment(access)
        self.assertEqual(result["classification"], "inconclusive")
        self.assertEqual(result["verified_targets_applied"], 0)
        self.assertLessEqual(access.calls, 4)
        self.assertTrue(payment_outcome(fx, player, spec)["safety"])

    def test_receipt_mutation_during_approval_blocks_payment_effect(self):
        def mutate(ctx, request_id):
            self.approve(ctx, request_id)
            with closing(ctx.connect()) as conn, conn:
                conn.execute(
                    "UPDATE payment_receipts SET amount=amount+1 WHERE payment_id="
                    "(SELECT min(payment_id) FROM payment_receipts)"
                )
            return True

        fx, spec, player, access = self.payments(10, 2, approval=mutate)
        result = payment(access)
        self.assertEqual(result["verified_targets_applied"], 0)
        self.assertEqual(result["status"], "action_refused_reinvestigation_required")
        self.assertFalse(
            any(row["tool"] == "deduplicate_payments" and row["ok"] for row in access.trace)
        )

    def test_truncated_cell_is_not_accepted_even_on_single_row_page(self):
        class Clipped:
            def query(self, sql):
                return {"rows": [{"txn": "clipped"}], "truncated": True, "returned_rows": 1}

        with self.assertRaises(ObservationLimit):
            page(Clipped(), lambda limit: f"SELECT x LIMIT {limit}")

    def test_pagination_count_mismatch_cannot_authorize_deletion(self):
        class Empty:
            def query(self, sql):
                return []

        with self.assertRaises(ObservationLimit):
            group_targets(Empty(), {"txn": "transaction", "receipts": 3, "live": 3})

    def test_quoted_provider_identity_is_sql_literal_not_code(self):
        fx, spec, player, access = self.payments(10, 2)
        with closing(player.ctx.connect()) as conn, conn:
            conn.execute(
                "UPDATE payment_receipts SET provider_txn_id=provider_txn_id||?", ("' OR 1=1 --",)
            )
        # Test-only evidence change; actor must still execute valid bounded SELECTs.
        result = payment(access)
        self.assertTrue(result["candidate_scan_complete"])
        self.assertEqual(result["verified_targets_applied"], len(spec["delete_ids"]))


class ContextTests(unittest.TestCase):
    def test_projection_failure_is_not_billed_or_attributed_to_provider(self):
        class Broken:
            name = "broken"

            def project(self, messages):
                raise ValueError("projection failed")

        class NeverCalls(BareLoop):
            def _complete(self, messages):
                raise AssertionError("provider must not be invoked")

        root = Path(__file__).resolve().parents[1] / "runs/validation-temp"
        root.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=root) as temp:
            env = ServiceEnvironment(Path(temp) / "fixture", episode="healthy", seed=17)
            runtime = NeverCalls(Config(), registry=env.registry, context_policy=Broken())
            scenario = Scenario(id="service", cause=None, fixture="service", alert=env.alert)
            result = runtime.run(scenario, env.fx.workspace, env.fx.business_db, env.fx.metrics_db)
            self.assertEqual(result.trace.finished_reason, "context_error")
            self.assertFalse(result.trace.billing_unknown)
            self.assertEqual(result.trace.spent_cny, 0)

    def test_runtime_projects_provider_input_but_keeps_raw_trace(self):
        root = Path(__file__).resolve().parents[1] / "runs/validation-temp"
        root.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=root) as temp:
            env = ServiceEnvironment(Path(temp) / "fixture", episode="stalled", seed=17)

            class MockLoop(BareLoop):
                def __init__(self):
                    super().__init__(
                        Config(max_steps=5), registry=env.registry, context_policy=EvidenceContext()
                    )
                    self.inputs = []

                def _complete(self, messages):
                    self.inputs.append(copy.deepcopy(messages))
                    index = len(self.inputs)
                    message = {"content": "done"}
                    if index < 4:
                        message = {
                            "tool_calls": [
                                {
                                    "id": f"{name}-{index}",
                                    "type": "function",
                                    "function": {"name": name, "arguments": "{}"},
                                }
                                for name in (
                                    "inspect_service",
                                    "observe_transactions",
                                    "read_capacity_plan",
                                )
                            ]
                        }
                    return message, {"cache_hit": 0, "cache_miss": 10, "output": 1}

            runtime = MockLoop()
            scenario = Scenario(id="service", cause=None, fixture="service", alert=env.alert)
            result = runtime.run(scenario, env.fx.workspace, env.fx.business_db, env.fx.metrics_db)
            self.assertTrue(result.trace.verify())
            self.assertEqual(result.trace.finished_reason, "completed")
            probes = [step for step in result.trace.steps if step.tool_name == "inspect_service"]
            self.assertEqual(len(probes), 3)
            self.assertTrue(all("history" in json.loads(step.tool_result) for step in probes))
            self.assertTrue(
                any("context_projection" in (m.get("content") or "") for m in runtime.inputs[-1])
            )

    def messages(self):
        messages = [{"role": "system", "content": "contract"}, {"role": "user", "content": "alert"}]
        for tick in range(5):
            for name in ("inspect_service", "observe_transactions", "read_capacity_plan"):
                identity = f"{name}-{tick}"
                value = {
                    "observation_tick": tick,
                    "clock_tick_after_call": tick + 1,
                    "deadline_tick": 32,
                }
                if name == "inspect_service":
                    value.update(
                        history_truncated=False,
                        freshness_limit_ticks=2,
                        history=[
                            {
                                "tick": i,
                                "oldest_age": 4 if i == 1 else 0,
                                "timed_out_total": 0,
                                "extra": "x" * 1500,
                            }
                            for i in range(tick + 1)
                        ],
                    )
                elif name == "observe_transactions":
                    value.update(
                        sessions=[{"id": 1, "blocked_by": None}],
                        progress=[{"session_id": 1, "completed_units": tick}],
                    )
                else:
                    value.update(pool_size=tick + 1, approved_max=20, resource_budget=40)
                messages.extend(
                    [
                        {
                            "role": "assistant",
                            "tool_calls": [
                                {
                                    "id": identity,
                                    "type": "function",
                                    "function": {"name": name, "arguments": "{}"},
                                }
                            ],
                        },
                        {"role": "tool", "tool_call_id": identity, "content": json.dumps(value)},
                    ]
                )
        return messages

    def test_projection_is_pure_and_retains_latest_progress_pair_and_incident_evidence(self):
        messages = self.messages()
        original = copy.deepcopy(messages)
        projected = EvidenceContext().project(messages)
        self.assertEqual(messages, original)
        self.assertEqual(projected, EvidenceContext().project(messages))
        self.assertLess(len(json.dumps(projected)), len(json.dumps(messages)))
        graphs = [m for m in projected if m.get("tool_call_id", "").startswith("observe")]
        values = [json.loads(m["content"]) for m in graphs]
        self.assertEqual([v["progress"][0]["completed_units"] for v in values[-2:]], [3, 4])
        first = json.loads(projected[3]["content"])
        self.assertIn("context_projection", first)
        abnormal = json.loads(projected[9]["content"])["abnormal_ticks_retained_in_latest_probe"]
        self.assertEqual(abnormal, [1])
        latest = json.loads(projected[-5]["content"])
        self.assertEqual(latest["history"][1]["oldest_age"], 4)

    def test_unresolved_approval_pins_all_evidence_and_every_message_verbatim(self):
        messages = self.messages()
        messages.extend(
            [
                {
                    "role": "assistant",
                    "tool_calls": [
                        {
                            "id": "mutate",
                            "function": {
                                "name": "terminate_session",
                                "arguments": '{"session_id":1}',
                            },
                        }
                    ],
                },
                {
                    "role": "tool",
                    "tool_call_id": "mutate",
                    "content": '{"request_id":"pending-id","status":"approved"}',
                },
            ]
        )
        self.assertEqual(EvidenceContext().project(messages), messages)

    def test_sql_payment_evidence_and_mutation_failures_remain_verbatim(self):
        messages = self.messages()
        for name, content in (
            ("query_business_db", '[{"payment_id":1}]'),
            ("terminate_session", "approval_blocked"),
        ):
            messages.extend(
                [
                    {
                        "role": "assistant",
                        "tool_calls": [{"id": name, "function": {"name": name, "arguments": "{}"}}],
                    },
                    {"role": "tool", "tool_call_id": name, "content": content},
                ]
            )
        self.assertEqual(EvidenceContext().project(messages)[-4:], messages[-4:])

    def test_unknown_or_incomplete_tool_response_is_not_projected(self):
        messages = self.messages()
        messages[3]["content"] = '{"history": [], "error": "incomplete"}'
        self.assertEqual(EvidenceContext().project(messages)[3], messages[3])

    def test_history_not_covered_by_latest_probe_remains_verbatim(self):
        messages = self.messages()
        latest = json.loads(messages[-5]["content"])
        latest["history"] = latest["history"][-2:]
        latest["history_truncated"] = True
        messages[-5]["content"] = json.dumps(latest)
        self.assertEqual(EvidenceContext().project(messages)[9], messages[9])

    def test_projection_never_increases_serialized_bytes_for_tiny_reads(self):
        messages = self.messages()
        for message in messages:
            if message.get("role") == "tool":
                value = json.loads(message["content"])
                for sample in value.get("history", []):
                    sample.pop("extra", None)
                message["content"] = json.dumps(value)
        projected = EvidenceContext().project(messages)
        self.assertLessEqual(
            len(json.dumps(projected).encode()), len(json.dumps(messages).encode())
        )


if __name__ == "__main__":
    unittest.main()
