"""工具集。

读工具让 Agent 去**确认**一个故障，而不是假设它存在。这个区别正是本项目的要点：
误报场景只有靠核对业务状态、而不是相信看板的 Agent 才能通过；重复写入场景也只有
查数据而不是看指标的 Agent 才能诊断出来。

写工具是分级的。它们的描述里明说了层级，因为真实的运维手册会写清哪一步需要签核，
把这件事藏起来会让护栏变成一个陷阱而不是一道管控。
"""

from __future__ import annotations

import json
from typing import Literal

from pydantic import BaseModel, Field

from .base import Tool, ToolContext, ToolResult, WriteTool

MAX_ROWS = 60
MAX_CHARS = 6_000


def _truncate(text: str, limit: int = MAX_CHARS) -> str:
    return text if len(text) <= limit else text[:limit] + f"\n... [已截断 {len(text) - limit} 字符]"


def _rows(conn, sql: str, params: tuple = ()) -> list[dict]:  # noqa: ANN001
    return [dict(r) for r in conn.execute(sql, params).fetchall()]


# =====================================================================================
# 读工具
# =====================================================================================


class MetricsArgs(BaseModel):
    service: str = Field(description="服务名，例如 'order-service' 或 'payment-service'。")
    since_minutes: int = Field(default=30, description="回溯多少分钟。")


class ReadServiceMetrics(Tool):
    name = "read_service_metrics"
    description = (
        "读取某个服务的观测数据。这是监控系统的认知，**不是事实来源**，可能滞后或出错。"
    )
    args_model = MetricsArgs

    def run(self, ctx: ToolContext, args: MetricsArgs) -> ToolResult:
        conn = ctx.connect("metrics")
        try:
            rows = _rows(
                conn,
                "SELECT metric, value, ts FROM service_metrics "
                "WHERE service = ? ORDER BY ts DESC LIMIT ?",
                (args.service, MAX_ROWS),
            )
        finally:
            conn.close()
        if not rows:
            return ToolResult.success(f"{args.service!r} 没有指标数据")
        return ToolResult.success(_truncate(json.dumps(rows, indent=2, ensure_ascii=False)))


class QueryArgs(BaseModel):
    sql: str = Field(description="一条只读的 SELECT 语句。")
    note: str = Field(default="", description="你为什么要查这一条。")


class QueryBusinessDb(Tool):
    name = "query_business_db"
    description = (
        "对权威业务库执行只读 SELECT。"
        "表：customers、orders、payments、products、search_index、sync_state、"
        "service_config、db_sessions、alert_acknowledgements、repair_log。"
    )
    args_model = QueryArgs

    def run(self, ctx: ToolContext, args: QueryArgs) -> ToolResult:
        sql = args.sql.strip().rstrip(";")
        if not sql.lower().startswith("select"):
            return ToolResult.failure("只允许 SELECT 语句")
        conn = ctx.connect()
        try:
            rows = _rows(conn, sql)
        except Exception as exc:  # noqa: BLE001
            return ToolResult.failure(f"查询失败: {exc}")
        finally:
            conn.close()
        if not rows:
            return ToolResult.success("[]（无数据行）")
        return ToolResult.success(_truncate(json.dumps(rows[:MAX_ROWS], indent=2, ensure_ascii=False)))


class NoArgs(BaseModel):
    pass


class ListSessions(Tool):
    name = "list_sessions"
    description = "列出实时数据库会话，包含哪个会话正在阻塞哪个。"
    args_model = NoArgs

    def run(self, ctx: ToolContext, args: NoArgs) -> ToolResult:  # noqa: ARG002
        conn = ctx.connect()
        try:
            rows = _rows(
                conn,
                "SELECT id, service, state, started_at, blocked_by, query FROM db_sessions "
                "ORDER BY id",
            )
        finally:
            conn.close()
        if not rows:
            return ToolResult.success("当前没有活动会话")
        return ToolResult.success(_truncate(json.dumps(rows, indent=2, ensure_ascii=False)))


class DescribeConfig(Tool):
    name = "describe_config"
    description = "读取权威的服务配置项取值。"
    args_model = NoArgs

    def run(self, ctx: ToolContext, args: NoArgs) -> ToolResult:  # noqa: ARG002
        conn = ctx.connect()
        try:
            rows = _rows(conn, "SELECT key, value, description FROM service_config ORDER BY key")
        finally:
            conn.close()
        return ToolResult.success(_truncate(json.dumps(rows, indent=2, ensure_ascii=False)))


