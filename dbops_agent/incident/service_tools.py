"""Public observations and transactional actions for the bounded service simulation.

Tools know schema/contracts, never episode names or evaluator files. Queue/history
are host-owned; raw Agent SQL continues to expose only the original public tables.
"""

import json
from contextlib import closing

from pydantic import BaseModel, Field

from ..tools.base import Tool, ToolResult
from ..tools.library import NoArgs, RaisePoolCeiling, SetConfig, TerminateSession


def encoded(value):
    return ToolResult.success(json.dumps(value, ensure_ascii=False))


class InspectService(Tool):
    name = "inspect_service"
    args_model = NoArgs
    description = (
        "读取服务探针和请求队列历史，含采样 tick、pending、oldest_age、完成/超时数。"
        "健康契约：待处理最老年龄<=2 tick，累计超时=0。观察可能缺失或滞后；"
        "age>2才新鲜度不足，不能用业务表行数证明服务健康。history覆盖告警窗口-4至今。"
    )

    def run(self, ctx, args):
        with closing(ctx.connect()) as conn:
            clock, lag, available = conn.execute(
                "SELECT tick,lag,available FROM service_clock"
            ).fetchone()
            history = (
                [
                    dict(r)
                    for r in conn.execute(
                        "SELECT * FROM service_history WHERE tick<=? ORDER BY tick DESC LIMIT 64",
                        (clock - lag,),
                    )
                ]
                if available
                else []
            )
        history.reverse()
        return encoded(
            {
                "history": history,
                "available": bool(history),
                "freshness_limit_ticks": 2,
                "history_truncated": bool(history) and history[0]["tick"] > -4,
            }
        )


class ObserveTransactions(Tool):
    name = "observe_transactions"
    args_model = NoArgs
    description = (
        "当前会话blocked_by图及事务已完成工作量；多时刻进度可用于比较。"
        "completed_units是仿真工作计数；终止真实blocker可释放连接，但中止已有工作计入成本。"
        "既不承诺真实数据库回滚，也不把state/年龄当异常标签。"
    )

    def run(self, ctx, args):
        with closing(ctx.connect()) as conn:
            sessions = [dict(r) for r in conn.execute("SELECT * FROM db_sessions ORDER BY id")]
            progress = [
                dict(r)
                for r in conn.execute(
                    "SELECT "
                    "session_id,total_units,completed_units,last_progress_tick,status "
                    "FROM service_progress ORDER BY session_id"
                )
            ]
        return encoded({"sessions": sessions, "progress": progress})


class CapacityPlan(Tool):
    name = "read_capacity_plan"
    args_model = NoArgs
    description = (
        "只读已批准的连接上界、每连接资源单位和资源预算，不包含推荐值或根因。"
        "提高连接数不能提高数据库自身每tick处理能力，必须复查实际请求进度。"
    )

    def run(self, ctx, args):
        with closing(ctx.connect()) as conn:
            plan = dict(conn.execute("SELECT * FROM service_capacity_plan").fetchone())
            plan["pool_size"] = int(
                conn.execute(
                    "SELECT value FROM service_config WHERE key='db.pool.max_size'"
                ).fetchone()[0]
            )
        return encoded(plan)


class WaitArgs(BaseModel):
    ticks: int = Field(ge=1, le=4)


class WaitService(Tool):
    name = "wait_for_service"
    args_model = WaitArgs
    description = "等待1～4个逻辑tick，推进请求和后台事务；需要另行观察验证。"

    def run(self, ctx, args):
        return encoded({"waited_ticks": args.ticks})


def capacity_allowed(conn, value):
    approved, units, budget = conn.execute(
        "SELECT approved_max,units_per_connection,resource_budget FROM service_capacity_plan"
    ).fetchone()
    return 1 <= value <= approved and value * units <= budget


class ServiceRaisePool(RaisePoolCeiling):
    description = (
        RaisePoolCeiling.description + " 同一事务中核验公开capacity plan的批准上界和资源预算。"
    )

    def apply(self, ctx, args, conn):
        if not capacity_allowed(conn, args.max_size):
            return ToolResult.failure("超出公开连接上界或资源预算", "refused")
        return super().apply(ctx, args, conn)


class ServiceSetConfig(SetConfig):
    description = "L1独立审批。此服务契约仅允许修改连接池；与L0扩容使用同一公开容量约束。"

    def apply(self, ctx, args, conn):
        if args.key != "db.pool.max_size" or not capacity_allowed(conn, int(args.value)):
            return ToolResult.failure("此服务的配置变更超出公开容量契约", "refused")
        return super().apply(ctx, args, conn)


class ServiceTerminate(TerminateSession):
    description = (
        "L1独立审批。只允许终止当前实际阻塞其他会话的目标；执行时重新检查blocked_by。"
        "释放直接等待者，并记录aborted工作量。进度正常的blocker也允许终止，成本单列。"
    )

    def apply(self, ctx, args, conn):
        if not conn.execute(
            "SELECT 1 FROM db_sessions WHERE blocked_by=?", (args.session_id,)
        ).fetchone():
            return ToolResult.failure("目标当前没有直接等待者，拒绝终止无关会话", "refused")
        result = super().apply(ctx, args, conn)
        if result.ok:
            conn.execute(
                "UPDATE service_progress SET status='aborted' WHERE session_id=? "
                "AND status='active'",
                (args.session_id,),
            )
        return result
