"""Versioned, source-bound auxiliary financial ledgers.

These ledgers never post additional engine costs. Asset recognition and declared
supplier costs are reconciled to the already calculated object/element ledger.
"""
import csv
import hashlib
import io
import re
import unicodedata
from decimal import Decimal as D, ROUND_HALF_UP, localcontext
from urllib.parse import urlencode

SCOPE = {"book", "period", "currency"}
MONTH = re.compile(r"[0-9]{4}-(?:0[1-9]|1[0-2])")
MONEY = re.compile(r"[0-9]{1,18}(?:\.[0-9]{1,2})?")
QUANTITY = re.compile(r"[0-9]{1,18}(?:\.[0-9]{1,6})?")
ZERO = D(0)


def need(ok, message):
    if not ok:
        raise ValueError("financial_ledgers: " + message)


def canonical(value):
    return unicodedata.normalize("NFKC", str(value)).casefold().strip()


def numeric(value, where, money=False):
    need(isinstance(value, str) and (MONEY if money else QUANTITY).fullmatch(value) is not None,
         where + ": required nonnegative " + ("two-decimal money" if money else "quantity"))
    return D(value)


def fmt(value):
    return str(value.quantize(D(".01"), rounding=ROUND_HALF_UP))


def _source(files, schema, opt, fields, scope, start, source_uris, seen, *, opening=False):
    need(isinstance(opt, dict) and isinstance(opt.get("file"), str), "file declaration required")
    name = opt["file"]
    need(name in files and isinstance(files[name], str), name + ": missing source")
    cols = opt.get("columns")
    need(isinstance(cols, dict) and set(cols) == fields and all(isinstance(v,str) and v.strip() for v in cols.values())
         and len(set(cols.values())) == len(cols),
         name + ": invalid column mapping")
    digest = hashlib.sha256(files[name].encode("utf-8")).hexdigest()
    need(schema.get("raw_composite", {}).get("source_hashes", {}).get(name) == digest,
         name + ": required source hash missing or mismatch")
    reader = csv.DictReader(io.StringIO(files[name].lstrip("\ufeff")), strict=True)
    headers = reader.fieldnames or []
    need(set(headers) == set(cols.values()) | SCOPE and len(headers) == len(set(headers)),
         name + ": unknown/missing/duplicate headers")
    try:
        raw_rows = list(reader)
    except csv.Error as exc:
        raise ValueError("financial_ledgers: " + name + ": invalid CSV: " + str(exc)) from None
    records = []
    for line, raw in enumerate(raw_rows, 2):
        where = name + ":row " + str(line)
        need(None not in raw and all(v is not None for v in raw.values()), where + ": ragged row")
        period = raw["period"]
        need(MONTH.fullmatch(period) is not None and start <= period <= scope["period"],
             where + ": period outside declared history")
        need(not opening or period == start, where + ": opening control must match history_start")
        need(raw["book"] == scope["book"] and raw["currency"] == scope["currency"],
             where + ": book/currency mismatch")
        row = {key: raw[column].strip() for key, column in cols.items()}
        need(row["id"] and len(row["id"]) <= 160 and canonical(row["id"]) not in seen,
             where + ": duplicate or aliased source id")
        seen.add(canonical(row["id"]))
        need(row["status"] in {"data", "explicit_zero", "computed", "missing", "not_applicable"},
             where + ": unsupported evidence status")
        need(bool(row["evidence"]), where + ": evidence is required")
        if row["status"] == "explicit_zero":
            for value_field in ("amount", "quantity"):
                if value_field in row:
                    need(numeric(row[value_field], where, value_field == "amount") == ZERO,
                         where + ": explicit_zero cannot carry a nonzero value")
        row.update(period=period, source_sha256=digest, location=where)
        row["source_refs"] = [{"dataset_id": schema.get("dataset_id", "csv-unregistered"),
            "source_uri": (source_uris or {}).get(name, "upload://sources/" + name + ".csv"),
            "field": cols["quantity"] if row["status"] == "computed" and "quantity" in cols else cols.get("amount", cols.get("quantity", cols["id"])),
            "selector": urlencode({"row": line, cols["id"]: row["id"]}),
            "kind": "raw", "formula": ""}]
        if "quantity" in cols and row["source_refs"][0]["field"] != cols["quantity"]:
            row["source_refs"].append({**row["source_refs"][0], "field": cols["quantity"]})
        records.append(row)
    need(records and len(records) <= 20000, name + ": empty source or record limit")
    # Preserve original line order within a month; original references must precede use.
    return sorted(records, key=lambda row: row["period"])