class IndexStatus(Tool):
    name = "check_index_status"
    description = (
        "把派生的 search_index 与它的源数据（orders）做比对，并给出记录的同步状态。"
    )
    args_model = NoArgs

    def run(self, ctx: ToolContext, args: NoArgs) -> ToolResult:  # noqa: ARG002
        conn = ctx.connect()
        try:
            orders = conn.execute("SELECT COUNT(*) AS n FROM orders").fetchone()["n"]
            indexed = conn.execute("SELECT COUNT(*) AS n FROM search_index").fetchone()["n"]
            orphans = conn.execute(
                "SELECT COUNT(*) AS n FROM search_index si "
                "LEFT JOIN orders o ON o.id = si.source_order_id WHERE o.id IS NULL"
            ).fetchone()["n"]
            missing = conn.execute(
                "SELECT COUNT(*) AS n FROM orders o "
                "LEFT JOIN search_index si ON si.source_order_id = o.id WHERE si.doc_id IS NULL"
            ).fetchone()["n"]
            sync = _rows(conn, "SELECT * FROM sync_state")
        finally:
            conn.close()
        return ToolResult.success(
            _truncate(
                json.dumps(
                    {
                        "订单总数": orders,
                        "已索引": indexed,
                        "孤儿索引行": orphans,
                        "未被索引的订单": missing,
                        "同步状态": sync,
                    },
                    indent=2,
                    ensure_ascii=False,
                )
            )
        )


# =====================================================================================
# 写工具
# =====================================================================================


class ConfirmableArgs(BaseModel):
    """所有分级动作共用的形状。

    `confirm_token` 对 L1 动作是必需的，对 L0 动作会被忽略。先不带它调用一次即可拿到令牌。
    """

    idempotency_key: str | None = Field(
        default=None,
        description=(
            "本次逻辑修复的稳定标识。当重试一次可能已经执行过的修复时，"
            "**必须传同一个键**，以免重复施加。"
        ),
    )
    confirm_token: str | None = Field(
        default=None, description="上一次需要确认的调用所返回的令牌。"
    )


class RebuildIndexArgs(ConfirmableArgs):
    pass


class RebuildSearchIndex(WriteTool):
    name = "rebuild_search_index"
    description = (
        "L0（无需确认直接执行）。从权威的 orders 表重建派生的 search_index。"
        "可逆且幂等。"
    )
    args_model = RebuildIndexArgs
    action = "rebuild_search_index"

    def apply(self, ctx: ToolContext, args: RebuildIndexArgs) -> ToolResult:
        conn = ctx.connect()
        try:
            conn.execute("DELETE FROM search_index")
            conn.execute(
                "INSERT INTO search_index (doc_id,title,body,source_order_id,indexed_at) "
                "SELECT id, 'Order ' || id, 'order ' || id || ' body', id, created_at FROM orders"
            )
            n = conn.execute("SELECT COUNT(*) AS n FROM search_index").fetchone()["n"]
            conn.execute(
                "UPDATE sync_state SET last_consistent_at = datetime('now'), rows_at_sync = ? "
                "WHERE structure = 'search_index'",
                (n,),
            )
            conn.commit()
        finally:
            conn.close()
        return ToolResult.success(f"已从 orders 重建搜索索引，共索引 {n} 篇文档")


class RaisePoolArgs(ConfirmableArgs):
    max_size: int = Field(description="新的连接池最大连接数。")


class RaisePoolCeiling(WriteTool):
    name = "raise_pool_ceiling"
    description = (
        "L0（无需确认直接执行）。把 db.pool.max_size 调高。可逆的调参动作。"
    )
    args_model = RaisePoolArgs
    action = "raise_pool_ceiling"

    def apply(self, ctx: ToolContext, args: RaisePoolArgs) -> ToolResult:
        conn = ctx.connect()
        try:
            conn.execute(
                "UPDATE service_config SET value = ? WHERE key = 'db.pool.max_size'",
                (str(args.max_size),),
            )
            conn.commit()
        finally:
            conn.close()
        return ToolResult.success(f"db.pool.max_size 已设为 {args.max_size}")


class AcknowledgeArgs(BaseModel):
    classification: Literal["true_incident", "false_alarm", "inconclusive"]
    rationale: str = Field(description="支撑该判定的具体证据。")
    idempotency_key: str | None = None
    confirm_token: str | None = None


class AcknowledgeAlert(WriteTool):
    name = "acknowledge_alert"
    description = (
        "L0。记录对当前告警的分诊结论。"
        "只有在**已核实业务状态健康**的情况下才可使用 classification='false_alarm'。"
    )
    args_model = AcknowledgeArgs
    action = "acknowledge_alert"

    def apply(self, ctx: ToolContext, args: AcknowledgeArgs) -> ToolResult:
        conn = ctx.connect()
        try:
            conn.execute(
                "INSERT INTO alert_acknowledgements (alert_id,classification,rationale,ts) "
                "VALUES (?,?,?,datetime('now'))",
                (ctx.alert_id, args.classification, args.rationale),
            )
            conn.commit()
        finally:
            conn.close()
        return ToolResult.success(f"告警 {ctx.alert_id} 已确认，结论为 {args.classification}")


