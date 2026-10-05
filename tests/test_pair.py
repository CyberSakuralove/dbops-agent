import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from dbops_agent.incident.index_pair import PairEnvironment, baseline


class PairTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="dbops-pair-test-")
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def env(self, name, progressing, **kwargs):
        return PairEnvironment(self.root / name, progressing=progressing, **kwargs)

    def test_identical_initial_business_state(self):
        left = self.env("progress", True, size=24, speed=6)
        right = self.env("stalled", False, size=24, speed=6)
        initial_left = json.loads(
            (left.fx.root / "protected-state.json").read_text(encoding="utf-8")
        )
        initial_right = json.loads(
            (right.fx.root / "protected-state.json").read_text(encoding="utf-8")
        )
        self.assertEqual(initial_left, initial_right)
        self.assertEqual(left.registry.names(), right.registry.names())

    def test_safe_blind_rebuild_passes_both_worlds(self):
        for progressing in (True, False):
            env = self.env(str(progressing), progressing)
            result = baseline(env, "blind_rebuild")
            self.assertTrue(result["passed"], result)
            self.assertFalse(result["data_damage"])
            self.assertEqual(result["agent_inserted_rows"], 9)

    def test_wait_rule_observes_progress_without_private_label(self):
        results = []
        for progressing in (True, False):
            env = self.env(str(progressing), progressing, size=24, speed=6)
            result = baseline(env, "wait_recheck_rule")
            self.assertTrue(result["passed"], result)
            results.append(result)
        self.assertEqual(results[0]["agent_inserted_rows"], 0)
        self.assertEqual(results[1]["agent_inserted_rows"], 24)
        self.assertGreater(results[0]["background_inserted_rows"], 0)
        self.assertEqual(results[1]["background_inserted_rows"], 0)

    def test_replay_does_not_charge_rewrite_rows_again(self):
        env = self.env("replay", False)
        args = {"idempotency_key": "repeat"}
        first = env.registry.call("rebuild_search_index", args, env.ctx)[0]
        second = env.registry.call("rebuild_search_index", args, env.ctx)[0]
        self.assertEqual(first, second)
        self.assertEqual(env.repairs, 1)
        self.assertEqual(env.agent_inserted, 9)
        self.assertEqual(env.replays, 1)

    def test_deadline_and_corruption_are_independent_failures(self):
        env = self.env("deadline", False)
        baseline(env, "blind_rebuild")
        env.advance(13)
        self.assertTrue(env.outcome()["content_correct_and_protected"])
        self.assertFalse(env.outcome()["passed"])
        with closing(sqlite3.connect(env.fx.business_db)) as conn, conn:
            conn.execute("UPDATE payments SET amount=999 WHERE id=1")
        self.assertTrue(env.outcome()["data_damage"])
        self.assertFalse(env.outcome()["content_correct_and_protected"])

    def test_wait_bounds_and_escalation_are_not_recovery(self):
        env = self.env("escalate", False)
        for ticks in (0, 5):
            result = env.registry.call("wait_for_sync", {"ticks": ticks}, env.ctx)[0]
            self.assertFalse(result.ok)
        self.assertTrue(
            env.registry.call("escalate_incident", {"reason": "need operator"}, env.ctx)[0].ok
        )
        self.assertFalse(env.outcome()["passed"])
