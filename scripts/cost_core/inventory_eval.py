"""Cloud-only, data-driven moving-weighted-average acceptance reference.

One book/item/warehouse/UOM/currency per stream; integer minor-unit book values.
No execution of user expressions, imports, callbacks or industry-specific code.
"""
from __future__ import annotations
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from enum import Enum
from fractions import Fraction
import json
import unicodedata
import re


class Invalid(ValueError):
    pass


class Kind(str, Enum):
    RECEIPT = "receipt"
    ISSUE = "sale_issue"
    RETURN = "sale_return"
    PRODUCTION_ISSUE = "production_issue"
    PRODUCTION_RETURN = "production_return"
    SHRINK = "shrink"
    TRANSFER_OUT = "transfer_out"
    TRANSFER_IN = "transfer_in"


@dataclass(frozen=True)
class Identity:
    book: str
    warehouse: str
    item: str
    uom: str
    currency: str


@dataclass(frozen=True)
class Transaction:
    id: str
    source_id: str
    at: datetime
    sequence: int
    kind: Kind
    quantity: Fraction
    evidence: str
    receipt_cents: int | None = None
    original_issue_id: str | None = None
    transfer_id: str | None = None
    production_object_id: str | None = None
    economic_key: str | None = None


IDENTITY = {"book", "warehouse", "item", "uom", "currency"}
POLICY = "line_half_up_minor_units_with_cumulative_original_issue_returns"
PRODUCTION_POLICY = "same_period_approved_batch_consumption_v1"
MAX_EVENTS = 500
MAX_HISTORY = 2000
MAX_BYTES = 1024 * 1024
MAX_STOCK_QUANTITY = Fraction("999999999999.999999")
MAX_STOCK_CENTS = 99999999999999999999


def check(ok, message):
    if not ok:
        raise Invalid(message)


def fields(obj, required, optional=frozenset()):
    check(isinstance(obj, dict), "Expected object")
    check(required <= obj.keys(), "Missing required fields: " + str(sorted(required - obj.keys())))
    check(obj.keys() <= required | optional, "Unknown fields: " + str(sorted(obj.keys() - required - optional)))


def text(value):
    check(isinstance(value, str) and 0 < len(value.strip()) <= 512, "Expected nonempty bounded text")
    return value


