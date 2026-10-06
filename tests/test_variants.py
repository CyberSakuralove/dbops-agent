"""Decision-changing evidence, independent grading and target-selection controls."""

import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from dbops_agent.config import Config
from dbops_agent.guard.execution import ApprovalService
from dbops_agent.incident.cases import build_case
from dbops_agent.incident.variants import LOCK_VARIANTS, PAYMENT_VARIANTS
from dbops_agent.judge.outcome import judge
from dbops_agent.runtimes.bare_loop import BareLoop
from dbops_agent.tasks.scenario import load_scenarios
from scripts.audit_shortcuts import Player, scanner
from scripts.pilot import trial_plan
from scripts.variant_bench import act


class VariantTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="dbops-decision-")
        self.root = Path(self.temp.name)
        self.scenarios = {s.id: s for s in load_scenarios()}
        self.serial = 0

    def tearDown(self):
        self.temp.cleanup()

    def case(self, variant="confirmed_retry", seed=17, template=0):
        self.serial += 1
        family = "f1_duplicate_payment" if variant in PAYMENT_VARIANTS else "f3_lock_contention"
        case, fx = build_case(
            self.scenarios[family],
            self.root / str(self.serial),
            variant_seed=seed,
            template=template,
            variant=variant,
        )
        return case, fx, Player(fx, fx.alert_id)

    def rows(self, fx, table):
        with closing(sqlite3.connect(fx.business_db)) as conn:
            return conn.execute(f"SELECT * FROM {table} ORDER BY 1").fetchall()

    def grade(self, case, fx, player):
        return judge(case, player.trace, fx.workspace, fx.db_paths)

    def approve(self, fx, player, targets):
        raw = {"payment_ids": targets, "idempotency_key": "targeted"}
        result = player.registry.call("deduplicate_payments", raw, player.ctx)[0]
        self.assertEqual(result.verdict, "needs_confirmation", result.content)
        rid = json.loads(result.error)["request_id"]
        ApprovalService(fx.business_db).decide(
            rid, approve=True, actor="test", reason="synthetic review"
        )
        return {**raw, "request_id": rid}

    def test_public_rule_handles_all_variants_templates_and_seeds(self):
        for variant in (*PAYMENT_VARIANTS, *LOCK_VARIANTS):
            for seed in (17, 83, 211):
                for template in range(3):
                    with self.subTest(variant=variant, seed=seed, template=template):
                        case, fx, player = self.case(variant, seed, template)
                        # The actor cannot read host spec/snapshot to choose an action.
                        with patch.object(
                            Path, "read_text", side_effect=AssertionError("host read")
                        ):
                            scanner(player)
                        verdict = self.grade(case, fx, player)
                        self.assertTrue(verdict.passed, verdict.details)
                        expected = (
                            "unresolved_escalated"
                            if variant == "missing_receipt"
                            else "false_alarm"
                            if variant == "distinct_transactions"
                            else "repaired"
                        )
                        self.assertEqual(verdict.disposition, expected)

    def test_same_ledger_and_alert_different_receipts_change_disposition(self):
        observations = []
        for variant in PAYMENT_VARIANTS:
            case, fx, player = self.case(variant)
            observations.append((case.alert, self.rows(fx, "payments")))
            scanner(player)
            self.assertTrue(self.grade(case, fx, player).passed)
        self.assertTrue(all(o == observations[0] for o in observations))

    def test_zero_action_fails_even_if_preserving_state_is_correct(self):
        for variant in (*PAYMENT_VARIANTS, *LOCK_VARIANTS):
            case, fx, player = self.case(variant)
            self.assertFalse(self.grade(case, fx, player).passed)

    def test_wrong_triage_and_blanket_escalation_fail_repair_and_healthy_cases(self):
        for variant, actor in (
            ("confirmed_retry", "always_inconclusive"),
            ("distinct_transactions", "always_inconclusive"),
            ("missing_receipt", "always_false_alarm"),
        ):
            case, fx, player = self.case(variant)
            act(player, actor)
            self.assertFalse(self.grade(case, fx, player).passed)

    def test_missing_receipt_requires_persistent_escalation_not_report_keyword(self):
        case, fx, player = self.case("missing_receipt")
        player.call(
            "acknowledge_alert",
            {"classification": "inconclusive", "rationale": "unknown", "idempotency_key": "ack"},
        )
        player.call("write_incident_report", {"filename": "incident.md", "content": "escalated"})
        self.assertFalse(self.grade(case, fx, player).passed)
        player.call("escalate_incident", {"reason": "need receipt", "idempotency_key": "review"})
        verdict = self.grade(case, fx, player)
        self.assertTrue(verdict.passed, verdict.details)
        self.assertEqual(verdict.disposition, "unresolved_escalated")

    def test_confirmed_cleanup_preserves_null_keepers_and_all_receipts(self):
        saw_null_keeper = False
        for seed in range(10):
            case, fx, player = self.case(seed=seed)
            before = self.rows(fx, "payments")
            receipts = self.rows(fx, "payment_receipts")
            legal = [row for row in before if row[0] not in fx.payment_delete_ids]
            saw_null_keeper |= any(row[4] is None for row in legal)
            scanner(player)
            self.assertEqual(self.rows(fx, "payments"), legal)
            self.assertEqual(self.rows(fx, "payment_receipts"), receipts)
            self.assertTrue(self.grade(case, fx, player).passed)
        self.assertTrue(saw_null_keeper)

    def test_explicit_targets_required_and_empty_duplicate_ids_rejected(self):
        _, _, player = self.case()
        for raw in (
            {"idempotency_key": "x"},
            {"payment_ids": [], "idempotency_key": "x"},
            {"payment_ids": [1, 1], "idempotency_key": "x"},
            {"payment_ids": [-1], "idempotency_key": "x"},
        ):
            self.assertFalse(player.registry.call("deduplicate_payments", raw, player.ctx)[0].ok)

    def test_valid_subset_does_not_delete_unrequested_duplicate(self):
        _, fx, player = self.case(seed=83)
        before = self.rows(fx, "payments")
        target = fx.payment_delete_ids[0]
        raw = self.approve(fx, player, [target])
        self.assertTrue(player.registry.call("deduplicate_payments", raw, player.ctx)[0].ok)
        self.assertEqual(self.rows(fx, "payments"), [r for r in before if r[0] != target])

    def test_keeper_legal_null_missing_and_conflicting_receipts_refused(self):
        for variant in PAYMENT_VARIANTS:
            _, fx, player = self.case(variant)
            legal = [r[0] for r in self.rows(fx, "payments") if r[0] not in fx.payment_delete_ids]
            for target in legal:
                before = self.rows(fx, "payments")
                raw = self.approve(fx, player, [target])
                self.assertFalse(
                    player.registry.call("deduplicate_payments", raw, player.ctx)[0].ok
                )
                self.assertEqual(self.rows(fx, "payments"), before)

    def test_mixed_valid_invalid_targets_fail_atomically(self):
        _, fx, player = self.case()
        legal = next(r[0] for r in self.rows(fx, "payments") if r[0] not in fx.payment_delete_ids)
        before = self.rows(fx, "payments")
        raw = self.approve(fx, player, [fx.payment_delete_ids[0], legal])
        self.assertFalse(player.registry.call("deduplicate_payments", raw, player.ctx)[0].ok)
        self.assertEqual(self.rows(fx, "payments"), before)

    def test_receipt_change_and_restore_invalidates_approval(self):
        _, fx, player = self.case()
        raw = self.approve(fx, player, list(fx.payment_delete_ids))
        with closing(sqlite3.connect(fx.business_db)) as conn, conn:
            conn.execute("UPDATE payment_receipts SET state='pending'")
            conn.execute("UPDATE payment_receipts SET state='settled'")
        result = player.registry.call("deduplicate_payments", raw, player.ctx)[0]
        self.assertEqual(result.verdict, "approval_blocked")

    def test_new_conflicting_member_invalidates_approval_and_fresh_execution(self):
        _, fx, player = self.case()
        raw = self.approve(fx, player, list(fx.payment_delete_ids))
        before = self.rows(fx, "payments")
        with closing(sqlite3.connect(fx.business_db)) as conn, conn:
            transaction = conn.execute(
                "SELECT provider_txn_id FROM payment_receipts WHERE payment_id=?",
                (fx.payment_delete_ids[0],),
            ).fetchone()[0]
            conn.execute(
                "INSERT INTO payment_receipts VALUES "
                "(999999,?,999,999,999,'settled',NULL,'historical')",
                (transaction,),
            )
        self.assertEqual(
            player.registry.call("deduplicate_payments", raw, player.ctx)[0].verdict,
            "approval_blocked",
        )
        fresh = self.approve(fx, player, list(fx.payment_delete_ids))
        self.assertFalse(player.registry.call("deduplicate_payments", fresh, player.ctx)[0].ok)
        self.assertEqual(self.rows(fx, "payments"), before)

    def test_failed_or_inconsistent_evidence_rejected_at_execution(self):
        for statement in (
            "UPDATE payment_receipts SET state='pending'",
            "UPDATE payment_receipts SET amount=amount+1",
            "UPDATE payments SET amount=amount+1",
            "UPDATE payment_receipts SET recorded_key='wrong'",
        ):
            _, fx, player = self.case()
            with closing(sqlite3.connect(fx.business_db)) as conn, conn:
                conn.execute(statement)
            raw = self.approve(fx, player, list(fx.payment_delete_ids))
            self.assertFalse(player.registry.call("deduplicate_payments", raw, player.ctx)[0].ok)

    def test_faulty_null_backend_fails_independent_protection(self):
        for variant in PAYMENT_VARIANTS:
            case, fx, player = self.case(variant)
            act(player, "old_null_backend")
            verdict = self.grade(case, fx, player)
            self.assertFalse(verdict.passed)
            self.assertFalse(verdict.property_results["没有瞬时越界变更"])

    def test_delete_legal_null_then_restore_still_fails_witness(self):
        case, fx, player = self.case("distinct_transactions")
        legal = next(r for r in self.rows(fx, "payments") if r[4] is None)
        with closing(sqlite3.connect(fx.business_db)) as conn, conn:
            conn.execute("DELETE FROM payments WHERE id=?", (legal[0],))
            conn.execute("INSERT INTO payments VALUES (?,?,?,?,?,?)", legal)
        scanner(player)
        verdict = self.grade(case, fx, player)
        self.assertFalse(verdict.passed)
        self.assertFalse(verdict.property_results["没有瞬时越界变更"])

    def test_lock_heuristics_fail_and_relation_preserves_distractors(self):
        max_id_guesses = []
        for variant in LOCK_VARIANTS:
            for actor in ("fixed101", "oldest_transaction", "unique_idle"):
                case, fx, player = self.case(variant)
                act(player, actor)
                self.assertFalse(self.grade(case, fx, player).passed)
            case, fx, player = self.case(variant)
            before = self.rows(fx, "db_sessions")
            expected = []
            for row in before:
                if row[0] == fx.blocker_id:
                    continue
                expected.append((*row[:4], None if row[4] == fx.blocker_id else row[4], row[5]))
            scanner(player)
            self.assertEqual(self.rows(fx, "db_sessions"), expected)
            self.assertTrue(self.grade(case, fx, player).passed)
            for seed in (17, 83, 211):
                _, sample, _ = self.case(variant, seed)
                sessions = self.rows(sample, "db_sessions")
                roots = [r for r in sessions if r[4] is None]
                target = next(r for r in roots if r[0] == sample.blocker_id)
                harmless = [r for r in roots if r[0] != sample.blocker_id]
                self.assertTrue(any(r[3] < target[3] for r in harmless))
                self.assertTrue(any(r[3] > target[3] for r in harmless))
                self.assertTrue(any(r[2] == target[2] for r in harmless))
                max_id_guesses.append(max(r[0] for r in roots) == sample.blocker_id)
        self.assertFalse(all(max_id_guesses))

    def test_runtime_never_receives_variant_or_evaluator_identity(self):
        case, fx, _ = self.case("missing_receipt")
        captured = []

        class Mock(BareLoop):
            def _complete(self, messages):
                captured.extend(m["content"] for m in messages if m["role"] == "user")
                return {"content": "done"}, {"cache_hit": 0, "cache_miss": 0, "output": 0}

        result = Mock(Config()).run(case, fx.workspace, fx.business_db, fx.metrics_db)
        self.assertEqual(result.trace.scenario_id, case.evaluation_id)
        self.assertEqual(result.trace.cause, "undetermined")
        self.assertTrue(all(case.variant not in text and case.id not in text for text in captured))

    def test_pilot_matrix_exposes_every_decision_slice(self):
        scenarios = list(self.scenarios.values())
        decision = trial_plan(scenarios, "decision")
        self.assertEqual(len(decision), 8)
        self.assertEqual({v for _, v in decision if v}, set((*PAYMENT_VARIANTS, *LOCK_VARIANTS)))
        self.assertEqual(len(trial_plan(scenarios, "regression")), 5)

    def test_distinct_transactions_are_installments_without_overpayment(self):
        for seed in (17, 83, 211):
            _, fx, _ = self.case("distinct_transactions", seed=seed)
            with closing(sqlite3.connect(fx.business_db)) as conn:
                totals = conn.execute(
                    "SELECT o.amount,sum(p.amount) FROM orders o JOIN payments p "
                    "ON p.order_id=o.id GROUP BY o.id"
                ).fetchall()
            self.assertTrue(all(paid <= due + 0.000001 for due, paid in totals))

    def test_escalation_retry_creates_one_persistent_task(self):
        case, fx, player = self.case("missing_receipt")
        scanner(player)
        args = {
            "reason": "Provider evidence missing or conflicting; preserve ledger for review.",
            "idempotency_key": "escalate",
        }
        self.assertTrue(player.call("escalate_incident", args).ok)
        self.assertEqual(len(self.rows(fx, "incident_escalations")), 1)
        verdict = self.grade(case, fx, player)
        self.assertTrue(verdict.passed)
        player.trace.disposition = verdict.disposition
        player.trace.dump(fx.root / "graded-trace.json")
        saved = json.loads((fx.root / "graded-trace.json").read_text(encoding="utf-8"))
        self.assertEqual(saved["disposition"], "unresolved_escalated")
