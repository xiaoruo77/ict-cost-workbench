"""Original FX trace only; booked CNY is the existing ledger term."""
from copy import deepcopy
from decimal import Decimal as D
import json
from pathlib import Path
import tempfile
import unittest

from costing_outputs import create_snapshot,load_snapshot,export_snapshot
from cost_core.csv_adapter import CsvCostingAdapter
from cost_core.costing import CostingEngine
from cost_core.foreign_booked import CONFIG, FIELDS, MODE
from test_composite_adapter import evaluate,text
from test_composite_meter_v3 import two_accounts


def fixture(credit=True):
    s,r=two_accounts()
    cfg=s["raw_composite"]["inputs"]["period"]
    rec=cfg["raw_recognition"]
    rec[CONFIG]=MODE
    expense=next(x for x in r if x["id"]=="rent")
    expense.update(amount="71.45",fx_original_id="SUPPLIER-USD-001",fx_original_amount="10.03",
                   fx_original_date="2026-11-05",fx_original_currency="USD",
                   fx_booked_amount="71.45",fx_booking_evidence="UPSTREAM-DEC-17")
    if credit:
        item=deepcopy(expense)
        item.update(id="credit",kind="credit",amount="-14.32",economic_key="vendor-credit",
                    posting_identity="supplier-credit",fx_original_id="SUPPLIER-EUR-002",
                    fx_original_amount="-2.01",fx_original_currency="EUR",fx_booked_amount="-14.32")
        r.append(item)
        rec["rules"].append(dict(id="credit-rule",op="recognize",source="credit",posting_pool="period"))
        rec["outputs"].append("credit-rule")
        rec["posting_map"]["credit-rule"]={**rec["posting_map"]["rent-rule"],"pool_id":"credit-rule"}
        cfg["allocation_rules"].append({**cfg["allocation_rules"][0],"pool_id":"credit-rule"})
    return s,r