def _accounts(value, required, optional=frozenset()):
    need(isinstance(value, dict) and value and len(value) <= 1000, "explicit bounded accounts required")
    need(len({canonical(k) for k in value}) == len(value), "aliased account ids")
    for name, cfg in value.items():
        need(isinstance(name, str) and name.strip() and isinstance(cfg, dict)
             and required <= set(cfg) <= required | set(optional)
             and all(isinstance(cfg[k],str) and (cfg[k].strip() or k == "policy_source") for k in required)
             and all(isinstance(v,str) for v in cfg.values()), name + ": invalid account declaration")
    return value


def _bindings(value, accounts, key):
    need(isinstance(value, list) and len(value) <= 1000, "cost_bindings must be a bounded list")
    seen_assets = set()
    normalized = []
    for item in value:
        need(isinstance(item, dict) and set(item) == {key, "cost_element", "object_ids"},
             "invalid cost binding")
        ids, objects = item[key], item["object_ids"]
        need(isinstance(ids, list) and ids and all(isinstance(x,str) and x for x in ids) and len(set(ids)) == len(ids) and set(ids) <= set(accounts),
             "unknown or duplicate binding accounts")
        need(not (set(ids) & seen_assets), "account recognized cost bound more than once")
        seen_assets.update(ids)
        need(isinstance(objects, list) and objects and all(isinstance(x, str) and x for x in objects)
             and len(set(objects)) == len(objects), "invalid binding objects")
        need(isinstance(item["cost_element"], str) and item["cost_element"], "cost element required")
        normalized.append(dict(item))
    return normalized


