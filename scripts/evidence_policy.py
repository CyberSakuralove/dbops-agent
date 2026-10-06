"""Independent rule baseline. All decisions use public tool observations.

No import of the executor's duplicate predicate, fixture spec or protected snapshot.
"""

import json
from collections import defaultdict


def observe(player, table):
    result = player.call("query_business_db", {"sql": f"SELECT * FROM {table}"})
    if not result.ok:
        raise RuntimeError(result.content)
    data = json.loads(result.content)
    if isinstance(data, dict):
        raise RuntimeError("Incomplete observation; narrow the query before deciding")
    return data


def payment_decision(player):
    payments = observe(player, "payments")
    receipts = observe(player, "payment_receipts")
    by_id = {r["payment_id"]: r for r in receipts}
    groups = defaultdict(list)
    for row in payments:
        receipt = by_id.get(row["id"])
        if receipt is not None:
            groups[receipt["provider_txn_id"]].append(row)
    targets = []
    uncertain = any(p["id"] not in by_id for p in payments)
    for transaction, rows in groups.items():
        history = [r for r in receipts if r["provider_txn_id"] == transaction]
        payloads = {(r["order_id"], r["customer_id"], r["amount"]) for r in history}
        valid = bool(transaction.strip()) and len(payloads) == 1
        valid = valid and all(r["state"] == "settled" for r in history)
        for payment in rows:
            receipt = by_id[payment["id"]]
            valid = valid and all(
                payment[key] == receipt[key] for key in ("order_id", "customer_id", "amount")
            )
            valid = valid and payment["idempotency_key"] == receipt["recorded_key"]
            valid = valid and payment["created_at"] == receipt["recorded_at"]
        if not valid:
            uncertain = True
        elif len(rows) > 1:
            targets.extend(sorted(p["id"] for p in rows)[1:])
    if targets:
        result = player.call(
            "deduplicate_payments", {"payment_ids": sorted(targets), "idempotency_key": "dedup"}
        )
        if not result.ok:
            raise RuntimeError(result.content)
    if uncertain:
        return "inconclusive"
    return "true_incident" if targets else "false_alarm"


def triage(player, classification):
    player.call(
        "acknowledge_alert",
        {
            "classification": classification,
            "rationale": "Public ledger, receipt and service checks.",
            "idempotency_key": "ack",
        },
    )
    if classification == "inconclusive":
        player.call(
            "escalate_incident",
            {
                "reason": "Provider evidence missing or conflicting; preserve ledger for review.",
                "idempotency_key": "escalate",
            },
        )