def instant(value):
    check(isinstance(value, str) and bool(re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ", value)),
          "Timestamp must be UTC YYYY-MM-DDTHH:MM:SSZ")
    try:
        return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except ValueError as exc:
        raise Invalid("Invalid timestamp") from exc


def timestamp(value):
    return value.strftime("%Y-%m-%dT%H:%M:%SZ")


def quantity(value, allow_zero=False):
    check(isinstance(value, str) and bool(re.fullmatch(r"[0-9]{1,12}(\.[0-9]{1,6})?", value)),
          "Quantity must be a nonnegative decimal string, at most six decimal places")
    result = Fraction(value)
    check(result >= 0 if allow_zero else result > 0, "Quantity must be positive")
    return result


def quantity_text(value):
    # Input quantities have at most six decimals and are only added/subtracted.
    scaled = value * 1000000
    check(scaled.denominator == 1, "Unsupported quantity precision")
    number = scaled.numerator
    return (str(number // 1000000) + "." + f"{number % 1000000:06}").rstrip("0").rstrip(".")


def money(value):
    check(isinstance(value, str) and bool(re.fullmatch(r"[0-9]{1,18}(\.[0-9]{1,2})?", value)),
          "Value must be a nonnegative decimal string in currency minor units")
    cents = Fraction(value) * 100
    check(cents.denominator == 1, "Unsupported money precision")
    return cents.numerator


def money_text(cents):
    return ("-" if cents < 0 else "") + str(abs(cents) // 100) + "." + f"{abs(cents) % 100:02}"


def round_half_up(value: Fraction) -> int:
    sign = -1 if value < 0 else 1
    whole, remainder = divmod(abs(value.numerator), value.denominator)
    return sign * (whole + int(2 * remainder >= value.denominator))


def ratio_text(value):
    return f"{value.numerator}/{value.denominator}"


def ratio(value):
    check(isinstance(value, str) and bool(re.fullmatch(r"[0-9]{1,100}/[1-9][0-9]{0,100}", value)),
          "Invalid bounded exact unit valuation")
    return Fraction(value)


def identity(obj):
    ident = Identity(**{key: text(obj[key]) for key in IDENTITY})
    check(bool(re.fullmatch(r"[A-Z]{3}", ident.currency)), "Invalid currency")
    return ident


def identity_fields(ident):
    return {key: getattr(ident, key) for key in sorted(IDENTITY)}


def run_document(raw):
    """Atomic JSON-compatible API: invalid input returns no postings or state."""
    try:
        return evaluate(raw)
    except Invalid as exc:
        return {"status": "invalid", "error": str(exc), "outputs": {}}


def evaluate(raw):
    driver = evaluate_stream(raw)
    try:
        row = next(driver)
        while True:
            driver.send(None)
            row = next(driver)
    except StopIteration as done:
        return done.value


def evaluate_stream(raw, allow_transfers=False):
    """Internal typed event/receipt state machine. Driver owns transfer cents.

    Advance to a Transaction, send None (or dispatched cents on arrival),
    receive completed audit movement, then advance to the next Transaction.
    """
    fields(raw, {"operation", "policy", "context", "opening", "transactions"}, {"production_policy"})
    check(raw["operation"] == "moving_weighted_average", "Unsupported operation")
    check(raw["policy"] == POLICY, "Unsupported valuation/rounding policy")
    production = "production_policy" in raw
    if production:
        check(raw["production_policy"] == PRODUCTION_POLICY, "Unsupported production inventory policy")
    context = raw["context"]
    fields(context, IDENTITY | {"period", "cutoff_at"})
    ident = identity(context)
    check(isinstance(context["period"], str) and bool(re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", context["period"])),
          "Invalid reporting period")
    start = instant(context["period"] + "-01T00:00:00Z")
    cutoff = instant(context["cutoff_at"])
    check(cutoff.strftime("%Y-%m") == context["period"], "Cutoff outside reporting period")
    opening = raw["opening"]
    fields(opening, IDENTITY | {"as_of", "quantity", "value", "status", "evidence",
                               "issues", "seen_event_ids", "seen_source_ids"})
    check(identity(opening) == ident, "Opening identity: cross-book/warehouse/item/UOM/currency")
    as_of = instant(opening["as_of"])
    check(start - timedelta(seconds=1) <= as_of < cutoff, "Stale/future opening snapshot")
    if allow_transfers:
        check(as_of == start - timedelta(seconds=1), "Transfers need all openings at prior month-end")
    check(opening["status"] in ("data", "evidenced_zero"), "Missing/unsupported opening coverage")
    text(opening["evidence"])
    stock_qty, stock_cents = quantity(opening["quantity"], True), money(opening["value"])
    check(stock_qty > 0 or stock_cents == 0, "Zero stock quantity with positive value")
    if opening["status"] == "evidenced_zero":
        check(stock_qty == 0 and stock_cents == 0, "Evidenced zero must be zero quantity and value")
    opening_qty, opening_cents = stock_qty, stock_cents
    for field in ("seen_event_ids", "seen_source_ids", "issues"):
        check(isinstance(opening[field], list) and len(opening[field]) <= MAX_HISTORY, "Invalid/oversized history")
    seen_events = [text(x) for x in opening["seen_event_ids"]]
    seen_sources = [text(x) for x in opening["seen_source_ids"]]
    check(len(set(seen_events)) == len(seen_events) and len(set(seen_sources)) == len(seen_sources),
          "Duplicate history identity")
    issues = {}
    historical_issue_sources = set()
    for item in opening["issues"]:
        fields(item, IDENTITY | {"id", "source_id", "at", "quantity", "issued_value", "returned_quantity",
                                 "unit_cost_before_round_exact", "evidence"})
        check(identity(item) == ident, "Original issue: cross-book/warehouse/item/UOM/currency")
        iid, sid = text(item["id"]), text(item["source_id"])
        check(iid in seen_events and sid in seen_sources, "Original issue missing history identity")
        check(iid not in issues, "Duplicate original issue")
        check(sid not in historical_issue_sources, "Duplicate original issue source")
        historical_issue_sources.add(sid)
        text(item["evidence"])
        check(instant(item["at"]) <= as_of, "Original issue is later than opening snapshot")
        issued_qty, returned_qty = quantity(item["quantity"]), quantity(item["returned_quantity"], True)
        issued_cents = money(item["issued_value"])
        check(returned_qty <= issued_qty, "Prior returns exceed original issue")
        original_unit = ratio(item["unit_cost_before_round_exact"])
        check(round_half_up(original_unit * issued_qty * 100) == issued_cents, "Inconsistent original issue valuation")
        issues[iid] = dict(item)
    check(isinstance(raw["transactions"], list) and len(raw["transactions"]) <= MAX_EVENTS, "Invalid/oversized transaction list")
    transactions = []
    economic_keys = set()
    last_order = (as_of, -1)
    for row in raw["transactions"]:
        special = {"receipt_value", "original_issue_id", "restock_status"}
        if allow_transfers:
            special.add("transfer_id")
        if production:
            special.update(("production_object_id", "economic_key"))
        fields(row, IDENTITY | {"id", "source_id", "at", "sequence", "kind", "quantity", "evidence"},
               special)
        check(identity(row) == ident, "Transaction: cross-book/warehouse/item/UOM/currency")
        try:
            kind = Kind(row["kind"])
        except (ValueError, TypeError) as exc:
            raise Invalid("Unsupported transaction kind") from exc
        if kind in (Kind.TRANSFER_OUT, Kind.TRANSFER_IN):
            check(allow_transfers, "Unsupported transaction kind")
        if kind in (Kind.PRODUCTION_ISSUE, Kind.PRODUCTION_RETURN):
            check(production, "Production movement requires declared production inventory policy")
        allowed = {Kind.RECEIPT: {"receipt_value"}, Kind.RETURN: {"original_issue_id", "restock_status"},
                   Kind.ISSUE: set(), Kind.SHRINK: set(),
                   Kind.PRODUCTION_ISSUE: {"production_object_id", "economic_key"},
                   Kind.PRODUCTION_RETURN: {"production_object_id", "economic_key", "original_issue_id", "restock_status"},
                   Kind.TRANSFER_OUT: {"transfer_id"}, Kind.TRANSFER_IN: {"transfer_id"}}[kind]
        check((row.keys() & special) == allowed,
              "Wrong/missing operation-specific fields")
        at = instant(row["at"])
        seq = row["sequence"]
        check(type(seq) is int and 0 <= seq <= 1000000, "Invalid sequence")
        check(as_of < at <= cutoff and at.strftime("%Y-%m") == context["period"], "Cross-period/future/stale transaction")
        check((at, seq) > last_order, "Unordered or ambiguous transaction chronology")
        last_order = (at, seq)
        tid, sid = text(row["id"]), text(row["source_id"])
        check(tid not in seen_events and sid not in seen_sources, "Duplicate/replayed event or source identity")
        seen_events.append(tid)
        seen_sources.append(sid)
        check(len(seen_events) <= MAX_HISTORY and len(seen_sources) <= MAX_HISTORY, "History capacity exceeded")
        amount = money(row["receipt_value"]) if kind == Kind.RECEIPT else None
        returns = kind in (Kind.RETURN, Kind.PRODUCTION_RETURN)
        linked = text(row["original_issue_id"]) if returns else None
        if returns:
            check(row["restock_status"] == "restockable", "Return restockability unknown/unsupported")
        batch = text(row["production_object_id"]) if kind in (Kind.PRODUCTION_ISSUE, Kind.PRODUCTION_RETURN) else None
        economic_key = text(row["economic_key"]) if batch is not None else None
        if economic_key is not None:
            canonical = unicodedata.normalize("NFKC", unicodedata.normalize("NFKC", economic_key).casefold()).strip()
            check(canonical and canonical not in economic_keys, "Duplicate or aliased production economic identity")
            economic_keys.add(canonical)
        transactions.append(Transaction(tid, sid, at, seq, kind, quantity(row["quantity"]),
                                        text(row["evidence"]), amount, linked, row.get("transfer_id"), batch, economic_key))
    postings, movements = [], []
    receipt_cents = net_cogs = shrink_cents = production_cents = 0
    receipt_qty = net_issue_qty = shrink_qty = production_qty = Fraction(0)
    production_issues = {}
    transit_in_qty = transit_out_qty = Fraction(0)
    transit_in_cents = transit_out_cents = 0
    for tx in transactions:
        dispatch_cents = yield tx
        if tx.kind != Kind.TRANSFER_IN:
            check(dispatch_cents is None, "Unexpected internal transfer dispatch value")
        before_qty, before_cents = stock_qty, stock_cents
        unit_before = Fraction(stock_cents, 100) / stock_qty if stock_qty else None
        detail = {"event_id": tx.id, "source_id": tx.source_id, "at": timestamp(tx.at),
                  "kind": tx.kind.value, "evidence": tx.evidence,
                  "quantity_before": quantity_text(before_qty), "value_before": money_text(before_cents),
                  "unit_cost_before_exact": None if unit_before is None else ratio_text(unit_before)}
        if tx.kind == Kind.RECEIPT:
            change_qty, change_cents = tx.quantity, tx.receipt_cents
            receipt_qty += change_qty
            receipt_cents += change_cents
            detail["treatment"] = "stock_receipt_not_cost_expense"
        elif tx.kind == Kind.TRANSFER_IN:
            check(type(dispatch_cents) is int and dispatch_cents >= 0, "Missing dispatched transfer cost")
            change_qty, change_cents = tx.quantity, dispatch_cents
            transit_in_qty += change_qty
            transit_in_cents += change_cents
            detail.update(transfer_id=tx.transfer_id, treatment="transfer_receipt_asset_only")
        elif tx.kind == Kind.TRANSFER_OUT:
            check(tx.quantity <= stock_qty, "Insufficient stock for transfer")
            exact = Fraction(stock_cents) * tx.quantity / stock_qty
            out_cents = round_half_up(exact)
            change_qty, change_cents = -tx.quantity, -out_cents
            transit_out_qty += tx.quantity
            transit_out_cents += out_cents
            detail.update(transfer_id=tx.transfer_id, treatment="transfer_dispatch_asset_only",
                          ideal_issue_cents_exact=ratio_text(exact),
                          line_rounding_cents_exact=ratio_text(Fraction(out_cents) - exact))
        elif tx.kind in (Kind.ISSUE, Kind.SHRINK, Kind.PRODUCTION_ISSUE):
            check(tx.quantity <= stock_qty, "Insufficient stock / negative inventory")
            ideal_cents = Fraction(stock_cents) * tx.quantity / stock_qty
            out_cents = round_half_up(ideal_cents)
            change_qty, change_cents = -tx.quantity, -out_cents
            category = "cogs" if tx.kind == Kind.ISSUE else "production_consumption" if tx.kind == Kind.PRODUCTION_ISSUE else "shrink"
            if tx.kind in (Kind.ISSUE, Kind.PRODUCTION_ISSUE):
                original = {**identity_fields(ident), "id": tx.id, "source_id": tx.source_id,
                            "at": timestamp(tx.at), "quantity": quantity_text(tx.quantity),
                            "issued_value": money_text(out_cents), "returned_quantity": "0",
                            "unit_cost_before_round_exact": ratio_text(unit_before), "evidence": tx.evidence}
                if tx.kind == Kind.ISSUE:
                    issues[tx.id] = original
                else:
                    production_issues[tx.id] = {**original, "production_object_id": tx.production_object_id}
            if tx.kind == Kind.ISSUE:
                net_cogs += out_cents
                net_issue_qty += tx.quantity
            elif tx.kind == Kind.PRODUCTION_ISSUE:
                production_cents += out_cents
                production_qty += tx.quantity
            else:
                shrink_cents += out_cents
                shrink_qty += tx.quantity
            posting = {"event_id": tx.id, "source_id": tx.source_id, "category": category,
                       "amount": money_text(out_cents), "recognition_period": context["period"]}
            detail.update(treatment="stock_issue_to_production" if tx.kind == Kind.PRODUCTION_ISSUE else "cost_posting_and_stock_issue", ideal_issue_cents_exact=ratio_text(ideal_cents),
                          line_rounding_cents_exact=ratio_text(Fraction(out_cents) - ideal_cents))
        else:
            production_return = tx.kind == Kind.PRODUCTION_RETURN
            original_issues = production_issues if production_return else issues
            check(tx.original_issue_id in original_issues,
                  "Undefined/forward/scope-mismatched production issue reference" if production_return
                  else "Undefined/forward/non-sale original issue reference")
            original = original_issues[tx.original_issue_id]
            if production_return:
                check(original["production_object_id"] == tx.production_object_id,
                      "Production return batch differs from original issue")
            check(instant(original["at"]) <= tx.at, "Return predates original issue")
            original_qty = quantity(original["quantity"])
            prior_returned = quantity(original["returned_quantity"], True)
            cumulative = prior_returned + tx.quantity
            check(cumulative <= original_qty, "Return exceeds original issued quantity")
            original_cents = money(original["issued_value"])
            # Cumulative proportional credit ensures full returns exactly reverse the original posted cents.
            restored_cents = (round_half_up(Fraction(original_cents) * cumulative / original_qty)
                              - round_half_up(Fraction(original_cents) * prior_returned / original_qty))
            original["returned_quantity"] = quantity_text(cumulative)
            change_qty, change_cents = tx.quantity, restored_cents
            if production_return:
                production_cents -= restored_cents
                production_qty -= tx.quantity
            else:
                net_cogs -= restored_cents
                net_issue_qty -= tx.quantity
            posting = {"event_id": tx.id, "source_id": tx.source_id,
                       "category": "production_consumption" if production_return else "cogs",
                       "amount": money_text(-restored_cents), "recognition_period": context["period"],
                       "original_issue_id": tx.original_issue_id, "original_source_id": original["source_id"],
                       "original_issue_period": original["at"][:7]}
            detail.update(treatment="linked_production_consumption_reversal_and_restock" if production_return
                          else "linked_original_cost_reversal_and_restock",
                          original_issue_id=tx.original_issue_id,
                          original_issued_value=original["issued_value"],
                          original_source_id=original["source_id"], original_evidence=original["evidence"],
                          original_unit_before_round_exact=original["unit_cost_before_round_exact"],
                          original_effective_unit_cost_exact=ratio_text(Fraction(original_cents, 100) / original_qty),
                          cumulative_returned_quantity=quantity_text(cumulative))
        if tx.kind in (Kind.ISSUE, Kind.SHRINK, Kind.RETURN, Kind.PRODUCTION_ISSUE, Kind.PRODUCTION_RETURN):
            if tx.production_object_id is not None:
                production_fields = {"production_object_id": tx.production_object_id, "economic_key": tx.economic_key}
                posting.update(production_fields)
                detail.update(production_fields)
            postings.append(posting)
        stock_qty += change_qty
        stock_cents += change_cents
        check(stock_qty >= 0 and stock_cents >= 0 and (stock_qty > 0 or stock_cents == 0), "Invalid resulting stock balance")
        check(stock_qty <= MAX_STOCK_QUANTITY and stock_cents <= MAX_STOCK_CENTS,
              "Stock balance capacity exceeded")
        detail.update(quantity_change=("-" if change_qty < 0 else "") + quantity_text(abs(change_qty)),
                      value_change=money_text(change_cents), quantity_after=quantity_text(stock_qty),
                      value_after=money_text(stock_cents))
        movements.append(detail)
        yield detail
    check(opening_cents + receipt_cents + transit_in_cents ==
          net_cogs + production_cents + shrink_cents + stock_cents + transit_out_cents, "Value conservation failure")
    check(opening_qty + receipt_qty + transit_in_qty ==
          net_issue_qty + production_qty + shrink_qty + stock_qty + transit_out_qty, "Quantity conservation failure")
    next_state = {**identity_fields(ident), "as_of": timestamp(cutoff), "quantity": quantity_text(stock_qty),
                  "value": money_text(stock_cents), "status": "data", "evidence": "Derived reference closing snapshot",
                  "issues": list(issues.values()), "seen_event_ids": seen_events, "seen_source_ids": seen_sources}
    return {"status": "valid", "can_claim_complete": False, "scope": "offline_reference_not_deployment",
            "operation": raw["operation"], "policy": POLICY, "context": dict(context),
            "period_scope": "period_start_to_cutoff" if as_of == start - timedelta(seconds=1) else "incremental_to_cutoff",
            "outputs": {"available_value": money_text(opening_cents + receipt_cents),
                        "net_cogs": money_text(net_cogs), "shrink": money_text(shrink_cents),
                        "closing_value": money_text(stock_cents), "closing_quantity": quantity_text(stock_qty),
                        "reconciliation_difference": "0.00",
                        **({"net_production_consumption": money_text(production_cents),
                            "net_production_quantity": quantity_text(production_qty)} if production else {}),
                        **({"transfer_in":money_text(transit_in_cents),"transfer_out":money_text(transit_out_cents)}
                           if allow_transfers else {})},
            "cost_postings": postings, "stock_movements": movements,
            "stock_balance": {"quantity": quantity_text(stock_qty), "value": money_text(stock_cents),
                              "treatment": "asset_balance_not_cost_expense"},
            "next_state": next_state}


if __name__ == "__main__":
    import sys
    with open(sys.argv[1], "rb") as handle:
        content = handle.read(MAX_BYTES + 1)
    if len(content) > MAX_BYTES:
        result = {"status": "invalid", "error": "Input size limit exceeded", "outputs": {}}
    else:
        try:
            result = run_document(json.loads(content))
        except (ValueError, UnicodeError, RecursionError):
            result = {"status": "invalid", "error": "Malformed JSON", "outputs": {}}
    print(json.dumps(result, indent=2))
    sys.exit(0 if result["status"] == "valid" else 1)