def compile_financial_ledgers(files, schema, source_uris=None, *, recognized_entries=()):
    cfg = schema.get("business_views", {}).get("financial_ledgers")
    if cfg is None:
        return None
    need(isinstance(cfg, dict) and {"version", "policy_source", "history_start"} <= set(cfg)
         <= {"version", "policy_source", "history_start", "settlement", "assets", "custody"},
         "invalid configuration fields")
    need(type(cfg["version"]) is int and cfg["version"] == 1 and isinstance(cfg["policy_source"], str)
         and cfg["policy_source"], "version and declared approved policy source required")
    scope = schema.get("raw_composite", {}).get("scope")
    need(isinstance(scope, dict) and set(scope) == SCOPE and all(isinstance(v,str) and v.strip() for v in scope.values()) and scope["currency"] == "CNY",
         "common CNY ledger scope required")
    start = cfg["history_start"]
    need(isinstance(start, str) and MONTH.fullmatch(start) is not None and MONTH.fullmatch(scope["period"])
         and start <= scope["period"], "invalid history window")
    plan = {"version": 1, "policy_source": cfg["policy_source"], "scope": dict(scope),
            "history_start": start}
    seen = set()
    if "settlement" in cfg:
        opt = cfg["settlement"]
        need(isinstance(opt, dict) and {"file", "columns", "accounts"} <= set(opt)
             <= {"file", "columns", "accounts", "cost_bindings"}, "invalid settlement options")
        accounts = _accounts(opt["accounts"], {"kind", "opening_ref"}, {"counterparty", "revenue_treatment", "revenue_policy_source"})
        for name, acc in accounts.items():
            need(acc["kind"] in {"receivable", "payable", "agency"} and acc["opening_ref"],
                 name + ": account kind and approved opening_ref required")
            if acc["kind"] == "receivable":
                need(acc.get("revenue_treatment") in {"recognized_service", "billing_only"}, name + ": AR revenue treatment required")
                need(acc["revenue_treatment"] != "recognized_service" or acc.get("revenue_policy_source"),
                     name + ": recognized service revenue policy evidence required")
            else:
                need("revenue_treatment" not in acc and "revenue_policy_source" not in acc,
                     name + ": revenue treatment is only valid for receivables")
        fields = {"id", "account_id", "kind", "op", "amount", "ref", "status", "evidence"}
        rows = _source(files, schema, opt, fields, scope, start, source_uris, seen)
        for row in rows:
            need(row["account_id"] in accounts and row["kind"] == accounts[row["account_id"]]["kind"],
                 row["location"] + ": account/kind mismatch")
            need(row["status"] != "computed", row["location"] + ": settlement amount cannot be inferred")
            if row["status"] in {"missing", "not_applicable"}:
                need(row["amount"] == "", row["location"] + ": missing/not-applicable cannot carry a value")
            else:
                row["amount"] = str(numeric(row["amount"], row["location"], True))
        bindings = _bindings(opt.get("cost_bindings", []), accounts, "account_ids")
        need(all(accounts[a]["kind"] == "payable" for b in bindings for a in b["account_ids"]),
             "supplier cost bindings require payable accounts")
        plan["settlement"] = {"accounts": accounts, "records": rows, "cost_bindings": bindings}
    if "assets" in cfg:
        opt = cfg["assets"]
        need(isinstance(opt, dict) and set(opt) == {"file", "columns", "opening", "accounts", "cost_bindings"},
             "invalid asset options")
        accounts = _accounts(opt["accounts"], {"kind", "unit", "valuation", "policy_source"})
        for name, acc in accounts.items():
            need(acc["kind"] in {"inventory", "fixed", "prepaid", "service_in_progress"}
                 and acc["unit"] and acc["policy_source"]
                 and acc["valuation"] in {"declared", "moving_average"}, name + ": invalid asset policy")
            need(acc["valuation"] != "moving_average" or acc["kind"] == "inventory",
                 name + ": moving_average only applies to owned inventory")
        fields = {"id", "asset_id", "quantity", "unit", "amount", "status", "evidence"}
        opening = _source(files, schema, opt["opening"], fields, scope, start, source_uris, seen, opening=True)
        need(len(opening) == len(accounts) and {r["asset_id"] for r in opening} == set(accounts),
             "every asset requires one approved opening control")
        moves = _source(files, schema, opt, fields | {"op", "ref"}, scope, start, source_uris, seen)
        for row in opening + moves:
            need(row["asset_id"] in accounts and row["unit"] == accounts[row["asset_id"]]["unit"],
                 row["location"] + ": asset/unit mismatch")
            need(row["status"] not in {"missing", "not_applicable"},
                 row["location"] + ": required asset value/quantity evidence is unavailable")
            row["quantity"] = str(numeric(row["quantity"], row["location"]))
            if row["status"] == "computed":
                need(row in moves and row["op"] == "consume"
                     and accounts[row["asset_id"]]["valuation"] == "moving_average" and row["amount"] == "",
                     row["location"] + ": computed amount requires approved moving_average consumption")
            else:
                row["amount"] = str(numeric(row["amount"], row["location"], True))
        plan["assets"] = {"accounts": accounts, "opening": opening, "records": moves,
            "cost_bindings": _bindings(opt["cost_bindings"], accounts, "asset_ids")}
    if "custody" in cfg:
        opt = cfg["custody"]
        need(isinstance(opt, dict) and set(opt) == {"file", "columns", "goods"}, "invalid custody options")
        goods = _accounts(opt["goods"], {"unit", "opening_ref"})
        rows = _source(files, schema, opt, {"id", "goods_id", "op", "quantity", "unit", "ref", "status", "evidence"},
                       scope, start, source_uris, seen)
        for row in rows:
            need(row["goods_id"] in goods and row["unit"] == goods[row["goods_id"]]["unit"],
                 row["location"] + ": custody goods/unit mismatch")
            need(row["status"] != "computed", row["location"] + ": custody quantity cannot be inferred")
            if row["status"] in {"missing", "not_applicable"}:
                need(row["quantity"] == "", row["location"] + ": unavailable quantity must be blank")
            else:
                row["quantity"] = str(numeric(row["quantity"], row["location"]))
        plan["custody"] = {"goods": goods, "records": rows}
    need(any(k in plan for k in ("settlement", "assets", "custody")), "at least one financial ledger is required")
    return plan


