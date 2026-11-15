"""Schedule declared closed internal transfer pairs; never creates cost postings."""
from heapq import heappop, heappush
import re

from .inventory_eval import (
    MAX_HISTORY, Kind, check, evaluate_stream, instant, money, money_text, quantity, quantity_text,
)

TRANSFER_POLICY = "closed_same_period_pairs_v1"
KINDS = {Kind.TRANSFER_IN.value, Kind.TRANSFER_OUT.value}


def transfer_key(v):
    check(type(v) is str and re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", v) is not None, "Invalid transfer ID")
    return v.casefold()


def check_history(history):
    check(type(history) is list and len(history) <= MAX_HISTORY, "Invalid transfer history")
    values = {transfer_key(v) for v in history}
    check(len(values) == len(history), "Duplicate transfer history")
    return values


def run_grouped(grouped, history):
    claimed = check_history(history)
    orders = set()
    pairs = {}
    for _, _, doc in grouped:
        for row in doc["transactions"]:
            clock = (instant(row["at"]), row["sequence"])
            check(clock not in orders, "Ambiguous combined inventory chronology")
            orders.add(clock)
            kind = row["kind"]
            if kind not in KINDS:
                check("transfer_id" not in row, "Transfer ID on unrelated movement")
                continue
            transfer_id = row.get("transfer_id")
            key = transfer_key(transfer_id)
            check(key not in claimed, "Replayed transfer ID")
            pair = pairs.setdefault(key, {})
            check(kind not in pair, "Duplicate or partial transfer legs are unsupported")
            pair[kind] = (row, clock)
    check(len(pairs) + len(history) <= MAX_HISTORY, "Transfer history capacity exceeded")
    for pair in pairs.values():
        check(set(pair) == KINDS, "Both transfer legs must close inside the declared period and cutoff")
        a, t0 = pair[Kind.TRANSFER_OUT.value]
        b, t1 = pair[Kind.TRANSFER_IN.value]
        check(a["transfer_id"] == b["transfer_id"], "Conflicting transfer ID spellings")
        check(a["warehouse"] != b["warehouse"] and
              all(a[k] == b[k] for k in ("book", "item", "uom", "currency")), "Transfer warehouse/item/unit scope mismatch")
        check(quantity(a["quantity"]) == quantity(b["quantity"]), "Transfer quantity mismatch")
        check(t0 < t1, "Transfer receipt precedes dispatch")

    queue = []
    states = {}
    results = {}
    transit = {}
    proofs = []
    def advance(j, stream):
        try:
            tx = next(stream)
        except StopIteration as done:
            results[j] = done.value
            return
        heappush(queue, (tx.at, tx.sequence, j, tx))

    for j, _, doc in grouped:
        states[j] = evaluate_stream(doc, allow_transfers=True)
        advance(j, states[j])
    while queue:
        _, _, j, tx = heappop(queue)
        handle = states[j]
        incoming = None
        if tx.kind == Kind.TRANSFER_IN:
            key = transfer_key(tx.transfer_id)
            check(key in transit, "Transfer receipt has no in-transit dispatch")
            incoming = transit.pop(key)
        move = handle.send(incoming)
        if tx.kind == Kind.TRANSFER_OUT:
            transit[transfer_key(tx.transfer_id)] = money(move["value_change"].lstrip("-"))
        elif tx.kind == Kind.TRANSFER_IN:
            a, _ = pairs[key][Kind.TRANSFER_OUT.value]
            b, _ = pairs[key][Kind.TRANSFER_IN.value]
            proofs.append(dict(transfer_id=tx.transfer_id,item=b["item"],uom=b["uom"],
                               from_warehouse=a["warehouse"],to_warehouse=b["warehouse"],
                               dispatch_at=a["at"],receipt_at=b["at"],quantity=quantity_text(tx.quantity),
                               dispatch_source_id=a["source_id"],receipt_source_id=b["source_id"],
                               amount=money_text(incoming),treatment="asset_transfer_not_operating_expense"))
        advance(j, handle)
    check(not transit, "Unclosed transfer in transit")
    check(len(proofs) == len(pairs), "Incomplete transfer proof")
    return results, proofs, list(history) + [x["transfer_id"] for x in proofs]
