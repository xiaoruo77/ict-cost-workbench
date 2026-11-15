import copy
import csv
import io
import json
from decimal import Decimal as D
from pathlib import Path
from tempfile import TemporaryDirectory
from urllib.parse import parse_qsl
import unittest

from test_inventory_adapter import fixture, convert
from test_meter_adapter import config, line
from cost_core.csv_adapter import CsvCostingAdapter
from cost_core.costing import CostingEngine
from costing_outputs import create_snapshot,load_snapshot,export_snapshot

def setup():
    retail=fixture()
    _,inv=convert(retail)
    meter=config()
    meter["scope"]["book"]=retail["context"]["book"]
    rows=[]
    for pos,item in enumerate(retail["transactions"]):
        rows.append({**item,"input_name":"stock","posting_identity":f"inv-{pos}"})
    service={**line(meter,150000,id="utility-1"),"input_name":"tariff","posting_identity":"utility-1"}
    rows.insert(1,service)
    schema={"dataset_id":"csv-123456789abcdef0","raw_composite":{
        "version":1,"scope":{x:retail["context"][x] for x in ("book","period","currency")},
        "inputs":{"stock":{"raw_inventory":inv["raw_inventory"]},"tariff":{"raw_meter":meter}}}}
    return schema,rows

def text(rows):
    f=io.StringIO()
    w=csv.DictWriter(f,fieldnames=sorted({k for row in rows for k in row}))
    w.writeheader();w.writerows(rows)
    return f.getvalue()

def evaluate(schema,rows):
    req=CsvCostingAdapter().from_text(text(rows),schema)
    return req,CostingEngine().analyze(req)

class CompositeTests(unittest.TestCase):
    def test_expense_ledgers_but_not_memo_sum(self):
        req,out=evaluate(*setup());rec=out["recognition"]
        self.assertEqual(D(out["totals"]["total_cost"]),D("1032.80"))
        self.assertEqual(rec["posted_pool_totals"],{"stock":"382.80","tariff":"650.00"})
        self.assertEqual(rec["inputs"]["stock"]["inventory_valuation"]["outputs"]["closing_value"],"277.20")
        self.assertTrue(out["ledger_reconciled"])
        self.assertFalse(rec["can_claim_complete"])
        self.assertEqual(set(e.entry_id.split(":")[0] for e in req.entries),{"stock","tariff"})
        sale=next(e for e in req.entries if e.amount==D("396.00"))
        self.assertEqual(dict(parse_qsl(sale.source_refs[0].selector)),
                         {"row":"4","source_id":"DOC_SALE_1:LINE_1","input_name":"stock"})
        tariff=next(e for e in req.entries if e.amount==D("650.00"))
        self.assertEqual(dict(parse_qsl(tariff.source_refs[0].selector)),
                         {"row":"3","id":"utility-1","input_name":"tariff"})

    def test_atomic_reject_overlap_and_scope(self):
        for what in ("economic","source","month","currency","book","undeclared","missing_branch","trust"):
            with self.subTest(what=what), self.assertRaises(ValueError):
                s,r=setup()
                if what=="economic": r[1]["posting_identity"]=r[0]["posting_identity"]
                if what=="source": r[1]["id"]=r[0]["source_id"]
                if what=="month": r[1]["period"]="2026-12"
                if what=="currency": r[1]["currency"]="EUR"
                if what=="book": r[1]["book"]="other"
                if what=="undeclared": r[1]["input_name"]="evil"
                if what=="missing_branch": r.pop(1)
                if what=="trust": s["trusted_authority_id"]="forged"
                evaluate(s,r)

    def test_third_input_retains_excluded_nonzero_gap(self):
        from test_adapter_recognition import convert as convert_recognition
        s,rows=setup()
        ctx=s["raw_composite"]["scope"]
        raw={"name":"small period fees","context":ctx,
             "rules":[{"id":"admin-rec","op":"recognize","source":"actual-admin","posting_pool":"period"}],
             "outputs":["admin-rec"],"excluded_sources":{"unsettled":"untied"},
             "sources":[{**ctx,"id":"actual-admin","kind":"expense","status":"data","amount":"44.00",
                         "economic_key":"admin","evidence":"memo"},
                        {**ctx,"id":"unsettled","kind":"expense","status":"data","amount":"99.00",
                         "economic_key":"unsettled","evidence":"to match"}]}
        _,inner=convert_recognition(raw)
        s["raw_composite"]["inputs"]["period_cost"]={"raw_recognition":inner["raw_recognition"]}
        rows += [{**x,"posting_identity":x["id"],"input_name":"period_cost"} for x in raw["sources"]]
        _,result=evaluate(s,rows)
        rec=result["recognition"]
        self.assertEqual(D(result["totals"]["total_cost"]),D("1076.80"))
        self.assertEqual(rec["source_recognition_status"],"partial")
        self.assertEqual(rec["unrecognized_nonzero_sources"][0]["input_name"],"period_cost")
        self.assertFalse(rec["can_claim_complete"])

    def test_real_same_revision_exports_branch_aware_evidence(self):
        s,r=setup()
        req,result=evaluate(s,r)
        with TemporaryDirectory() as t:
            base=Path(t);ds=base/"costing_datasets"/s["dataset_id"];ds.mkdir(parents=True)
            (ds/"data.csv").write_text(text(r));(ds/"manifest.json").write_text(json.dumps({"schema":s}))
            init=create_snapshot(result,base,base,dict(dataset_id=s["dataset_id"],dataset_label="合并",
                     period="2026-11",scenario="actual",owner_external_id="qa"))
            snap=load_snapshot(init["analysis_id"],base)
            self.assertEqual(snap["revision"],init["revision"])
            entries=[e for e in snap["evidence"] if e["status"]=="captured"]
            self.assertEqual({e["rows"][0]["record"] for e in entries},set(range(2,7)))
            refs={e["selector"]:e for e in snap["evidence"]}
            self.assertEqual(refs["raw_composite.inputs.stock.raw_inventory.opening"]["mapping"]["configuration"]["value"],"240.00")
            self.assertEqual(refs["raw_composite.inputs.tariff.raw_meter.profile"]["mapping"]["configuration"]["unit"],"request")
            md=export_snapshot(snap,"md",base/"export").read_text()
            for term in ("组合台账","382.80","650.00","1,032.80","277.20","不再次入账"):
                self.assertIn(term,md)

if __name__=="__main__": unittest.main()