def _match_costs(bindings, expected, result, key):
    objects = {r["object_id"]: r for r in result["objects"]}
    claimed_pairs = set()
    rows = []
    bound = set()
    for item in bindings:
        expected_amount = sum((expected[a] for a in item[key]), ZERO)
        need(all(oid in objects for oid in item["object_ids"]), "cost binding references unknown cost object")
        pairs = {(oid, item["cost_element"]) for oid in item["object_ids"]}
        need(not (pairs & claimed_pairs), "cost element/object consumed by multiple bindings")
        claimed_pairs.update(pairs);bound.update(item[key])
        actual = sum((D(objects[oid]["cost_structure"].get(item["cost_element"], "0"))
                      for oid in item["object_ids"]), ZERO)
        need(actual == expected_amount, "asset/supplier recognition does not match engine cost: "
             + str(item) + "; expected=" + str(expected_amount) + "; actual=" + str(actual))
        refs = [ref for oid in item["object_ids"] for c in objects[oid].get("cost_components", [])
                if c.get("element_id") == item["cost_element"] for ref in c.get("source_refs", [])]
        rows.append({**item, "expected_recognition": fmt(expected_amount), "engine_cost": fmt(actual),
                     "status": "matched", "cost_source_refs": refs})
    need(all(amount == 0 or a in bound for a, amount in expected.items()), "unbound nonzero recognized asset cost")
    return rows


def _missing_activity(records, id_field, identities, start, current):
    """Check coverage for each declared subject throughout the history window."""
    month = lambda period: int(period[:4]) * 12 + int(period[5:]) - 1
    expected_months = month(current) - month(start) + 1
    observed = {ident: set() for ident in identities}
    declarations = set()
    movements = set()
    for row in records:
        if row["op"] == "opening":
            continue
        ident, period = row[id_field], row["period"]
        key = (ident, period)
        if row["op"] == "no_activity":
            need(row["status"] == "explicit_zero" and not row["ref"],
                 row["location"] + ": no_activity requires explicit zero and no transaction reference")
            need(key not in declarations and key not in movements,
                 row["location"] + ": no_activity conflicts with other current-period evidence")
            declarations.add(key)
        else:
            need(key not in declarations,
                 row["location"] + ": movement conflicts with no_activity evidence")
            movements.add(key)
        observed[ident].add(period)
    return {ident for ident, periods in observed.items() if len(periods) != expected_months}