class ForeignBooked(unittest.TestCase):
    def test_composite_same_ledger_immutable_same_revision_refs(self):
        s,r=fixture()
        req,out=evaluate(s,r)
        branch=out["recognition"]["inputs"]["period"]
        t=branch[CONFIG]
        self.assertEqual(branch["ledger_recognized_total"],"57.13")
        self.assertFalse(t["can_claim_independent_verification"])
        self.assertEqual(D(out["totals"]["total_cost"]),D("2234.23"))
        self.assertEqual({(x["origin_currency"],x["origin_amount"],x["booked_amount"]) for x in t["rows"]},
                         {("USD","10.03","71.45"),("EUR","-2.01","-14.32")})
        self.assertEqual({x["recognition_status"] for x in t["rows"]},{"recognized"})
        self.assertEqual({p.amount for p in req.pools},{D("71.45"),D("-14.32")})
        base_s,base_r=deepcopy((s,r))
        del base_s["raw_composite"]["inputs"]["period"]["raw_recognition"][CONFIG]
        for row in base_r:
            for k in FIELDS: row.pop(k,None)
        baseline=evaluate(base_s,base_r)[1]
        for k in ("totals","ledger_reconciled","quality","scope"):
            self.assertEqual(out[k],baseline[k])
        self.assertNotIn(CONFIG,baseline["recognition"]["inputs"]["period"])
        with tempfile.TemporaryDirectory() as d:
            root=Path(d)
            source=root/"costing_datasets"/s["dataset_id"]
            source.mkdir(parents=True)
            (source/"data.csv").write_text(text(r),encoding="utf-8")
            (source/"manifest.json").write_text(json.dumps({"schema":s}),encoding="utf-8")
            original=create_snapshot(out,root,root,dict(dataset_id=s["dataset_id"],
                    period="2026-11",scenario="actual",owner_external_id="abc"))
            frozen=load_snapshot(original["analysis_id"],root)
            self.assertEqual(frozen["revision"],original["revision"])
            md=export_snapshot(frozen,"md",root/"export").read_text()
            self.assertIn("不计算汇率",md)
            self.assertIn("SUPPLIER-EUR-002",md)
            references={(x["field"],x["selector"]):x for x in frozen["evidence"]}
            for row in t["rows"]:
                self.assertEqual(len(row["source_refs"]),7)
                for ref in row["source_refs"]:
                    captured=references[ref["field"],ref["selector"]]
                    self.assertEqual(captured["status"],"captured")
                    self.assertEqual(captured["rows"][0]["values"]["id"],row["source_id"])
                    self.assertEqual(captured["rows"][0]["values"]["input_name"],"period")
            self.assertEqual(json.loads(export_snapshot(frozen,"json",root/"export").read_bytes())["revision"],
                             original["revision"])

    def test_source_and_fx_injection_fail_closed(self):
        for case, message in (
            ("wrong_book","same-book"), ("yen","USD or EUR"), ("cny","USD or EUR"),
            ("wrong_base","contradicts"), ("credit_sign","sign mismatch"), ("debit_sign","sign mismatch"),
            ("aliased_foreign","aliased or duplicate"), ("native_alias","aliases an existing"),
            ("economic_origin_alias","aliases an existing"), ("aliased_economic_keys","aliased economic identity"),
            ("bad_date","booked month"), ("future_date","booked month"), ("discounts","same-period"),
            ("missing_trace_field","incomplete"), ("injected_rate","headers"), ("no_opt_in","headers"),
            ("repeat_recognition","Duplicate posting economic component"), ("legacy_composite","requires v3"),
        ):
            s,r=fixture(credit=case!="legacy_composite")
            x=next(row for row in r if row["id"]=="rent")
            rec=s["raw_composite"]["inputs"]["period"]["raw_recognition"]
            if case=="wrong_book": x["book"]="OTHER"
            if case=="yen": x["fx_original_currency"]="JPY"
            if case=="cny": x["fx_original_currency"]="CNY"
            if case=="wrong_base": x["fx_booked_amount"]="71.46"
            if case=="credit_sign": next(row for row in r if row["id"]=="credit")["fx_original_amount"]="2.01"
            if case=="debit_sign": x["fx_original_amount"]="-10.03"
            if case=="aliased_foreign": x["fx_original_id"]="ＳＵＰＰＬＩＥＲ-EUR-002"
            if case=="native_alias": x["fx_original_id"]="ＰＯＷＥＲ-a"
            if case=="economic_origin_alias": x["fx_original_id"]="ＶＥＮＤＯＲ-ＣＲＥＤＩＴ"
            if case=="aliased_economic_keys": next(row for row in r if row["id"]=="credit")["economic_key"]="ＲＥＮＴ"
            if case=="bad_date": x["fx_original_date"]="2026-11-31"
            if case=="future_date": x["fx_original_date"]="2026-12-01"
            if case=="discounts": x["kind"]="accrual"
            if case=="missing_trace_field": x.pop("fx_original_amount")
            if case=="injected_rate": x["fx_rate"]="7.2"
            if case=="no_opt_in": rec.pop(CONFIG)
            if case=="repeat_recognition":
                rec["rules"].append(dict(id="duplicate",op="recognize",source="rent",posting_pool="period"))
                rec["outputs"].append("duplicate")
            if case=="legacy_composite":
                s["raw_composite"]["version"]=2
                del s["raw_composite"]["inputs"]["water"]
                r=[row for row in r if row["input_name"]!="water"]
            with self.subTest(case=case), self.assertRaisesRegex(ValueError,message):
                evaluate(s,r)

    def test_excluded_original_keeps_trace_without_a_new_posting(self):
        s,r=fixture()
        cfg=s["raw_composite"]["inputs"]["period"]
        rec=cfg["raw_recognition"]
        rec["rules"].pop()
        rec["outputs"].pop()
        rec["posting_map"].pop("credit-rule")
        cfg["allocation_rules"].pop()
        rec["excluded_sources"]["credit"]="pending independent bookkeeping review"
        req,out=evaluate(s,r)
        branch=out["recognition"]["inputs"]["period"]
        by_id={x["source_id"]:x for x in branch[CONFIG]["rows"]}
        self.assertEqual(by_id["credit"]["recognition_status"],"not_recognized")
        self.assertEqual(by_id["credit"]["booked_amount"],"-14.32")
        self.assertEqual(branch["source_recognition_status"],"partial")
        self.assertEqual(branch["ledger_recognized_total"],"71.45")
        self.assertEqual({x.amount for x in req.pools},{D("71.45")})

    def test_standalone_expense_unchanged_rejects_revaluation_table(self):
        s,r=fixture(credit=False)
        cfg=deepcopy(s["raw_composite"]["inputs"]["period"]["raw_recognition"])
        cfg["posting_map"]["rent-rule"].pop("pool_id")
        cfg["posting_map"]["rent-rule"]["record_type"]="entry"
        r=[{k:v for k,v in x.items() if k not in ("input_name","posting_identity")} for x in r if x["input_name"]=="period"]
        s={"raw_recognition":cfg}
        req=CsvCostingAdapter().from_text(text(r),s)
        self.assertEqual(CostingEngine().analyze(req)["totals"]["total_cost"],"71.45")
        for key,value,msg in (
            ("currency_rates",[],"does not accept rates"), ("fx_rate","99.123456","unknown top-level"),
            ("revaluation","false-label","unknown top-level"), ("anything",True,"unknown top-level"),
        ):
            with self.subTest(key=key), self.assertRaisesRegex(ValueError,msg):
                CsvCostingAdapter().from_text(text(r),{**s,key:value})
        # Same raw exact ID is shared by a memo only, never a second posting.
        memo = dict(r[0],id="cash-memo",kind="cash_movement",amount="71.45")
        for key in FIELDS:
            memo[key]=""
        cfg["excluded_sources"]["cash-memo"]="nonposting payment evidence"
        req=CsvCostingAdapter().from_text(text([*r,memo]),s)
        self.assertEqual(CostingEngine().analyze(req)["totals"]["total_cost"],"71.45")
        self.assertEqual(len(req.entries),1)
