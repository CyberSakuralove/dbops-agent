"""Counterexamples for label leakage, collateral effects and execution integrity."""

import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from dbops_agent.config import Config
from dbops_agent.guard.execution import ApprovalService
from dbops_agent.incident.cases import build_case
from dbops_agent.incident.faults import build_fixture, fault_for
from dbops_agent.incident.identity import incident_id
from dbops_agent.incident.index_pair import PairEnvironment, baseline
from dbops_agent.judge.outcome import judge
from dbops_agent.record.ledger import Ledger
from dbops_agent.record.trace import Step, Trace
from dbops_agent.runtimes.bare_loop import BareLoop
from dbops_agent.tasks.scenario import load_scenarios
from dbops_agent.tools.base import Tool, ToolResult
from dbops_agent.tools.library import NoArgs, TerminateSession
from dbops_agent.tools.registry import ToolRegistry
from scripts.audit_shortcuts import Player, scanner
from tests.test_runtime import response


class AuditTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="dbops-counterexample-")
        self.root = Path(self.temp.name)
        self.scenarios = load_scenarios()
        self.serial = 0

    def tearDown(self):
        self.temp.cleanup()

    def case(self, index, seed=17):
        self.serial += 1
        return build_case(self.scenarios[index], self.root / str(self.serial), variant_seed=seed)

    def test_id_persistent_unique_and_unreadable_mapping(self):
        ids = set()
        for index in range(5):
            _, fx = self.case(index)
            ids.add(fx.alert_id)
            self.assertEqual(incident_id(fx.business_db), fx.alert_id)
            self.assertRegex(fx.alert_id, r"^INC-[0-9a-f]{32}$")
            player = Player(fx, fx.alert_id)
            self.assertFalse(
                player.call("query_business_db", {"sql": "SELECT * FROM incident_identity"}).ok
            )
        self.assertEqual(len(ids), 5)

    def test_hash_and_unicode_directory_is_read_correctly_by_judge(self):
        case, fx = build_case(self.scenarios[4], self.root / "实例#一", variant_seed=17)
        player = Player(fx, fx.alert_id)
        scanner(player)
        self.assertTrue(judge(case, player.trace, fx.workspace, fx.db_paths).passed)

    def test_key_conflict_is_persistently_audited(self):
        _, fx = self.case(3)
        player = Player(fx, fx.alert_id)
        self.assertTrue(
            player.call("raise_pool_ceiling", {"max_size": 20, "idempotency_key": "pool"}).ok
        )
        self.assertEqual(
            player.call("raise_pool_ceiling", {"max_size": 30, "idempotency_key": "pool"}).verdict,
            "key_conflict",
        )
        with closing(sqlite3.connect(fx.business_db)) as conn:
            self.assertEqual(
                conn.execute(
                    "SELECT count(*) FROM repair_log WHERE outcome='key_conflict'"
                ).fetchone()[0],
                1,
            )

    def test_unknown_usage_stops_before_effect_and_marks_billing_unknown(self):
        case, fx = self.case(1)

        class Mock(BareLoop):
            def _complete(self, messages):
                return response([("rebuild_search_index", {"idempotency_key": "fix"})]), {
                    "cache_hit": 0,
                    "cache_miss": 0,
                    "output": 0,
                    "usage_unknown": 1,
                }

        result = Mock(Config()).run(case, fx.workspace, fx.business_db, fx.metrics_db)
        self.assertEqual(result.trace.finished_reason, "usage_unknown")
        self.assertTrue(result.trace.billing_unknown)
        self.assertFalse(any(s.was_write for s in result.trace.steps))

    def test_runtime_prompt_ack_and_grade_share_identity(self):
        case, fx = self.case(4)
        captured = []

        class Mock(BareLoop):
            def _complete(self, messages):
                captured.extend(m["content"] for m in messages if m["role"] == "user")
                return {"content": "done"}, {"cache_hit": 0, "cache_miss": 0, "output": 0}

        Mock(Config()).run(case, fx.workspace, fx.business_db, fx.metrics_db)
        self.assertTrue(all(fx.alert_id in msg and case.id not in msg for msg in captured))
        player = Player(fx, fx.alert_id)
        result = player.call(
            "acknowledge_alert",
            {"classification": "false_alarm", "rationale": "healthy", "idempotency_key": "ack"},
        )
        self.assertIn(fx.alert_id, result.content)
        rows = json.loads(
            player.call(
                "query_business_db", {"sql": "SELECT alert_id FROM alert_acknowledgements"}
            ).content
        )
        self.assertEqual(rows[0]["alert_id"], fx.alert_id)
        self.assertTrue(judge(case, player.trace, fx.workspace, fx.db_paths).passed)

    def test_id_permutation_changes_lookup_but_not_state_policy(self):
        # Reusing IDs in ISOLATED test databases is a nuisance intervention, not
        # production identity generation. All 25 fault/ID combinations are tested.
        lookup_passes = 0
        from scripts.identity_bench import LEGACY

        for external in LEGACY:
            for index in range(5):
                for actor in ("scanner", "lookup"):
                    case, fx = self.case(index)
                    with closing(sqlite3.connect(fx.business_db)) as conn, conn:
                        conn.execute("UPDATE incident_identity SET id=?", (external,))
                    player = Player(fx, fx.alert_id)
                    if actor == "scanner":
                        scanner(player)
                    else:
                        name, args = LEGACY[external]
                        player.call(name, {**args, "idempotency_key": "lookup"})
                    passed = judge(case, player.trace, fx.workspace, fx.db_paths).passed
                    if actor == "scanner":
                        self.assertTrue(passed, (external, case.id))
                    else:
                        lookup_passes += passed
        # Randomized blocker IDs additionally invalidate the hardcoded session101.
        self.assertEqual(lookup_passes, 4)

    def test_release_waiters_preserves_every_other_field(self):
        case, fx = self.case(2)
        player = Player(fx, fx.alert_id)
        self.assertNotEqual(fx.blocker_id, 101)
        with closing(sqlite3.connect(fx.business_db)) as conn:
            before = conn.execute(
                "SELECT * FROM db_sessions WHERE blocked_by=? ORDER BY id", (fx.blocker_id,)
            ).fetchall()
        self.assertTrue(
            player.call(
                "terminate_session", {"session_id": fx.blocker_id, "idempotency_key": "kill"}
            ).ok
        )
        with closing(sqlite3.connect(fx.business_db)) as conn:
            after = conn.execute("SELECT * FROM db_sessions WHERE id != 101 ORDER BY id").fetchall()
            self.assertEqual(
                conn.execute("SELECT count(*) FROM db_sessions WHERE id=101").fetchone()[0], 1
            )
        expected = [tuple([*r[:4], None, *r[5:]]) for r in before]
        self.assertEqual(after, expected)
        self.assertTrue(judge(case, player.trace, fx.workspace, fx.db_paths).passed)

    def test_old_waiter_deletion_backend_is_rejected(self):
        class Broken(TerminateSession):
            def apply(self, ctx, args, conn):
                conn.execute("DELETE FROM db_sessions WHERE blocked_by=?", (args.session_id,))
                conn.execute("DELETE FROM db_sessions WHERE id=?", (args.session_id,))
                return ToolResult.success("done")

        case, fx = self.case(2)
        player = Player(fx, fx.alert_id)
        player.registry = ToolRegistry([Broken()])
        self.assertTrue(
            player.call(
                "terminate_session", {"session_id": fx.blocker_id, "idempotency_key": "kill"}
            ).ok
        )
        self.assertFalse(judge(case, player.trace, fx.workspace, fx.db_paths).passed)

    def test_l0_raise_cannot_lower_or_claim_missing_config_success(self):
        _, fx = self.case(4)
        player = Player(fx, fx.alert_id)
        self.assertFalse(
            player.call("raise_pool_ceiling", {"max_size": 1, "idempotency_key": "lower"}).ok
        )
        with closing(sqlite3.connect(fx.business_db)) as conn, conn:
            conn.execute("DELETE FROM service_config WHERE key='db.pool.max_size'")
        self.assertFalse(
            player.call("raise_pool_ceiling", {"max_size": 30, "idempotency_key": "missing"}).ok
        )

    def test_structured_truncation_is_valid_and_marked(self):
        _, fx = self.case(4)
        player = Player(fx, fx.alert_id)
        sql = (
            "SELECT a.name,b.name AS other,c.name AS third FROM customers a,customers b,customers c"
        )
        output = json.loads(player.call("query_business_db", {"sql": sql}).content)
        self.assertTrue(output["truncated"])
        self.assertLessEqual(output["returned_rows"], 60)
        output = json.loads(
            player.call(
                "query_business_db", {"sql": "SELECT '" + "x" * 2000 + "' AS value"}
            ).content
        )
        self.assertTrue(output["truncated"])
        self.assertLess(len(output["rows"][0]["value"]), 550)

    def test_unexpected_error_does_not_reveal_private_path(self):
        class Broken(Tool):
            name = "broken"
            args_model = NoArgs

            def run(self, ctx, args):
                raise OSError("private/f3_lock_contention/protected-state.json")

        _, fx = self.case(4)
        player = Player(fx, fx.alert_id)
        player.registry = ToolRegistry([Broken()])
        error = player.call("broken", {}).content
        self.assertNotIn("f3_lock_contention", error)
        self.assertNotIn("protected-state", error)

    def test_lag_releases_real_historical_samples(self):
        left = PairEnvironment(self.root / "left", progressing=True, metric_lag=3)
        right = PairEnvironment(self.root / "right", progressing=False, metric_lag=3)

        def samples(env):
            with closing(sqlite3.connect(env.fx.metrics_db)) as conn:
                return conn.execute(
                    "SELECT metric,value,ts FROM service_metrics ORDER BY id"
                ).fetchall()

        self.assertEqual(samples(left), samples(right))
        right.advance(2)
        self.assertEqual(len(samples(right)), 2)
        right.advance(1)
        rows = samples(right)
        self.assertEqual(rows[2][1:], (10.0, right.anchor.isoformat()))
        right.advance(1)
        self.assertEqual(samples(right)[:4], rows)

    def test_state_success_does_not_hide_run_interruption(self):
        env = PairEnvironment(self.root / "pair", progressing=False)
        baseline(env, "blind_rebuild")
        for reason in ("budget", "max_steps", "api_error", "unknown"):
            trace = Trace("index-pair", "test", "mock", 0, "none", finished_reason=reason)
            self.assertTrue(env.outcome(trace)["state_success"])
            self.assertFalse(env.outcome(trace)["passed"])

    def test_trace_length_mismatch_fails_without_raising(self):
        trace = Trace("case", "test", "mock", 0, "none")
        trace.append(Step(index=0))
        trace.hashes.clear()
        self.assertFalse(trace.verify())

    def test_parallel_batch_cannot_bypass_tool_call_limit(self):
        case, fx = self.case(1)

        class Mock(BareLoop):
            def _complete(self, messages):
                return response([("rebuild_search_index", {"idempotency_key": "fix"})] * 3), {
                    "cache_hit": 0,
                    "cache_miss": 1,
                    "output": 1,
                }

        result = Mock(Config(max_tool_calls_per_incident=2)).run(
            case, fx.workspace, fx.business_db, fx.metrics_db
        )
        self.assertEqual(result.trace.finished_reason, "max_tool_calls")
        self.assertFalse(any(s.was_write for s in result.trace.steps))

    def test_denial_cannot_be_renewed_with_new_key(self):
        _, fx = self.case(0)
        player = Player(fx, fx.alert_id)
        # Direct call avoids the Player harness that intentionally auto-approves.
        result = player.registry.call(
            "deduplicate_payments", {"idempotency_key": "one"}, player.ctx
        )[0]
        rid = json.loads(result.error)["request_id"]
        ApprovalService(fx.business_db).decide(rid, approve=False, actor="test", reason="deny")
        blocked = player.registry.call(
            "deduplicate_payments", {"idempotency_key": "two"}, player.ctx
        )[0]
        self.assertEqual(blocked.verdict, "approval_blocked")
        self.assertEqual(ApprovalService(fx.business_db).pending(), [])

    def test_operator_failure_returns_partial_trace(self):
        case, fx = self.case(0)

        class Mock(BareLoop):
            def _complete(self, messages):
                return response([("deduplicate_payments", {"idempotency_key": "fix"})]), {
                    "cache_hit": 0,
                    "cache_miss": 1,
                    "output": 1,
                }

        def broken(ctx, request):
            raise RuntimeError("host fixture path")

        result = Mock(Config(), approval_handler=broken).run(
            case, fx.workspace, fx.business_db, fx.metrics_db
        )
        self.assertEqual(result.trace.finished_reason, "operator_error")
        self.assertEqual(len(result.trace.steps), 1)
        self.assertTrue(result.trace.verify())

    def test_broken_evaluator_artifact_is_reported_as_infrastructure_error(self):
        case, fx = self.case(4)
        player = Player(fx, fx.alert_id)
        scanner(player)
        (fx.root / "protected-state.json").write_text("{broken", encoding="utf-8")
        verdict = judge(case, player.trace, fx.workspace, fx.db_paths)
        self.assertFalse(verdict.passed)
        self.assertEqual(verdict.attribution, "evaluator_error")

    def test_budget_token_and_api_failure_preserve_partial_trace(self):
        for mode in ("budget", "max_tokens", "api_error"):
            fx = build_fixture("f2_index_drift", self.root / mode)
            fault_for("f2_index_drift").inject(fx)
            case = self.scenarios[1].model_copy(deep=True)
            case.max_tokens = 11 if mode == "max_tokens" else 10000

            class Mock(BareLoop):
                n = 0

                def _complete(self, messages, mode=mode):
                    self.n += 1
                    if self.n == 2 and mode == "api_error":
                        raise OSError("provider secret path")
                    return response(
                        [("check_index_status", {})]
                        if self.n == 1
                        else [("rebuild_search_index", {"idempotency_key": "fix"})]
                    ), {"cache_hit": 0, "cache_miss": 10, "output": 0}

            result = Mock(Config()).run(
                case,
                fx.workspace,
                fx.business_db,
                fx.metrics_db,
                ledger=Ledger(budget_cny=0.000001 if mode == "budget" else 3),
            )
            self.assertEqual(result.trace.finished_reason, mode)
            self.assertTrue(result.trace.verify())
            saved = json.loads((fx.root / "runtime-trace.json").read_text(encoding="utf-8"))
            self.assertEqual(saved["finished_reason"], mode)
            self.assertTrue(result.trace.steps)
            self.assertFalse(any(s.tool_name == "rebuild_search_index" for s in result.trace.steps))
            self.assertFalse(judge(case, result.trace, fx.workspace, fx.db_paths).passed)
