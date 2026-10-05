"""Trusted local executor. Approval decisions are deliberately absent from Agent tools.

Only effects in business.db share the operation/approval transaction. This is not a
distributed exactly-once protocol and does not cover reports or remote APIs.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import time
import uuid
from contextlib import closing
from dataclasses import asdict
from pathlib import Path

from .policy import Tier, Verdict

PUBLIC_TABLES = frozenset(
    {
        "customers",
        "orders",
        "payments",
        "products",
        "search_index",
        "sync_state",
        "service_config",
        "db_sessions",
        "alert_acknowledgements",
    }
)

SCHEMA = (
    "CREATE TABLE IF NOT EXISTS operations (incident TEXT, key TEXT, fingerprint TEXT NOT NULL, "
    "action TEXT NOT NULL, request_id TEXT, result TEXT NOT NULL, PRIMARY KEY(incident,key))",
    "CREATE TABLE IF NOT EXISTS approvals (request_id TEXT PRIMARY KEY, incident TEXT NOT NULL, "
    "key TEXT NOT NULL, fingerprint TEXT NOT NULL, action TEXT NOT NULL, arguments TEXT NOT NULL, "
    "resource_version TEXT NOT NULL, expires REAL NOT NULL, status TEXT NOT NULL, "
    "decided_by TEXT, decision_reason TEXT)",
)


def initialize(conn: sqlite3.Connection) -> None:
    audit_columns = {row[1] for row in conn.execute("PRAGMA table_info(repair_log)")}
    if "incident" not in audit_columns:
        conn.execute("ALTER TABLE repair_log ADD COLUMN incident TEXT")
    for statement in SCHEMA:
        conn.execute(statement)
    conn.execute(
        "CREATE TABLE IF NOT EXISTS resource_versions "
        "(resource TEXT PRIMARY KEY, version INTEGER NOT NULL)"
    )
    # Persistent epochs prevent ABA: changing a resource and restoring its old value
    # still invalidates approval. Only the affected resource's epoch is included.
    for table in ("payments", "db_sessions", "service_config"):
        for kind in ("INSERT", "UPDATE", "DELETE"):
            aliases = (
                ["NEW"] if kind == "INSERT" else ["OLD"] if kind == "DELETE" else ["OLD", "NEW"]
            )
            resources = []
            for alias in aliases:
                if table == "payments":
                    resources.append("'payments:*'")
                elif table == "service_config":
                    resources.append(f"'config:'||{alias}.key")
                else:
                    resources.extend([f"'session:'||{alias}.id", f"'session:'||{alias}.blocked_by"])
            body = " ".join(
                "INSERT INTO resource_versions(resource,version) "
                f"SELECT {resource},1 WHERE {resource} IS NOT NULL "
                "ON CONFLICT(resource) DO UPDATE SET version=version+1;"
                for resource in set(resources)
            )
            conn.execute(
                f"CREATE TRIGGER IF NOT EXISTS guard_version_{table}_{kind.lower()} "
                f"AFTER {kind} ON {table} BEGIN {body} END"
            )


def canonical(arguments: dict) -> str:
    return json.dumps(arguments, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def payload(raw: dict) -> dict:
    return {
        k: v for k, v in raw.items() if k not in {"request_id", "idempotency_key", "confirm_token"}
    }


def fingerprint(action: str, raw: dict) -> str:
    return hashlib.sha256(canonical([action, payload(raw)]).encode()).hexdigest()


def resource_version(conn: sqlite3.Connection, action: str, raw: dict) -> str:
    # Bind only relevant resources. Audit records and unrelated config writes do not
    # invalidate a pending approval. Snapshot hashes also work across process restarts.
    if action == "deduplicate_payments":
        resource = "payments:*"
        rows = conn.execute("SELECT * FROM payments ORDER BY id").fetchall()
    elif action == "terminate_session":
        resource = f"session:{raw['session_id']}"
        rows = conn.execute(
            "SELECT * FROM db_sessions WHERE id=? OR blocked_by=? ORDER BY id",
            (raw["session_id"], raw["session_id"]),
        ).fetchall()
    elif action == "set_config":
        resource = f"config:{raw['key']}"
        rows = conn.execute("SELECT * FROM service_config WHERE key=?", (raw["key"],)).fetchall()
    else:
        resource = "none"
        rows = []
    epoch = conn.execute(
        "SELECT version FROM resource_versions WHERE resource=?", (resource,)
    ).fetchone()
    return hashlib.sha256(
        canonical([epoch[0] if epoch else 0, [list(row) for row in rows]]).encode()
    ).hexdigest()


def audit(conn: sqlite3.Connection, ctx, action: str, raw: dict, outcome: str) -> None:
    conn.execute(
        "INSERT INTO repair_log (action,tier,target,idempotency_key,outcome,ts,incident) "
        "VALUES (?,?,?,?,?,datetime('now'),?)",
        (
            action,
            ctx.policy.tier_of(action).value,
            canonical(payload(raw)),
            raw.get("idempotency_key"),
            outcome,
            ctx.alert_id,
        ),
    )


class ApprovalService:
    """Host/operator interface, never exported by ToolRegistry.

    An operator with local DB access is trusted. A script may simulate this role for
    experiments but must label itself simulated; this class alone is not OS isolation.
    """

    def __init__(self, db: Path):
        self.db = db

    def pending(self) -> list[dict]:
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.row_factory = sqlite3.Row
            initialize(conn)
            return [
                dict(r)
                for r in conn.execute(
                    "SELECT * FROM approvals WHERE status='pending' ORDER BY rowid"
                )
            ]

    def decide(self, request_id: str, *, approve: bool, actor: str, reason: str) -> None:
        if not actor.strip() or not reason.strip():
            raise ValueError("审批必须记录操作者和理由")
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute("BEGIN IMMEDIATE")
            initialize(conn)
            row = conn.execute(
                "SELECT status,expires FROM approvals WHERE request_id=?", (request_id,)
            ).fetchone()
            if row is None or row[0] != "pending":
                raise ValueError("没有可决策的 pending 请求")
            if row[1] <= time.time():
                raise ValueError("审批申请已过期；需要新申请")
            conn.execute(
                "UPDATE approvals SET status=?,decided_by=?,decision_reason=? WHERE request_id=?",
                ("approved" if approve else "denied", actor, reason, request_id),
            )


def execute(tool, ctx, args):
    from ..tools.base import ToolResult

    action = tool.action
    raw = args.model_dump()
    key = raw.get("idempotency_key")
    if not isinstance(key, str) or not key.strip():
        return ToolResult.failure("数据库写操作必须提供非空 idempotency_key", "refused")
    fp = fingerprint(action, raw)
    conn = ctx.connect()
    try:
        conn.execute("BEGIN IMMEDIATE")  # serialize concurrent retries before lookup
        initialize(conn)
        ctx.checkpoint("before_effect")
        previous = conn.execute(
            "SELECT fingerprint,result FROM operations WHERE incident=? AND key=?",
            (ctx.alert_id, key),
        ).fetchone()
        if previous:
            if previous["fingerprint"] != fp:
                audit(conn, ctx, action, raw, "key_conflict")
                conn.commit()
                return ToolResult.failure("幂等键已用于不同动作或参数", "key_conflict")
            # Lookup comes BEFORE consumed-approval validation: same completed logical
            # operation returns the exact stored result, even after a lost response.
            return ToolResult(**json.loads(previous["result"]))

        verdict, reason = ctx.policy.evaluate(action, raw)
        if verdict in {Verdict.REFUSED, Verdict.BAD_TOKEN}:
            audit(conn, ctx, action, raw, verdict.value)
            conn.commit()
            return ToolResult.failure(reason, verdict.value)
        request_id = raw.get("request_id")
        if ctx.policy.tier_of(action) is Tier.L1_CONFIRM:
            version = resource_version(conn, action, raw)
            denied = conn.execute(
                "SELECT count(*) FROM approvals WHERE incident=? AND fingerprint=? "
                "AND resource_version=? AND status='denied'",
                (ctx.alert_id, fp, version),
            ).fetchone()[0]
            if denied:
                # A new key/request cannot renew the same denied mutation of the
                # same resource. A changed resource or new parameters need review.
                audit(conn, ctx, action, raw, "approval_blocked")
                conn.commit()
                return ToolResult.failure(
                    "相同参数与资源版本的变更已被独立操作者拒绝", "approval_blocked"
                )
            if not request_id:
                request = conn.execute(
                    "SELECT * FROM approvals WHERE incident=? AND key=? AND fingerprint=? "
                    "AND resource_version=? AND expires>? AND status IN ('pending','approved') "
                    "ORDER BY rowid DESC LIMIT 1",
                    (ctx.alert_id, key, fp, version, time.time()),
                ).fetchone()
                if request is None:
                    request_id = uuid.uuid4().hex
                    conn.execute(
                        "INSERT INTO approvals "
                        "(request_id,incident,key,fingerprint,action,arguments,resource_version,"
                        "expires,status) VALUES (?,?,?,?,?,?,?,?, 'pending')",
                        (
                            request_id,
                            ctx.alert_id,
                            key,
                            fp,
                            action,
                            canonical(payload(raw)),
                            version,
                            time.time() + ctx.approval_ttl_seconds,
                        ),
                    )
                    status = "pending"
                else:
                    request_id, status = request["request_id"], request["status"]
                audit(conn, ctx, action, raw, "needs_confirmation")
                conn.commit()
                return ToolResult.failure(
                    canonical(
                        {
                            "request_id": request_id,
                            "status": status,
                            "message": "由独立操作者批准后，携带 request_id 和原幂等键重试",
                        }
                    ),
                    "needs_confirmation",
                )
            approval = conn.execute(
                "SELECT * FROM approvals WHERE request_id=?", (request_id,)
            ).fetchone()
            valid = (
                approval is not None
                and approval["incident"] == ctx.alert_id
                and approval["key"] == key
                and approval["fingerprint"] == fp
                and approval["resource_version"] == version
                and approval["expires"] > time.time()
                and approval["status"] == "approved"
            )
            if not valid:
                audit(conn, ctx, action, raw, "approval_blocked")
                conn.commit()
                return ToolResult.failure(
                    "审批未批准、过期、已消费或与请求/资源状态不匹配", "approval_blocked"
                )

        result = tool.apply(ctx, args, conn)
        if not result.ok:
            conn.rollback()  # no key reservation or approval consumption on failure
            conn.execute("BEGIN IMMEDIATE")
            audit(conn, ctx, action, raw, "failed")
            conn.commit()
            return result
        ctx.checkpoint("after_effect")
        result.verdict = "applied"
        conn.execute(
            "INSERT INTO operations VALUES (?,?,?,?,?,?)",
            (ctx.alert_id, key, fp, action, request_id, canonical(asdict(result))),
        )
        if ctx.policy.tier_of(action) is Tier.L1_CONFIRM:
            conn.execute("UPDATE approvals SET status='consumed' WHERE request_id=?", (request_id,))
        audit(conn, ctx, action, raw, "applied")
        conn.commit()
        ctx.checkpoint("after_commit")
        return result
    finally:
        # Returning/rethrowing before commit always rolls back.
        conn.close()
