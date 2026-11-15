"""Trace upstream-booked foreign invoices; do not compute or post any FX."""
from datetime import date
from decimal import Decimal
import re
import unicodedata

MODE = "already_booked_v1"
CONFIG = "foreign_booked_trace"
ALLOWED_SCHEMA = {"raw_recognition",
                  "dataset_id","plugin_id","period","currency","scenario","industry","label",
                  "allocation_rules"}
FIELDS = frozenset(("fx_original_id", "fx_original_date", "fx_original_currency",
                    "fx_original_amount", "fx_booked_amount", "fx_booking_evidence"))
_NUM = re.compile(r"-?(0|[1-9][0-9]{0,13})\.[0-9]{2}")
_BASE = re.compile(r"-?(0|[1-9][0-9]{0,13})(?:\.[0-9]{1,2})?")
_DATE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")


def source_key(text):
    return unicodedata.normalize("NFKC", text).casefold()


def _valid_text(s):
    return (0 < len(s) <= 160 and s == s.strip() and
            not any(unicodedata.category(c).startswith("C") for c in s))


def decode(rows, scope, need):
    """Return copied normalized trace records keyed by the already-booked source ID."""
    need(scope.get("currency") == "CNY", "foreign booked trace requires a CNY ledger")
    base_sources = {source_key(row["id"]) for row in rows}
    need(len(base_sources) == len(rows), "aliased base source IDs in foreign booked scope")
    # Existing evaluator owns exact-key accounting semantics (e.g., a memo
    # record may reuse a book-economic ID). Reject only distinct aliases here.
    economic = {}
    for row in rows:
        value = row["economic_key"]
        need(_valid_text(value), "invalid economic identity in foreign booked scope")
        key = source_key(value)
        need(economic.setdefault(key,value) == value, "aliased economic identity in foreign booked scope")
    used = set()
    trace = {}
    for row in rows:
        values = {k:row.get(k, "") for k in FIELDS}
        if not any(values.values()):
            continue
        need(all(values.values()), "incomplete foreign booked trace")
        need(row["book"] == scope.get("book") and row["period"] == scope.get("period") and
             row["currency"] == "CNY" and row["kind"] in {"expense", "credit"} and row["status"] == "data" and
             not row.get("origin_period") and not row.get("bridge_group"),
             "foreign booked trace supports only same-book, same-period CNY expense or credit")
        original_id, ev = values["fx_original_id"], values["fx_booking_evidence"]
        need(_valid_text(original_id) and _valid_text(ev), "invalid foreign source or booking evidence identifier")
        key = source_key(original_id)
        need(key not in base_sources and key not in economic and key not in used, "aliased or duplicate original foreign source")
        used.add(key)
        stamp = values["fx_original_date"]
        try:
            valid = bool(_DATE.fullmatch(stamp)) and date.fromisoformat(stamp).isoformat() == stamp
        except ValueError:
            valid = False
        need(valid and stamp[:7] == row["period"], "foreign original date must be within the booked month")
        cur, amount, booked = (values[k] for k in ("fx_original_currency","fx_original_amount","fx_booked_amount"))
        need(cur in {"USD", "EUR"}, "foreign booked trace only accepts USD or EUR originals")
        base = row["amount"]
        need(_NUM.fullmatch(amount) and _NUM.fullmatch(booked) and _BASE.fullmatch(base),
             "invalid foreign or booked amount precision")
        orig, booking = Decimal(amount), Decimal(booked)
        need(booking == Decimal(base), "foreign trace contradicts upstream CNY booked amount")
        need((orig < 0 and booking < 0) if row["kind"] == "credit" else (orig > 0 and booking > 0),
             "foreign original and booked amount sign mismatch")
        trace[row["id"]] = {"source_id":row["id"], "origin_source_id":original_id, "origin_date":stamp,
                            "origin_currency":cur, "origin_amount":amount, "booked_currency":"CNY",
                            "booked_amount":booked, "booking_evidence_id":ev, "kind":row["kind"]}
    need(bool(trace), "foreign booked trace enabled without original records")
    return trace
