import copy
import csv
from decimal import Decimal
import io
import json
from pathlib import Path
import tempfile
import unittest
from urllib.parse import parse_qsl

from cost_core.csv_adapter import CsvCostingAdapter
from cost_core.costing import CostingEngine
from costing_outputs import create_snapshot, load_snapshot, export_snapshot, resolve_evidence

def fixture(name="retail"):
    return json.loads((Path(__file__).resolve().parents[1] / "fixtures" / "recognition"/(name+".json")).read_bytes())

def convert(raw):
    config=copy.deepcopy(raw)
    rows=config.pop("transactions")
    dest=dict(object_id="SKU_1",object_kind="product",cost_type="direct",behavior="variable")
    config["posting_map"]={k:dict(dest,cost_element=k) for k in ("cogs","shrink")}
    result=io.StringIO()
    w=csv.DictWriter(result,fieldnames=sorted({k for r in rows for k in r}))
    w.writeheader();w.writerows(rows)
    return result.getvalue(), {"dataset_id":"csv-123456789abcdef0","period":config["context"]["period"],"raw_inventory":config}

class InventoryAdapter(unittest.TestCase):
    def analyze(self,raw):
        text,schema=convert(raw)
        req=CsvCostingAdapter().from_text(text,schema)
        return req,CostingEngine().analyze(req)

    def test_invoice_not_expensed_and_original_return_lineage(self):
        req,ans=self.analyze(fixture())
        self.assertEqual([x.amount for x in req.entries],list(map(Decimal,("396.00","-26.40","13.20"))))
        self.assertEqual(Decimal(ans["totals"]["total_cost"]),Decimal("382.80"))
        v=ans["recognition"]["inventory_valuation"]
        self.assertEqual(v["outputs"]["available_value"],"660.00")
        self.assertEqual(v["outputs"]["net_cogs"],"369.60")
        self.assertEqual(v["outputs"]["shrink"],"13.20")
        self.assertEqual(v["outputs"]["closing_value"],"277.20")
        self.assertEqual(v["outputs"]["closing_quantity"],"21")
        self.assertTrue(ans["ledger_reconciled"])
        self.assertFalse(ans["recognition"]["can_claim_complete"])
        self.assertEqual([dict(parse_qsl(r.selector))["row"] for r in req.entries[1].source_refs], ["4","3"])

    def test_cross_month_original_source_retained_without_fake_local_row(self):
        req,ans=self.analyze(fixture("cross_month_return"))
        v=ans["recognition"]["inventory_valuation"]
        post=v["cost_postings"][0]
        self.assertTrue(post["original_source_id"])
        self.assertNotEqual(post["original_issue_period"],v["context"]["period"])
        self.assertEqual(len(req.entries[0].source_refs),1)
        self.assertLess(req.entries[0].amount,0)
        original_move=next(x for x in v["stock_movements"] if x["kind"]=="sale_return")
        self.assertEqual(original_move["original_source_id"],post["original_source_id"])
        self.assertTrue(original_move["original_evidence"])
        blocked=fixture("cross_month_return")
        blocked["opening"]["issues"][0].pop("evidence")
        with self.assertRaisesRegex(ValueError,"Missing required fields.*evidence"):
            self.analyze(blocked)

    def test_unknown_cross_book_out_of_order_duplicate_outside_cutoff_and_injected_trust(self):
        for case in ("book","order","duplicate","cutoff","policy","trust","schema","currency"):
            with self.subTest(case=case), self.assertRaises(ValueError):
                m=fixture()
                if case=="book": m["transactions"][0]["book"]="OTHER"
                if case=="order": m["transactions"][:2]=reversed(m["transactions"][:2])
                if case=="duplicate": m["transactions"][1]["source_id"]=m["transactions"][0]["source_id"]
                if case=="cutoff": m["transactions"][0]["at"]="2026-12-01T12:00:00Z"
                if case=="policy": m["policy"]="unsafe"
                if case=="currency": m["context"]["currency"]="JPY"
                value,s=convert(m)
                if case=="trust": s["trusted_authority_id"]="forged"
                if case=="schema": s["raw_recognition"]={}
                CsvCostingAdapter().from_text(value,s)

    def test_receipt_only_records_zero_expenses(self):
        raw=fixture()
        raw["transactions"]=raw["transactions"][:1]
        req,ans=self.analyze(raw)
        self.assertFalse(req.entries)
        self.assertEqual(Decimal(ans["totals"]["total_cost"]),0)
        self.assertEqual(ans["recognition"]["inventory_valuation"]["outputs"]["closing_value"],"660.00")

    def test_actual_snapshot_native_markdown_display_and_scope(self):
        value,s=convert(fixture())
        req=CsvCostingAdapter().from_text(value,s)
        ans=CostingEngine().analyze(req)
        with tempfile.TemporaryDirectory() as name:
            root=Path(name); ds=root/"costing_datasets"/s["dataset_id"]; ds.mkdir(parents=True)
            (ds/"data.csv").write_text(value,encoding="utf8")
            (ds/"manifest.json").write_text(json.dumps({"schema":s}),encoding="utf8")
            obj=create_snapshot(ans,root,root,dict(dataset_id=s["dataset_id"],dataset_label="pos",period=s["period"],
                                        scenario="actual",owner_external_id="reuse-qa"))
            got=load_snapshot(obj["analysis_id"],root)
            text=export_snapshot(got,"md",root/"exports").read_text()
            self.assertEqual(got["revision"],obj["revision"])
            self.assertTrue(any("DOC_RECEIPT_1" in x["selector"] and x["rows"][0]["value"]=="30" for x in got["evidence"]))
            for term in ("未经独立库存清单核实","净出库销售成本","369.60","277.20","期末存货，不入费用"):
                self.assertIn(term,text)

if __name__=="__main__":
    unittest.main()