def _settlement(plan, current):
    accounts, records = plan["accounts"], plan["records"]
    missing_activity = _missing_activity(records, "account_id", accounts, plan["history_start"], current)
    cost_bound_accounts = {aid for binding in plan["cost_bindings"] for aid in binding["account_ids"]}
    states = {a: {"balance": ZERO, "opening": None, "invoices": {}, "cash": {}, "refs": [],
                 "status": "provided", "net_charge": ZERO, "cash_in": ZERO, "cash_out": ZERO,
                 "charge": ZERO, "credit": ZERO, "opened": False} for a in accounts}
    for row in records:
        aid, kind, op = row["account_id"], row["kind"], row["op"]
        acc, state = accounts[aid], states[aid]
        is_current = row["period"] == current
        if state["opening"] is None and row["period"] >= current:
            state["opening"] = state["balance"]
        state["refs"].extend(row["source_refs"])
        if row["status"] in {"missing", "not_applicable"}:
            need(op == "opening" or state["opened"], row["location"] + " id=" + row["id"] + ": opening evidence required first")
            state["status"] = "missing" if row["status"] == "missing" else "not_applicable"
            if op == "opening": state["opened"] = True
            continue
        amount = D(row["amount"])
        if op == "opening":
            need(not state["opened"] and row["period"] == plan["history_start"]
                 and row["ref"] == acc["opening_ref"], row["location"] + " id=" + row["id"] + ": approved opening missing/duplicate")
            state["opened"] = True;state["balance"] = amount
            state["invoices"][row["id"]] = {"available": amount, "gross": amount, "credited": ZERO, "opening": True}
            if is_current: state["opening"] = amount
            continue
        need(state["opened"] and state["status"] == "provided", row["location"] + " id=" + row["id"] + ": unavailable opening/account evidence")
        if op == "no_activity":
            continue
        if kind in {"receivable", "payable"}:
            if op == "invoice":
                need(not row["ref"], row["location"] + " id=" + row["id"] + ": invoice cannot reference another transaction")
                state["invoices"][row["id"]] = {"available": amount, "gross": amount, "credited": ZERO, "opening": False}
                state["balance"] += amount
                if is_current:state["charge"] += amount;state["net_charge"] += amount
            elif op == "credit":
                original = state["invoices"].get(row["ref"])
                need(original is not None and original["credited"] + amount <= original["gross"], row["location"] + " id=" + row["id"] + ": missing original or excessive credit")
                original["credited"] += amount;original["available"] -= amount;state["balance"] -= amount
                if is_current:state["credit"] += amount;state["net_charge"] -= amount
            elif op in {"receipt", "payment"}:
                need(op == ("receipt" if kind == "receivable" else "payment"), row["location"] + " id=" + row["id"] + ": wrong cash direction")
                original = state["invoices"].get(row["ref"])
                need(original is not None and amount <= original["available"], row["location"] + " id=" + row["id"] + ": missing original or excessive settlement")
                original["available"] -= amount;state["balance"] -= amount
                state["cash"][row["id"]] = {"amount": amount, "reversed": ZERO, "original": row["ref"]}
                if is_current:state["cash_in" if kind == "receivable" else "cash_out"] += amount
            elif op in {"cash_reversal", "refund"}:
                need(op != "refund" or kind == "receivable", row["location"] + " id=" + row["id"] + ": refund requires receivable account")
                original = state["cash"].get(row["ref"])
                need(original is not None and original["reversed"] + amount <= original["amount"],
                     row["location"] + " id=" + row["id"] + ": missing original or excessive cash reversal")
                original["reversed"] += amount;state["balance"] += amount
                state["invoices"][original["original"]]["available"] += amount
                if is_current:state["cash_out" if kind == "receivable" else "cash_in"] += amount
            else:need(False, row["location"] + " id=" + row["id"] + ": unsupported receivable/payable operation")
        else:
            if op == "collect":
                need(not row["ref"], row["location"] + " id=" + row["id"] + ": collect cannot reference another transaction")
                state["invoices"][row["id"]] = {"available": amount, "opening": False}
                state["balance"] += amount
                if is_current:state["cash_in"] += amount;state["charge"] += amount
            elif op in {"remit", "refund"}:
                original = state["invoices"].get(row["ref"])
                need(original is not None and amount <= original["available"], row["location"] + " id=" + row["id"] + ": missing original or excessive agency outflow")
                original["available"] -= amount;state["balance"] -= amount
                if is_current:state["cash_out"] += amount;state["credit"] += amount
            else:need(False, row["location"] + " id=" + row["id"] + ": unsupported agency operation")
        need(kind != "agency" or state["balance"] >= 0, row["location"] + " id=" + row["id"] + ": negative agency liability")
    rows = [];payable = {}
    for aid, acc in accounts.items():
        s = states[aid];need(s["opened"], aid + ": opening evidence missing")
        if s["opening"] is None:s["opening"] = s["balance"]
        if aid in missing_activity and s["status"] == "provided":
            s["status"] = "missing"
        unknown = s["status"] != "provided"
        values = {"opening": None if unknown else fmt(s["opening"]), "charges": None if unknown else fmt(s["charge"]),
                  "credits": None if unknown else fmt(s["credit"]), "cash_in": None if unknown else fmt(s["cash_in"]),
                  "cash_out": None if unknown else fmt(s["cash_out"]), "closing": None if unknown else fmt(s["balance"]),
                  "outstanding_debit": None if unknown else fmt(max(s["balance"] if acc["kind"] == "receivable" else -s["balance"],ZERO)),
                  "outstanding_credit": None if unknown else fmt(max(-s["balance"] if acc["kind"] == "receivable" else s["balance"],ZERO)),
                  "net_billed": None if unknown else fmt(s["net_charge"]) if acc["kind"] == "receivable" else None,
                  "net_revenue": (fmt(s["net_charge"]) if not unknown and acc["kind"] == "receivable"
                                  and acc["revenue_treatment"] == "recognized_service" else None),
                  "supplier_net_charge": None if unknown else fmt(s["net_charge"]) if acc["kind"] == "payable" else None}
        rows.append({"account_id": aid, "kind": acc["kind"], "counterparty": acc.get("counterparty", aid),
                     "status": s["status"], "normal_balance": "debit" if acc["kind"] == "receivable" else "credit",
                     "revenue_treatment": acc.get("revenue_treatment"),
                     "revenue_policy_source": acc.get("revenue_policy_source"), **values, "source_refs": s["refs"]})
        if acc["kind"] == "payable":
            need(not unknown or aid not in cost_bound_accounts, aid + ": missing payable cannot reconcile cost")
            payable[aid] = s["net_charge"]
    complete = all(r["status"] == "provided" for r in rows)
    totals = {k: fmt(sum((D(r[k]) for r in rows if r[k] is not None), ZERO)) if complete else None
              for k in ("cash_in", "cash_out")}
    totals["net_cash_from_declared_accounts"] = (fmt(D(totals["cash_in"]) - D(totals["cash_out"])) if complete else None)
    for kind, field in [("receivable", "receivable_closing"), ("payable", "payable_closing"), ("agency", "agency_liability_closing")]:
        items = [r for r in rows if r["kind"] == kind]
        totals[field] = fmt(sum((D(r["closing"]) for r in items), ZERO)) if items and all(r["closing"] is not None for r in items) else None
    receivables = [r for r in rows if r["kind"] == "receivable"]
    totals["net_billed_receivables"] = fmt(sum((D(r["net_billed"]) for r in receivables), ZERO)) if receivables and all(r["net_billed"] is not None for r in receivables) else None
    revenue_complete = bool(receivables) and all(r["net_revenue"] is not None for r in receivables)
    totals["declared_net_revenue"] = fmt(sum((D(r["net_revenue"]) for r in receivables), ZERO)) if revenue_complete else None
    totals["revenue_status"] = "declared_recognized_service" if revenue_complete else "billing_only_or_incomplete"
    return {"status": "provided" if complete else "incomplete", "rows": rows, "totals": totals,
            "agency_is_income_or_cost": False}, payable