class DedupArgs(ConfirmableArgs):
    pass


class DeduplicatePayments(WriteTool):
    name = "deduplicate_payments"
    description = (
        "L1（**需要确认**）。删除不带幂等键、且其 (order_id, customer_id, amount) "
        "已被一条带键记录覆盖的支付行。这会销毁数据，无法撤销。"
    )
    args_model = DedupArgs
    action = "deduplicate_payments"

    def apply(self, ctx: ToolContext, args: DedupArgs) -> ToolResult:
        conn = ctx.connect()
        try:
            before = conn.execute("SELECT COUNT(*) AS n FROM payments").fetchone()["n"]
            # 重复行 = 一条无键行，而它的 (order_id, customer_id, amount) 已经被一条带键行覆盖。
            # 这些正是一次丢了键的重试留下的纯粹损害。
            conn.execute(
                "DELETE FROM payments WHERE idempotency_key IS NULL AND EXISTS ("
                "  SELECT 1 FROM payments k WHERE k.idempotency_key IS NOT NULL "
                "    AND k.order_id = payments.order_id "
                "    AND k.customer_id = payments.customer_id "
                "    AND k.amount = payments.amount"
                ")"
            )
            after = conn.execute("SELECT COUNT(*) AS n FROM payments").fetchone()["n"]
            conn.commit()
        finally:
            conn.close()
        return ToolResult.success(f"支付行从 {before} 行减少到 {after} 行")


class TerminateArgs(ConfirmableArgs):
    session_id: int = Field(description="要终止的数据库会话 id。")


class TerminateSession(WriteTool):
    name = "terminate_session"
    description = (
        "L1（**需要确认**）。终止一个实时数据库会话。该会话中未提交的工作会被回滚并丢失。"
        "不可逆。"
    )
    args_model = TerminateArgs
    action = "terminate_session"

    def apply(self, ctx: ToolContext, args: TerminateArgs) -> ToolResult:
        conn = ctx.connect()
        try:
            row = conn.execute(
                "SELECT id FROM db_sessions WHERE id = ?", (args.session_id,)
            ).fetchone()
            if row is None:
                return ToolResult.failure(f"不存在 id={args.session_id} 的会话")
            conn.execute("DELETE FROM db_sessions WHERE blocked_by = ?", (args.session_id,))
            conn.execute("DELETE FROM db_sessions WHERE id = ?", (args.session_id,))
            conn.commit()
        finally:
            conn.close()
        return ToolResult.success(
            f"会话 {args.session_id} 已终止；被它阻塞的等待者已释放"
        )


class SetConfigArgs(ConfirmableArgs):
    key: str
    value: str


class SetConfig(WriteTool):
    name = "set_config"
    description = (
        "L1（**需要确认**）。修改一个服务配置项。"
        "把 db.statement_timeout_ms 调到 30000 以上、或把 api.rate_limit_per_min 调到 600 "
        "以上，会被直接拒绝：这两者都是在掩盖资源饱和，而不是解决问题。"
    )
    args_model = SetConfigArgs
    action = "set_config"

    def apply(self, ctx: ToolContext, args: SetConfigArgs) -> ToolResult:
        conn = ctx.connect()
        try:
            cur = conn.execute(
                "UPDATE service_config SET value = ? WHERE key = ?", (args.value, args.key)
            )
            conn.commit()
            if cur.rowcount == 0:
                return ToolResult.failure(f"不存在该配置项: {args.key}")
        finally:
            conn.close()
        return ToolResult.success(f"{args.key} 已设为 {args.value}")


class WriteReportArgs(BaseModel):
    filename: str = Field(description="工作区内的相对路径。")
    content: str


class WriteReport(WriteTool):
    name = "write_incident_report"
    description = (
        "L0。向工作区写入一份 markdown 报告，例如事故小结或复盘记录。"
    )
    args_model = WriteReportArgs
    action = "write_incident_report"

    def apply(self, ctx: ToolContext, args: WriteReportArgs) -> ToolResult:
        target = (ctx.workspace / args.filename).resolve()
        if not str(target).startswith(str(ctx.workspace.resolve())):
            return ToolResult.failure("路径越出了工作区")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(args.content, encoding="utf-8")
        return ToolResult.success(f"已写入 {len(args.content)} 字符到 {args.filename}")
