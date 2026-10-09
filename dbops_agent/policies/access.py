"""Bounded tool access. Policies receive observations, never fixture truth."""

import json

from ..record.trace import Step
from ..tools.base import ToolResult


class ObservationLimit(RuntimeError):
    pass


class Access:
    def __init__(
        self, registry, ctx, approval_handler, *, max_calls=40, deadline=None, execution_trace=None
    ):
        if max_calls < 2:
            raise ValueError("need at least two calls to conclude or escalate")
        self.registry, self.ctx = registry, ctx
        self.approval_handler = approval_handler
        self.max_calls, self.deadline = max_calls, deadline
        self.calls, self.clock = 0, 0
        self.trace = []
        self.denied = False
        self.read_reserve = 0
        self.execution_trace = execution_trace

    def remaining(self):
        return self.max_calls - self.calls

    def available(self, calls=1, ticks=None):
        return self.remaining() >= calls and (
            self.deadline is None or self.clock + (ticks or calls) <= self.deadline
        )

    def _call(self, name, args):
        ticks = args.get("ticks", 1) if name == "wait_for_service" else 1
        if not self.available(1, ticks) or (
            name == "query_business_db" and self.remaining() <= self.read_reserve
        ):
            raise ObservationLimit("public call/deadline budget exhausted")
        result, latency = self.registry.call(name, args, self.ctx)
        self.calls += 1
        self.clock += args.get("ticks", 1) if name == "wait_for_service" and result.ok else 1
        try:
            value = json.loads(result.content) if result.ok else None
        except (TypeError, json.JSONDecodeError):
            value = None
        if isinstance(value, dict) and "clock_tick_after_call" in value:
            self.clock = value["clock_tick_after_call"]
            self.deadline = value["deadline_tick"]
        self.trace.append(
            {
                "tool": name,
                "args": dict(args),
                "ok": result.ok,
                "verdict": result.verdict,
                "result": result.content,
                "clock_after": self.clock,
            }
        )
        if self.execution_trace is not None:
            tool = self.registry.get(name)
            self.execution_trace.append(
                Step(
                    index=len(self.execution_trace.steps),
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
        return result, value

    def call(self, name, args=None):
        args = dict(args or {})
        result, value = self._call(name, args)
        if result.verdict == "needs_confirmation":
            request = json.loads(result.error)
            if not self.available(1, 2):
                raise ObservationLimit("insufficient budget for independent approval and retry")
            if request["status"] == "pending":
                allowed = self.approval_handler(self.ctx, request["request_id"])
                self.clock += 1  # independent decision, not a policy tool call
                if not allowed:
                    self.denied = True
                    return ToolResult.failure(
                        "Independent operator denied", "approval_denied"
                    ), None
            # Never carry this request_id into a subsequent planning iteration.
            result, value = self._call(name, {**args, "request_id": request["request_id"]})
        return result, value

    def query(self, sql):
        result, value = self.call("query_business_db", {"sql": sql})
        if not result.ok:
            raise ObservationLimit("query failed; preserve data")
        return value