def _assets(plan, current):
    accounts = plan["accounts"]
    missing_activity = _missing_activity(plan["records"], "asset_id", accounts, plan["history_start"], current)
    states = {}
    originals = {}
    for row in plan["opening"]:
        aid = row["asset_id"];q, amount = D(row["quantity"]), D(row["amount"])
        states[aid] = {"q": q, "amount": amount, "opening_q": None, "opening_amount": None,
                       "additions": ZERO, "recognized": ZERO, "capitalized": ZERO, "release": ZERO,
                       "refs": list(row["source_refs"]), "events": []}
        originals[row["id"]] = {"asset_id": aid, "quantity": q, "amount": amount, "released_q": ZERO, "released_amount": ZERO}
    for source_row in plan["records"]:
        row = dict(source_row);aid, op, q = row["asset_id"], row["op"], D(row["quantity"])
        acc, s = accounts[aid], states[aid];is_current = row["period"] == current
        if s["opening_amount"] is None and row["period"] >= current:s["opening_q"], s["opening_amount"] = s["q"], s["amount"]
        if op == "no_activity":
            s["refs"].extend(row["source_refs"])
            if is_current:
                s["events"].append(row)
            continue
        need(op in {"acquire", "capitalize", "consume", "depreciate", "amortize", "release"},
             row["location"] + " id=" + row["id"] + ": unsupported asset movement")
        if op in {"acquire", "capitalize"}:
            need(row["status"] != "computed" and (not row["ref"] or row["ref"] in originals),
                 row["location"] + " id=" + row["id"] + ": invalid addition reference")
            need(not row["ref"] or originals[row["ref"]]["asset_id"] == aid, row["location"] + " id=" + row["id"] + ": addition reference belongs to another asset")
            need(op != "capitalize" or acc["kind"] == "service_in_progress", row["location"] + " id=" + row["id"] + ": capitalization requires declared service asset")
            need(op != "acquire" or acc["kind"] != "service_in_progress", row["location"] + " id=" + row["id"] + ": service asset requires explicit capitalization")
            amount = D(row["amount"]);s["q"] += q;s["amount"] += amount
            originals[row["id"]] = {"asset_id": aid, "quantity": q, "amount": amount, "released_q": ZERO, "released_amount": ZERO}
            if is_current:s["additions"] += amount;s["capitalized"] += amount if op == "capitalize" else ZERO
        else:
            permitted = {"inventory": "consume", "fixed": "depreciate", "prepaid": "amortize", "service_in_progress": "release"}[acc["kind"]]
            need(op == permitted, row["location"] + " id=" + row["id"] + ": movement does not match asset policy")
            need(q <= s["q"] and (op != "depreciate" or q == 0), row["location"] + " id=" + row["id"] + ": quantity exceeds available asset")
            if acc["valuation"] == "moving_average":
                need(s["q"] > 0 or q == 0, row["location"] + " id=" + row["id"] + ": zero valuation denominator")
                derived = (s["amount"] * q / s["q"]).quantize(D(".01"), rounding=ROUND_HALF_UP) if s["q"] else ZERO
                need(row["status"] == "computed" or D(row["amount"]) == derived,
                     row["location"] + " id=" + row["id"] + ": amount differs from approved moving_average valuation")
                amount = derived;row["amount"] = fmt(amount);row["valuation_formula"] = "carrying_value * consumed_quantity / available_quantity; ROUND_HALF_UP(.01)"
            else:
                need(row["status"] != "computed", row["location"] + " id=" + row["id"] + ": declared schedule amount required")
                amount = D(row["amount"])
            need(amount <= s["amount"], row["location"] + " id=" + row["id"] + ": amount exceeds carrying asset")
            original = originals.get(row["ref"])
            need(original is not None and original["asset_id"] == aid, row["location"] + " id=" + row["id"] + ": missing/mismatched original asset record")
            if op == "release":
                need(original["released_q"] + q <= original["quantity"] and original["released_amount"] + amount <= original["amount"],
                     row["location"] + " id=" + row["id"] + ": release exceeds original capitalized balance")
                original["released_q"] += q;original["released_amount"] += amount
            s["q"] -= q;s["amount"] -= amount
            if is_current:s["recognized"] += amount;s["release"] += amount if op == "release" else ZERO
        s["refs"].extend(row["source_refs"])
        if is_current:s["events"].append(row)
    rows = [];recognized = {}
    for aid, acc in accounts.items():
        need(aid not in missing_activity,
             aid + ": asset journal missing activity evidence for a period; provide movements or explicit no_activity")
        s = states[aid]
        if s["opening_amount"] is None:s["opening_q"], s["opening_amount"] = s["q"], s["amount"]
        need(s["opening_amount"] + s["additions"] - s["recognized"] == s["amount"], aid + ": asset monetary bridge failed")
        recognized[aid] = s["recognized"]
        rows.append({"asset_id": aid, "kind": acc["kind"], "unit": acc["unit"], "status": "provided",
                     "opening_quantity": str(s["opening_q"]), "closing_quantity": str(s["q"]),
                     "opening_value": fmt(s["opening_amount"]), "additions": fmt(s["additions"]),
                     "recognized_cost": fmt(s["recognized"]), "capitalized_service_additions": fmt(s["capitalized"]),
                     "service_release": fmt(s["release"]), "closing_value": fmt(s["amount"]),
                     "opening_basis": "approved_opening_control" if current == plan["history_start"] else "computed_prior_period_closing",
                     "policy_source": acc["policy_source"], "source_refs": s["refs"], "current_events": s["events"]})
    totals = {key: fmt(sum((D(r[key]) for r in rows), ZERO)) for key in
              ("opening_value", "additions", "recognized_cost", "closing_value", "capitalized_service_additions", "service_release")}
    return {"status": "provided", "rows": rows, "totals": totals,
            "not_a_complete_balance_sheet": True, "never_add_closing_to_cost_total": True}, recognized


