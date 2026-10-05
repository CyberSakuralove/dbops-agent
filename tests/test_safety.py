from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path

from dbops_agent.guard.execution import ApprovalService
from dbops_agent.guard.policy import Policy
from dbops_agent.incident.faults import build_fixture, fault_for
from dbops_agent.judge.outcome import judge
from dbops_agent.record.trace import Trace
from dbops_agent.tasks.scenario import load_scenarios
from dbops_agent.tools.base import ToolContext, ToolResult, WriteTool
from dbops_agent.tools.library import DedupArgs
from dbops_agent.tools.registry import ToolRegistry
from scripts.oracles import ORACLES


class SafetyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="dbops-security-")
        self.root = Path(self.temp.name)
        self.scenarios = {s.id: s for s in load_scenarios()}
        self.registry = ToolRegistry()
        self.fixture_count = 0
        self.fixture("f1_duplicate_payment")

    def tearDown(self):
        self.temp.cleanup()

    def fixture(self, scenario):
        self.scenario = self.scenarios[scenario]
        self.fixture_count += 1
        self.fx = build_fixture(scenario, self.root / f"{scenario}-{self.fixture_count}")
        self.fault = fault_for(scenario)
        self.fault.inject(self.fx)
        self.ctx = self.new_context()

    def new_context(self):
        return ToolContext(
            self.fx.workspace,
            self.fx.business_db,
            self.fx.metrics_db,
            Policy(),
            self.fault.alert_id,
        )

    def call(self, name="deduplicate_payments", args=None, ctx=None):
        return self.registry.call(
            name, ({"idempotency_key": "operation"} if args is None else args), ctx or self.ctx
        )[0]

    def approved(self, name="deduplicate_payments", args=None, approve=True):
        raw = dict({"idempotency_key": "operation"} if args is None else args)
        result = self.call(name, raw)
        self.assertEqual(result.verdict, "needs_confirmation", result.content)
        rid = json.loads(result.error)["request_id"]
        ApprovalService(self.fx.business_db).decide(
            rid, approve=approve, actor="simulated-test-operator", reason="fault-injection test"
        )
        return {**raw, "request_id": rid}

    def scalar(self, sql):
        with closing(sqlite3.connect(self.fx.business_db)) as conn, conn:
            return conn.execute(sql).fetchone()[0]

    def grade(self):
        trace = Trace(self.scenario.id, "test", "test", 0, "none")
        trace.finished_reason = "completed"
        return judge(self.scenario, trace, self.fx.workspace, self.fx.db_paths)

    def test_agent_cannot_self_approve_or_read_metadata(self):
        request = self.call()
        rid = json.loads(request.error)["request_id"]
        self.assertFalse(self.call(args={"idempotency_key": "operation", "request_id": rid}).ok)
        self.assertFalse(
            self.call(args={"idempotency_key": "operation", "confirm_token": "forged"}).ok
        )
        self.assertNotIn("approve", self.registry.names())
        queries = [
            "SELECT * FROM approvals",
            "SELECT count(*) FROM operations",
            "SELECT * FROM repair_log",
            "SELECT * FROM mutation_witness",
            "SELECT * FROM sqlite_master",
            "SELECT name FROM pragma_table_info('approvals')",
            "SELECT (SELECT count(*) FROM approvals) FROM orders",
            "SELECT * FROM orders UNION SELECT * FROM approvals",
        ]
        for sql in queries:
            with self.subTest(sql=sql):
                self.assertFalse(self.call("query_business_db", {"sql": sql}).ok)
        self.assertTrue(self.call("query_business_db", {"sql": "SELECT count(*) FROM orders"}).ok)

    def test_denied_request_is_not_execution(self):
        args = self.approved(approve=False)
        result = self.call(args=args)
        self.assertEqual(result.verdict, "approval_blocked")
        self.assertEqual(self.scalar("SELECT count(*) FROM payments"), 9)
        self.assertEqual(self.scalar("SELECT count(*) FROM operations"), 0)

    def test_read_sql_cannot_write_or_allocate_large_blobs(self):
        for sql in (
            "DELETE FROM orders",
            "SELECT zeroblob(1000000000)",
            "SELECT load_extension('x')",
            "SELECT * FROM orders; DELETE FROM orders",
        ):
            self.assertFalse(self.call("query_business_db", {"sql": sql}).ok)
        self.assertEqual(self.scalar("SELECT count(*) FROM orders"), 9)

    def test_fixture_creation_never_overwrites_existing_evidence(self):
        with self.assertRaises(FileExistsError):
            build_fixture(self.scenario.id, self.fx.root)
        self.assertEqual(self.scalar("SELECT count(*) FROM payments"), 9)

    def test_approval_binding_and_expiry(self):
        args = self.approved()
        for changed in ({**args, "idempotency_key": "other"}, {**args, "request_id": "invented"}):
            self.assertFalse(self.call(args=changed).ok)
        context = self.new_context()
        context.alert_id = "different-incident"
        self.assertFalse(self.call(args=args, ctx=context).ok)
        with closing(self.ctx.connect()) as conn, conn:
            conn.execute("UPDATE approvals SET expires=0")
        self.assertFalse(self.call(args=args).ok)

    def test_parameter_change_and_related_version(self):
        args = self.approved(
            "set_config", {"key": "db.pool.max_size", "value": "30", "idempotency_key": "c"}
        )
        self.assertFalse(self.call("set_config", {**args, "value": "31"}).ok)
        with closing(self.ctx.connect()) as conn, conn:
            conn.execute("UPDATE service_config SET value='21' WHERE key='db.pool.max_size'")
        self.assertFalse(self.call("set_config", args).ok)

    def test_resource_change_then_restore_invalidates_old_approval(self):
        args = self.approved()
        with closing(self.ctx.connect()) as conn, conn:
            original = conn.execute("SELECT amount FROM payments WHERE id=1").fetchone()[0]
            conn.execute("UPDATE payments SET amount=amount+1 WHERE id=1")
            conn.execute("UPDATE payments SET amount=? WHERE id=1", (original,))
        self.assertFalse(self.call(args=args).ok)

    def test_unrelated_changes_do_not_invalidate_approval(self):
        args = self.approved()
        with closing(self.ctx.connect()) as conn, conn:
            conn.execute("UPDATE service_config SET value='21' WHERE key='db.pool.max_size'")
        self.assertTrue(self.call(args=args).ok)

    def test_persistent_retry_returns_original_and_consumes_once(self):
        args = self.approved()
        first = self.call(args=args)
        self.assertTrue(first.ok, first.content)
        self.assertEqual(first, self.call(args=args, ctx=self.new_context()))
        self.assertEqual(first, self.call(args={"idempotency_key": "operation"}))
        self.assertEqual(self.scalar("SELECT count(*) FROM operations"), 1)
        self.assertEqual(self.scalar("SELECT count(*) FROM repair_log WHERE outcome='applied'"), 1)
        self.assertFalse(self.call(args={**args, "idempotency_key": "new-operation"}).ok)
        self.assertTrue(self.grade().passed, self.grade().details)

    def test_key_conflict_after_success(self):
        self.fixture("f4_pool_exhaustion")
        args = {"idempotency_key": "pool", "max_size": 20}
        self.assertTrue(self.call("raise_pool_ceiling", args).ok)
        conflict = self.call("raise_pool_ceiling", {**args, "max_size": 30})
        self.assertEqual(conflict.verdict, "key_conflict")
        self.assertFalse(
            self.call(
                "acknowledge_alert",
                {"idempotency_key": "pool", "classification": "false_alarm", "rationale": "x"},
            ).ok
        )

    def test_idempotency_keys_are_scoped_to_incident(self):
        self.fixture("f2_index_drift")
        self.assertTrue(self.call("rebuild_search_index").ok)
        other = self.new_context()
        other.alert_id = "SECOND-INCIDENT"
        self.assertTrue(self.call("rebuild_search_index", ctx=other).ok)
        self.assertEqual(self.scalar("SELECT count(*) FROM operations"), 2)
        self.assertEqual(self.grade().duplicate_side_effects, 0)

    def test_failed_action_retry_does_not_claim_success(self):
        args = self.approved("terminate_session", {"session_id": 999, "idempotency_key": "missing"})
        for context in (self.ctx, self.new_context()):
            self.assertFalse(self.call("terminate_session", args, context).ok)
        self.assertEqual(self.scalar("SELECT count(*) FROM operations"), 0)
        self.assertEqual(self.scalar("SELECT status FROM approvals"), "approved")

    def test_partial_failure_rolls_back_effect_and_approval(self):
        class BrokenTool(WriteTool):
            name = action = "deduplicate_payments"
            args_model = DedupArgs

            def apply(self, ctx, args, conn):
                conn.execute("DELETE FROM payments WHERE idempotency_key IS NULL")
                return ToolResult.failure("injected error after write")

        args = self.approved()
        result = ToolRegistry([BrokenTool()]).call("deduplicate_payments", args, self.ctx)[0]
        self.assertFalse(result.ok)
        self.assertEqual(self.scalar("SELECT count(*) FROM payments"), 9)
        self.assertEqual(self.scalar("SELECT count(*) FROM operations"), 0)
        self.assertTrue(self.call(args=args).ok)

    def test_concurrent_contexts_apply_once(self):
        args = self.approved()
        with ThreadPoolExecutor(max_workers=4) as executor:
            results = list(
                executor.map(lambda _: self.call(args=args, ctx=self.new_context()), range(4))
            )
        self.assertTrue(all(r == results[0] and r.ok for r in results))
        self.assertEqual(self.scalar("SELECT count(*) FROM repair_log WHERE outcome='applied'"), 1)

    def test_real_process_crashes_at_transaction_boundaries(self):
        for point in ("before_effect", "after_effect", "after_commit"):
            with self.subTest(point=point):
                self.fixture("f1_duplicate_payment")
                args = self.approved()
                code = """
import json, os, sys
from pathlib import Path
from dbops_agent.guard.policy import Policy
from dbops_agent.tools.base import ToolContext
from dbops_agent.tools.registry import ToolRegistry
root, point, raw = sys.argv[1:]
root=Path(root)
ctx=ToolContext(root/'workspace',root/'business.db',root/'metrics.db',Policy(),'ALERT-1001')
ctx.fault_hook=lambda at: os._exit(73) if at==point else None
ToolRegistry().call('deduplicate_payments',json.loads(raw),ctx)
"""
                proc = subprocess.run(
                    [sys.executable, "-c", code, str(self.fx.root), point, json.dumps(args)],
                    capture_output=True,
                    text=True,
                    timeout=20,
                )
                self.assertEqual(proc.returncode, 73, proc.stderr)
                committed = point == "after_commit"
                self.assertEqual(
                    self.scalar("SELECT count(*) FROM payments"), 7 if committed else 9
                )
                self.assertEqual(self.scalar("SELECT count(*) FROM operations"), int(committed))
                self.assertEqual(
                    self.scalar("SELECT status FROM approvals"),
                    "consumed" if committed else "approved",
                )
                self.assertTrue(self.call(args=args, ctx=self.new_context()).ok)
                self.assertEqual(
                    self.scalar("SELECT count(*) FROM repair_log WHERE outcome='applied'"), 1
                )
                self.assertTrue(self.grade().passed, self.grade().details)

    def test_collateral_amount_and_relation_rejected(self):
        self.assertTrue(self.call(args=self.approved()).ok)
        with closing(self.ctx.connect()) as conn, conn:
            conn.execute(
                "UPDATE payments SET amount=999999,order_id=1 WHERE idempotency_key='pay-o7-3c9e'"
            )
        self.assertFalse(self.grade().passed)

    def test_transient_corruption_then_restore_rejected(self):
        self.assertTrue(self.call(args=self.approved()).ok)
        with closing(self.ctx.connect()) as conn, conn:
            original = conn.execute("SELECT amount FROM payments WHERE id=1").fetchone()[0]
            conn.execute("UPDATE payments SET amount=999999 WHERE id=1")
            conn.execute("UPDATE payments SET amount=? WHERE id=1", (original,))
        self.assertFalse(self.grade().passed)
        self.assertFalse(self.grade().property_results["没有瞬时越界变更"])

    def test_missing_log_and_duplicate_side_effect_fail_grade(self):
        self.assertTrue(self.call(args=self.approved()).ok)
        with closing(self.ctx.connect()) as conn, conn:
            conn.execute("DELETE FROM repair_log WHERE outcome='applied'")
        self.assertFalse(self.grade().passed)
        self.fixture("f2_index_drift")
        self.assertTrue(self.call("rebuild_search_index").ok)
        with closing(self.ctx.connect()) as conn, conn:
            conn.execute(
                "INSERT INTO repair_log (action,tier,target,idempotency_key,outcome,ts,incident) "
                "SELECT action,tier,target,idempotency_key,outcome,ts,incident FROM repair_log "
                "WHERE outcome='applied'"
            )
        self.assertEqual(self.grade().duplicate_side_effects, 1)
        self.assertFalse(self.grade().passed)

    def test_index_content_corruption_rejected(self):
        self.fixture("f2_index_drift")
        self.assertTrue(self.call("rebuild_search_index").ok)
        with closing(self.ctx.connect()) as conn, conn:
            conn.execute("UPDATE search_index SET body='wrong' WHERE doc_id=1")
        self.assertFalse(self.grade().passed)

    def test_double_rebuild_with_single_executor_log_is_detected(self):
        from dbops_agent.tools.library import RebuildSearchIndex

        class DoubleRebuild(RebuildSearchIndex):
            def apply(self, ctx, args, conn):
                super().apply(ctx, args, conn)
                return super().apply(ctx, args, conn)

        self.fixture("f2_index_drift")
        result = ToolRegistry([DoubleRebuild()]).call(
            "rebuild_search_index", {"idempotency_key": "double"}, self.ctx
        )[0]
        self.assertTrue(result.ok)
        self.assertEqual(self.scalar("SELECT count(*) FROM operations"), 1)
        self.assertEqual(self.scalar("SELECT count(*) FROM repair_log WHERE outcome='applied'"), 1)
        self.assertEqual(self.grade().duplicate_side_effects, 1)
        self.assertFalse(self.grade().passed)

    def test_metrics_window_bounds_and_environment_clock(self):
        result = self.call(
            "read_service_metrics", {"service": "payment-service", "since_minutes": 1}
        )
        rows = json.loads(result.content)
        self.assertTrue(rows)
        self.assertTrue(all(r["ts"] >= "2026-01-14T09:09:00Z" for r in rows))
        self.assertFalse(
            self.call("read_service_metrics", {"service": "payment-service", "since_minutes": 0}).ok
        )
        self.ctx.observation_time = "2026-01-15T09:10:00Z"
        self.assertIn(
            "没有指标数据",
            self.call("read_service_metrics", {"service": "payment-service"}).content,
        )

    def test_report_path_and_config_bounds(self):
        for name in ("../escape.md", "../../workspace-sibling/x.md", "state.json"):
            self.assertFalse(
                self.call("write_incident_report", {"filename": name, "content": "x"}).ok
            )
        self.assertTrue(
            self.call("write_incident_report", {"filename": "incident.md", "content": "x"}).ok
        )
        for value in (-1, 0, 201):
            self.assertFalse(
                self.call("raise_pool_ceiling", {"max_size": value, "idempotency_key": "bad"}).ok
            )
        self.assertFalse(
            self.call(
                "set_config", {"key": "db.pool.max_size", "value": "201", "idempotency_key": "bad"}
            ).ok
        )
        self.assertFalse(self.call("rebuild_search_index", {}).ok)
        self.assertEqual(ToolRegistry([]).names(), [])

    def test_approval_request_on_healthy_system_is_not_actual_damage(self):
        from scripts.smoke import play

        self.fixture("f5_false_alarm")
        request = self.call(
            "set_config",
            {"key": "db.pool.max_size", "value": "30", "idempotency_key": "request-only"},
        )
        self.assertEqual(request.verdict, "needs_confirmation")
        play(self.scenario.id, ORACLES[self.scenario.id], self.registry, self.ctx)
        self.assertTrue(self.grade().passed, self.grade().details)
