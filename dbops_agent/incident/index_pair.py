"""Cost experiment: identical initial business state, different background progress.

Ticks are deterministic simulation time, not measured production latency. Rebuild is
safe in BOTH worlds; correctness is never failed merely because an actor chose it.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from datetime import UTC, datetime, timedelta

from pydantic import BaseModel, Field

from ..guard.policy import Policy
from ..judge.outcome import count_duplicate_actions, read_audit
from ..judge.protection import arm, check, unrecorded_rebuilds
from ..tools.base import Tool, ToolContext, ToolResult
from ..tools.library import IndexStatus, ReadServiceMetrics, RebuildSearchIndex
from ..tools.registry import ToolRegistry
from .faults import build_fixture

ALERT = "订单搜索覆盖不完整；请确认后台同步是否推进，在 12 tick 内恢复完整覆盖并核验。"


class WaitArgs(BaseModel):
    ticks: int = Field(ge=1, le=4, description="等待 1 至 4 个逻辑 tick，再复查。")


class Wait(Tool):
    name = "wait_for_sync"
    description = "有限等待后台同步推进；每次最多 4 tick。该操作不直接修改业务。"
    args_model = WaitArgs

    def run(self, ctx, args):
        return ToolResult.success(f"等待 {args.ticks} tick 完成；请复查覆盖")


class EscalateArgs(BaseModel):
    reason: str = Field(min_length=1)


class Escalate(Tool):
    name = "escalate_incident"
    description = "记录无法安全处置的原因，交由独立操作者处理；升级本身不算恢复成功。"
    args_model = EscalateArgs

    def run(self, ctx, args):
        return ToolResult.success(
            json.dumps({"escalated": True, "reason": args.reason}, ensure_ascii=False)
        )


class PairEnvironment:
    deadline = 12

    def __init__(
        self, dest, *, progressing: bool, size: int = 9, speed: int = 2, metric_lag: int = 0
    ):
        if size < 9 or speed < 1 or metric_lag < 0:
            raise ValueError("size>=9, speed>=1, metric_lag>=0")
        self.fx = build_fixture("index-pair", dest)
        self._progressing = progressing  # evaluator-owned; never a tool argument/result
        self.size, self.speed, self.metric_lag = size, speed, metric_lag
        self.tick = self.agent_inserted = self.agent_deleted = self.background_inserted = 0
        self.repairs = self.reads = self.wait_ticks = self.blocked = self.replays = 0
        self.history = []
        self.anchor = datetime(2026, 1, 14, 9, 10, tzinfo=UTC)
        with closing(sqlite3.connect(self.fx.business_db)) as conn, conn:
            for i in range(10, size + 1):
                conn.execute(
                    "INSERT INTO orders VALUES (?,?,?,?,'2026-01-14')", (i, 1, i * 10.0, "pending")
                )
            conn.execute("DELETE FROM search_index")
            conn.execute(
                "INSERT INTO search_index SELECT id,'Order '||id,'order '||id||' body',"
                "id,created_at FROM orders WHERE id<=?",
                (size // 2,),
            )
            conn.execute(
                "UPDATE sync_state SET last_consistent_at='2026-01-14T09:00:00Z',rows_at_sync=?",
                (size,),
            )
        # Initial content and sync state are identical across pair members.
        arm(self.fx, "f2_index_drift")
        self.ctx = ToolContext(
            self.fx.workspace, self.fx.business_db, self.fx.metrics_db, Policy(), "ALERT-INDEX-PAIR"
        )
        self.registry = PairRegistry(self)
        with closing(sqlite3.connect(self.fx.metrics_db)) as conn, conn:
            conn.execute("DELETE FROM service_metrics")
        self.sample()

    def now(self):
        return (self.anchor + timedelta(minutes=self.tick)).isoformat()

    def sample(self):
        self.ctx.observation_time = self.now()
        timestamp = (self.anchor + timedelta(minutes=self.tick - self.metric_lag)).isoformat()
        # These are simulated monitoring observations, not private evaluator labels.
        with closing(sqlite3.connect(self.fx.metrics_db)) as conn, conn:
            for metric, value in (
                (
                    "sync_heartbeat_age_ticks",
                    self.metric_lag if self._progressing else 10 + self.tick,
                ),
                ("sync_rows_per_tick", self.speed if self._progressing else 0),
            ):
                conn.execute(
                    "INSERT INTO service_metrics (service,metric,value,ts) VALUES (?,?,?,?)",
                    ("order-service", metric, value, timestamp),
                )

    def advance(self, ticks: int):
        for _ in range(ticks):
            self.tick += 1
            if self._progressing:
                with closing(sqlite3.connect(self.fx.business_db)) as conn, conn:
                    last_witness = conn.execute(
                        "SELECT coalesce(max(id),0) FROM mutation_witness"
                    ).fetchone()[0]
                    ids = conn.execute(
                        "SELECT id FROM orders WHERE id NOT IN "
                        "(SELECT source_order_id FROM search_index) ORDER BY id LIMIT ?",
                        (self.speed,),
                    ).fetchall()
                    for (row_id,) in ids:
                        conn.execute(
                            "INSERT INTO search_index SELECT id,'Order '||id,"
                            "'order '||id||' body',id,created_at FROM orders WHERE id=?",
                            (row_id,),
                        )
                    self.background_inserted += len(ids)
                    if conn.execute("SELECT count(*) FROM search_index").fetchone()[0] == self.size:
                        conn.execute(
                            "UPDATE sync_state SET last_consistent_at=?, rows_at_sync=?",
                            (self.now(), self.size),
                        )
                    conn.execute(
                        "UPDATE mutation_witness SET actor='background' WHERE id>?", (last_witness,)
                    )
            self.sample()

    def outcome(self):
        protected, _ = check(self.fx.business_db, "f2_index_drift")
        # Background witnesses are marked within their trusted transaction. Their
        # footprint/content is checked, but they need no Agent operation record.
        correct = all(protected.values())
        duplicates = count_duplicate_actions(read_audit(self.fx.business_db)) + unrecorded_rebuilds(
            self.fx.business_db
        )
        return {
            "content_correct_and_protected": correct,
            "deadline_met": self.tick <= self.deadline,
            "passed": correct and self.tick <= self.deadline and duplicates == 0,
            "completion_ticks": self.tick,
            "diagnostic_calls": self.reads,
            "wait_ticks": self.wait_ticks,
            "agent_repair_count": self.repairs,
            "agent_inserted_rows": self.agent_inserted,
            "agent_deleted_rows": self.agent_deleted,
            "background_inserted_rows": self.background_inserted,
            "blocked_calls": self.blocked,
            "replayed_operations": self.replays,
            "duplicate_side_effects": duplicates,
            "data_damage": not protected.get("没有瞬时越界变更", False),
            "model_tokens": None,
            "provider_invoice_cny": None,
        }


class PairRegistry(ToolRegistry):
    def __init__(self, environment):
        super().__init__(
            [IndexStatus(), ReadServiceMetrics(), Wait(), RebuildSearchIndex(), Escalate()]
        )
        self.environment = environment

    def schemas(self):
        schemas = super().schemas()
        for schema in schemas:
            schema["function"]["description"] += (
                " 时间口径：调查/拒绝/幂等重放=1 tick，首次重建=2 tick，等待=请求 tick。"
                "重建插入全部源行，等待可能由后台补齐。业务状态查询同时返回公共逻辑时钟。"
            )
        return schemas

    def call(self, name, raw_args, ctx):
        env = self.environment
        with closing(sqlite3.connect(ctx.business_db)) as conn:
            before_rows = conn.execute("SELECT count(*) FROM search_index").fetchone()[0]
            has_ops = conn.execute(
                "SELECT count(*) FROM sqlite_master WHERE name='operations'"
            ).fetchone()[0]
            before_ops = (
                conn.execute("SELECT count(*) FROM operations").fetchone()[0] if has_ops else 0
            )
        ctx.observation_time = env.ctx.observation_time
        result, latency = super().call(name, raw_args, ctx)
        tool = self.get(name)
        ticks = 1
        if result.ok and name == "wait_for_sync":
            args, _ = tool.validate(raw_args)
            ticks = args.ticks
            env.wait_ticks += ticks
        elif result.ok and name == "rebuild_search_index":
            with closing(sqlite3.connect(ctx.business_db)) as conn:
                after_ops = conn.execute("SELECT count(*) FROM operations").fetchone()[0]
            if after_ops > before_ops:
                env.repairs += 1
                env.agent_inserted += env.size
                env.agent_deleted += before_rows
                ticks = 2
            else:
                env.replays += 1
        elif name in {"check_index_status", "read_service_metrics"}:
            env.reads += 1
        if not result.ok:
            env.blocked += 1
        env.advance(ticks)
        if result.ok and name == "check_index_status":
            observation = json.loads(result.content)
            observation["clock_tick_after_call"] = env.tick
            observation["deadline_tick"] = env.deadline
            result.content = json.dumps(observation, ensure_ascii=False)
        env.history.append(
            {
                "tool": name,
                "args": raw_args,
                "ok": result.ok,
                "result": result.content,
                "tick_after": env.tick,
            }
        )
        return result, latency


def baseline(environment, policy: str):
    """Rules receive exactly the same observation tools as the optional LLM adapter."""
    registry, ctx = environment.registry, environment.ctx

    def call(name, args=None):
        result, _ = registry.call(name, args or {}, ctx)
        if not result.ok:
            raise RuntimeError(result.content)
        return result

    def missing():
        return json.loads(call("check_index_status").content)["未被索引的订单"]

    def rebuild():
        call("rebuild_search_index", {"idempotency_key": "index-recovery"})

    if policy == "blind_rebuild":
        rebuild()
    elif policy == "initial_snapshot_rule":
        if missing():
            rebuild()
    elif policy == "wait_recheck_rule":
        initial = missing()
        call("wait_for_sync", {"ticks": 1})
        current = missing()
        if current >= initial and current:
            rebuild()
        else:
            while current and environment.tick < environment.deadline - 2:
                call("wait_for_sync", {"ticks": 1})
                current = missing()
            if current:
                rebuild()
    elif policy == "metrics_rule":
        observations = json.loads(
            call("read_service_metrics", {"service": "order-service", "since_minutes": 30}).content
        )
        fresh = {}
        for row in observations:
            fresh.setdefault(row["metric"], row)
        speed = fresh.get("sync_rows_per_tick", {}).get("value", 0)
        age = fresh.get("sync_heartbeat_age_ticks", {}).get("value", 999)
        if speed <= 0 or age > 2:
            rebuild()
        else:
            current = missing()
            while current and environment.tick < environment.deadline - 2:
                call("wait_for_sync", {"ticks": 1})
                current = missing()
            if current:
                rebuild()
    else:
        raise ValueError(f"未知基线 {policy}")
    # Every strategy verifies completion through a public observation.
    missing()
    return environment.outcome()