def _custody(plan, current):
    missing_activity = _missing_activity(plan["records"], "goods_id", plan["goods"], plan["history_start"], current)
    states = {g: {"q": ZERO, "opening": None, "opened": False, "status": "provided", "refs": [], "dispatches": {}} for g in plan["goods"]}
    for row in plan["records"]:
        gid, op = row["goods_id"], row["op"];s=states[gid];cfg=plan["goods"][gid]
        if s["opening"] is None and row["period"] >= current:s["opening"] = s["q"]
        s["refs"].extend(row["source_refs"])
        if row["status"] in {"missing", "not_applicable"}:
            s["status"] = row["status"]
            if op == "opening":s["opened"] = True
            continue
        q = D(row["quantity"])
        if op == "opening":
            need(not s["opened"] and row["period"] == plan["history_start"] and row["ref"] == cfg["opening_ref"],
                 row["location"] + " id=" + row["id"] + ": approved custody opening missing/duplicate")
            s["opened"] = True;s["q"] = q
            if row["period"] == current:s["opening"] = q
        else:
            need(s["opened"] and s["status"] == "provided", row["location"] + " id=" + row["id"] + ": custody opening/evidence unavailable")
            if op == "no_activity":
                continue
            if op == "receive":need(not row["ref"], row["location"] + " id=" + row["id"] + ": unexpected receive reference");s["q"] += q
            elif op == "dispatch":
                need(q <= s["q"] and not row["ref"], row["location"] + " id=" + row["id"] + ": custody dispatch exceeds available")
                s["q"] -= q;s["dispatches"][row["id"]] = [q, ZERO]
            elif op == "return":
                original = s["dispatches"].get(row["ref"])
                need(original is not None and original[1] + q <= original[0], row["location"] + " id=" + row["id"] + ": missing original or excessive custody return")
                original[1] += q;s["q"] += q
            else:need(False, row["location"] + " id=" + row["id"] + ": unsupported custody operation")
    rows=[]
    for gid, cfg in plan["goods"].items():
        s=states[gid];need(s["opened"], gid + ": custody opening missing")
        if gid in missing_activity and s["status"] == "provided":
            s["status"] = "missing"
        rows.append({"goods_id": gid, "unit": cfg["unit"], "status": s["status"],
                     "opening_quantity": str(s["q"] if s["opening"] is None else s["opening"]) if s["status"] == "provided" else None,
                     "closing_quantity": str(s["q"]) if s["status"] == "provided" else None,
                     "provider_owned_inventory": False, "source_refs": s["refs"]})
    return {"status": "provided" if all(r["status"]=="provided" for r in rows) else "incomplete",
            "rows": rows, "monetary_asset_or_cost": False}


