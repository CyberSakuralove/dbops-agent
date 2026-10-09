"""Aggregate public facts, then read complete candidate groups under a call budget."""

from math import ceil

from .access import ObservationLimit

SUMMARY = """SELECT r.provider_txn_id AS txn, count(*) AS receipts, count(p.id) AS live,
min(r.order_id) AS order_min,max(r.order_id) AS order_max,
min(r.customer_id) AS customer_min,max(r.customer_id) AS customer_max,
min(r.amount) AS amount_min,max(r.amount) AS amount_max,
sum(CASE WHEN r.state IS NOT 'settled' THEN 1 ELSE 0 END) AS bad_state,
sum(CASE WHEN p.id IS NOT NULL AND
(p.order_id IS NOT r.order_id OR p.customer_id IS NOT r.customer_id OR
p.amount IS NOT r.amount OR p.idempotency_key IS NOT r.recorded_key OR
p.created_at IS NOT r.recorded_at) THEN 1 ELSE 0 END) AS mismatch
FROM payment_receipts r LEFT JOIN payments p ON p.id=r.payment_id
{where} GROUP BY r.provider_txn_id HAVING live>1 OR bad_state>0 OR mismatch>0 OR
order_min IS NOT order_max OR customer_min IS NOT customer_max OR
amount_min IS NOT amount_max OR length(trim(r.provider_txn_id))=0
ORDER BY r.provider_txn_id LIMIT {limit}"""


def literal(text):
    return "'" + text.replace("'", "''") + "'"


def page(access, query, *, limit=10):
    while True:
        value = access.query(query(limit))
        if isinstance(value, list):
            return value
        if not isinstance(value, dict) or not value.get("truncated") or limit == 1:
            raise ObservationLimit("unreadable or clipped cell; no complete evidence")
        # Retry the same cursor with fewer rows. Never accept a clipped page as complete.
        limit = max(1, min(limit // 2, value.get("returned_rows", 0)))


def group_targets(access, summary):
    txn, cursor, receipts = summary["txn"], 0, []
    while True:
        rows = page(
            access,
            lambda n, cursor=cursor: (
                "SELECT r.*,p.id AS live_id,p.order_id AS live_order,"
                "p.customer_id AS live_customer,"
                "p.amount AS live_amount,p.idempotency_key AS live_key,p.created_at AS live_time "
                "FROM payment_receipts r LEFT JOIN payments p ON p.id=r.payment_id "
                f"WHERE r.provider_txn_id={literal(txn)} AND r.payment_id>{cursor} "
                f"ORDER BY r.payment_id LIMIT {n}"
            ),
        )
        if not rows:
            break
        ids = [r["payment_id"] for r in rows]
        if ids != sorted(set(ids)) or ids[0] <= cursor:
            raise ObservationLimit("non-monotone receipt page")
        receipts.extend(rows)
        cursor = ids[-1]
        if len(receipts) == summary["receipts"]:
            break
        if len(receipts) > summary["receipts"]:
            raise ObservationLimit("receipt group changed during investigation")
    if len(receipts) != summary["receipts"]:
        raise ObservationLimit("receipt count changed or pages incomplete")
    live = [r for r in receipts if r["live_id"] is not None]
    if len(live) != summary["live"]:
        raise ObservationLimit("ledger changed during investigation")
    payloads = {(r["order_id"], r["customer_id"], r["amount"]) for r in receipts}
    valid = (
        bool(txn.strip()) and len(payloads) == 1 and all(r["state"] == "settled" for r in receipts)
    )
    valid = valid and all(
        (r["live_order"], r["live_customer"], r["live_amount"], r["live_key"], r["live_time"])
        == (r["order_id"], r["customer_id"], r["amount"], r["recorded_key"], r["recorded_at"])
        for r in live
    )
    return (sorted(r["live_id"] for r in live)[1:] if valid else []), not valid


def run(access):
    targets, uncertain, complete_scan = [], False, False
    status = "completed"
    cursor = None
    access.read_reserve = 2
    try:
        missing = access.query(
            "SELECT count(*) AS missing FROM payments p LEFT JOIN payment_receipts r "
            "ON r.payment_id=p.id WHERE r.payment_id IS NULL"
        )
        if not isinstance(missing, list) or len(missing) != 1:
            raise ObservationLimit("missing-evidence count unavailable")
        uncertain = missing[0]["missing"] > 0
        while True:
            # Leave two calls for triage, and two per 60-id approved mutation.
            if access.remaining() < 3 + 2 * ceil(len(targets) / 60):
                raise ObservationLimit("preserve write and triage budget")
            where = "" if cursor is None else f"WHERE r.provider_txn_id>{literal(cursor)}"
            access.read_reserve = 2 + 2 * ceil(len(targets) / 60)
            groups = page(access, lambda n, where=where: SUMMARY.format(where=where, limit=n))
            if not groups:
                complete_scan = True
                break
            for group in groups:
                invalid = not group["txn"].strip() or group["bad_state"] or group["mismatch"]
                invalid = invalid or any(
                    group[f"{field}_min"] != group[f"{field}_max"]
                    for field in ("order", "customer", "amount")
                )
                if invalid:
                    # Public contradictions are enough to preserve/escalate this
                    # group. Only groups eligible for a mutation need full row evidence.
                    uncertain = True
                    cursor = group["txn"]
                    continue
                # A complete group may need multiple pages; if it exhausts the
                # observation allowance, already verified OTHER groups remain usable.
                reserve = 2 + 2 * ceil((len(targets) + max(0, group["live"] - 1)) / 60)
                if access.remaining() < reserve + ceil(group["receipts"] / 10):
                    raise ObservationLimit("candidate group exceeds remaining observation budget")
                access.read_reserve = reserve
                found, ambiguous = group_targets(access, group)
                targets.extend(found)
                uncertain = uncertain or ambiguous
                cursor = group["txn"]
    except ObservationLimit:
        status, uncertain = "partial_escalated_due_to_observation_limit", True
    applied = 0
    for offset in range(0, len(targets), 60):
        if not access.available(4):
            uncertain, status = True, "partial_escalated_due_to_call_limit"
            break
        result, _ = access.call(
            "deduplicate_payments",
            {
                "payment_ids": targets[offset : offset + 60],
                "idempotency_key": f"v2-dedup-{offset}",
            },
        )
        if not result.ok:
            uncertain, status = True, "action_refused_reinvestigation_required"
            break
        applied += len(targets[offset : offset + 60])
    classification = (
        "inconclusive"
        if uncertain or not complete_scan
        else ("true_incident" if applied else "false_alarm")
    )
    ack, _ = access.call(
        "acknowledge_alert",
        {
            "classification": classification,
            "rationale": "Complete public group evidence or escalation",
            "idempotency_key": "v2-payment-triage",
        },
    )
    completed = ack.ok
    if classification == "inconclusive":
        escalation, _ = access.call(
            "escalate_incident",
            {
                "reason": "Evidence incomplete/conflicting or bounded investigation stopped",
                "idempotency_key": "v2-payment-escalate",
            },
        )
        completed = completed and escalation.ok
    return {
        "status": status,
        "calls": access.calls,
        "classification": classification,
        "execution_complete": completed,
        "candidate_scan_complete": complete_scan,
        "verified_targets_applied": applied,
    }
