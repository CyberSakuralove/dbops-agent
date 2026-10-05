import json
import tempfile
import unittest
from pathlib import Path

from dbops_agent.config import Config
from dbops_agent.guard.execution import ApprovalService
from dbops_agent.incident.faults import build_fixture, fault_for
from dbops_agent.judge.report import group_traces
from dbops_agent.record.cassette import cache_key
from dbops_agent.record.ledger import Ledger
from dbops_agent.record.trace import Step, Trace
from dbops_agent.runtimes.bare_loop import BareLoop
from dbops_agent.tasks.scenario import load_scenarios


def response(calls):
    return {
        "content": None,
        "tool_calls": [
            {
                "id": str(i),
                "type": "function",
                "function": {"name": name, "arguments": json.dumps(args)},
            }
            for i, (name, args) in enumerate(calls)
        ],
    }


class RuntimeTests(unittest.TestCase):
    def test_cache_is_namespaced_by_actual_provider_request(self):
        request = {
            "model": "same-name",
            "messages": [{"role": "user", "content": "x"}],
            "tools": [],
            "temperature": 0,
        }
        self.assertNotEqual(
            cache_key(provider="https://one.example", **request),
            cache_key(provider="https://two.example", **request),
        )
        self.assertNotEqual(
            cache_key(provider="https://one.example", **request),
            cache_key(provider="https://one.example", **{**request, "temperature": 1}),
        )

    def test_replay_is_excluded_from_new_trial_statistics(self):
        fresh = Trace("case", "test", "bare", 1, "model", passed=True)
        replay = Trace("case", "test", "bare", 2, "model", passed=True)
        replay.append(Step(index=0, local_replay=True))
        self.assertEqual(group_traces([fresh, replay])["bare"].trials, 1)

    def test_local_replay_has_no_new_provider_usage(self):
        class MemoryCassette:
            def get(self, key):
                return {
                    "message": {"content": "replayed"},
                    "usage": {
                        "prompt_cache_hit_tokens": 100,
                        "prompt_cache_miss_tokens": 200,
                        "completion_tokens": 50,
                    },
                }

        runtime = BareLoop(Config())
        runtime.cassette = MemoryCassette()
        message, usage = runtime._complete([{"role": "user", "content": "x"}])
        self.assertEqual(message["content"], "replayed")
        self.assertEqual(usage, {"cache_hit": 0, "cache_miss": 0, "output": 0, "local_replay": 1})

    def test_parallel_tool_response_tokens_counted_once_and_cost_per_incident(self):
        with tempfile.TemporaryDirectory(prefix="dbops-runtime-") as temp:
            scenario = next(s for s in load_scenarios() if s.id == "f2_index_drift")
            fx = build_fixture(scenario.id, Path(temp) / "fixture")
            fault_for(scenario.id).inject(fx)
            ledger = Ledger(budget_cny=3)
            ledger.record("previous-incident", input_cache_miss=100_000, output=100)
            prior = ledger.spent_cny

            class MockLoop(BareLoop):
                calls = 0

                def _complete(self, messages):
                    self.calls += 1
                    message = (
                        response([("check_index_status", {}), ("describe_config", {})])
                        if self.calls == 1
                        else {"content": "done"}
                    )
                    return message, {"cache_hit": 5, "cache_miss": 10, "output": 3}

            result = MockLoop(Config()).run(
                scenario, fx.workspace, fx.business_db, fx.metrics_db, ledger=ledger
            )
            self.assertEqual(result.trace.tokens, {"cache_hit": 10, "cache_miss": 20, "output": 6})
            self.assertEqual(result.trace.spent_cny, round(ledger.spent_cny - prior, 6))
            self.assertTrue(result.trace.verify())

    def test_no_host_approval_handler_pauses_instead_of_self_approving(self):
        with tempfile.TemporaryDirectory(prefix="dbops-runtime-") as temp:
            scenario = next(s for s in load_scenarios() if s.id == "f1_duplicate_payment")
            fx = build_fixture(scenario.id, Path(temp) / "fixture")
            fault_for(scenario.id).inject(fx)

            class MockLoop(BareLoop):
                def _complete(self, messages):
                    return response([("deduplicate_payments", {"idempotency_key": "repair"})]), {
                        "cache_hit": 0,
                        "cache_miss": 10,
                        "output": 3,
                    }

            result = MockLoop(Config()).run(scenario, fx.workspace, fx.business_db, fx.metrics_db)
            self.assertEqual(result.trace.finished_reason, "approval_pending")
            self.assertEqual(ApprovalService(fx.business_db).pending()[0]["status"], "pending")
            self.assertEqual(len(result.trace.steps), 1)