def evaluate_financial_ledgers(plan, result):
    with localcontext() as ctx:
        ctx.prec = 60
        out={"version":1,"policy_source":plan["policy_source"],"scope":plan["scope"],"history_start":plan["history_start"],
             "status":"provided","adds_engine_cost":False}
        if result.get("scope",{}).get("type") == "object":
            return {**out,"status":"requires_book_scope","reason":"Auxiliary financial ledgers require all-object book scope.",
                    "settlement":None,"assets":None,"custody":None}
        if "settlement" in plan:
            p={**plan["settlement"],"history_start":plan["history_start"]}
            out["settlement"],payable=_settlement(p,plan["scope"]["period"])
            selected = {a: payable[a] for b in p["cost_bindings"] for a in b["account_ids"]}
            out["settlement"]["cost_reconciliation"]=_match_costs(p["cost_bindings"],selected,result,"account_ids") if p["cost_bindings"] else []
        if "assets" in plan:
            p={**plan["assets"],"history_start":plan["history_start"]}
            out["assets"],recognized=_assets(p,plan["scope"]["period"])
            out["assets"]["cost_reconciliation"]=_match_costs(p["cost_bindings"],recognized,result,"asset_ids")
            totals=out["assets"]["totals"];engine=D(result["totals"]["total_cost"]);recognized_total=D(totals["recognized_cost"])
            need(engine >= recognized_total,"asset recognition exceeds engine costs")
            out["cost_bridge"]={"engine_cost_total":fmt(engine),"asset_recognition_in_engine":fmt(recognized_total),
                "engine_other_costs":fmt(engine-recognized_total),"separate_asset_closing":totals["closing_value"],
                "approved_service_capitalization":totals["capitalized_service_additions"],
                "prior_service_asset_release":totals["service_release"],
                "cost_plus_approved_service_capitalization":fmt(engine+D(totals["capitalized_service_additions"])),
                "declared_resource_activity":fmt(engine-recognized_total+D(totals["additions"])),
                "asset_opening_plus_activity":fmt(D(totals["opening_value"])+engine-recognized_total+D(totals["additions"])),
                "cost_plus_asset_closing":fmt(engine+D(totals["closing_value"])),
                "closing_is_not_added_to_legacy_cost_or_production_asset":True}
            need(out["cost_bridge"]["asset_opening_plus_activity"]==out["cost_bridge"]["cost_plus_asset_closing"],
                 "combined declared resource bridge failed")
        if "custody" in plan:
            out["custody"]=_custody({**plan["custody"],"history_start":plan["history_start"]},plan["scope"]["period"])
        if any(out.get(k,{}).get("status") == "incomplete" for k in ("settlement","assets","custody")):
            out["status"] = "incomplete"
        return out