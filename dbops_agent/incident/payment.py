"""Public synthetic ledger contract; never reads evaluator labels or files.

A single provider namespace is assumed. Settled receipts retain local recording
metadata. Only order/customer/amount are the transaction payload; a retry may have
a different local key/time. No business table references payment ids in this model.
The smallest live id is the canonical representative of one provider transaction.
This does not authorize refunds or deduplicate distinct external charges.
"""

from collections import defaultdict


def verified_duplicates(conn) -> set[int]:
    """Fail closed for a whole transaction if any receipt/payload is inconsistent."""
    payments = {row[0]: tuple(row) for row in conn.execute("SELECT * FROM payments")}
    groups = defaultdict(list)
    for row in conn.execute("SELECT * FROM payment_receipts"):
        groups[row[1]].append(tuple(row))
    duplicates = set()
    for transaction, receipts in groups.items():
        if not transaction.strip():
            continue
        payloads = {row[2:5] for row in receipts}
        if len(payloads) != 1 or any(row[5] != "settled" for row in receipts):
            continue
        live = []
        consistent = True
        for receipt in receipts:
            payment = payments.get(receipt[0])
            if payment is None:
                continue  # Historical recording metadata survives previous cleanup.
            if payment[1:4] != receipt[2:5] or payment[4:6] != receipt[6:8]:
                consistent = False
                break
            live.append(payment[0])
        if consistent and len(live) > 1:
            duplicates.update(set(live) - {min(live)})
    return duplicates
