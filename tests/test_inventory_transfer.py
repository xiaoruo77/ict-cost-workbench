"""Exact closed-transfer acceptance via production adapter/valuation/snapshot."""
from copy import deepcopy
from decimal import Decimal as D
from pathlib import Path
from tempfile import TemporaryDirectory
from urllib.parse import parse_qs
import json
import unittest

from cost_core.csv_adapter import CsvCostingAdapter
from cost_core.costing import CostingEngine
from cost_core.inventory_ingest import SORT_CHRONOLOGY, TRANSFER_POLICY
from costing_outputs import create_snapshot,load_snapshot,export_snapshot
from test_composite_adapter import text
from test_composite_meter_v3 import two_accounts

BASE={"book":"Cold_store","period":"2026-11","currency":"CNY","cutoff_at":"2026-11-30T23:59:59Z"}

def case():
    attrs={"book":BASE["book"],"currency":BASE["currency"],"uom":"EA","item":"CRATE"}
    streams=[]
    for site,q,v,status in (("WH_A","10","100","data"),("WH_B","10","200","data"),("WH_C","0","0","evidenced_zero")):
        scope={"warehouse":site,"item":attrs["item"],"uom":attrs["uom"]}
        opened=dict(attrs,warehouse=site,as_of="2026-10-31T23:59:59Z",quantity=q,value=v,status=status,
                    evidence="synthetic opening",issues=[],seen_event_ids=[],seen_source_ids=[])
        mapping=dict(object_id=site,object_kind="product",behavior="variable",cost_type="direct")
        streams.append(dict(scope=scope,opening=opened,posting_map={k:dict(mapping,cost_element=k) for k in ("cogs","shrink")}))
    def row(site,n,kind,qty,key=None):
        m=dict(attrs,warehouse=site,id=f"MOVE{n}",source_id=f"DOC{n}",at=f"2026-11-{n:02d}T10:00:00Z",
               sequence="1",kind=kind,quantity=qty,evidence=f"shipping evidence {n}")
        if key is not None: m["transfer_id"]=key
        return m
    rows=[row("WH_A",1,"transfer_out","4","AtoB"),row("WH_B",3,"transfer_in","4","AtoB"),
          row("WH_B",4,"transfer_out","3","BtoC"),row("WH_C",5,"transfer_in","3","BtoC"),
          row("WH_C",6,"sale_issue","2")]
    return {"dataset_id":"csv-6717524abf4aca75","raw_inventory":{
        "operation":"moving_weighted_average","policy":"line_half_up_minor_units_with_cumulative_original_issue_returns",
        "context":dict(BASE),"chronology":SORT_CHRONOLOGY,"transfer_policy":TRANSFER_POLICY,"transfer_history":[],
        "streams":streams}},rows

def evaluate(s,rows):
    req=CsvCostingAdapter().from_text(text(rows),s)
    return req,CostingEngine().analyze(req)

class InventoryTransfer(unittest.TestCase):
    def snapshot(self,s,rows,result):
        with TemporaryDirectory() as t:
            root=Path(t); ds=root/"costing_datasets"/s["dataset_id"];ds.mkdir(parents=True)
            (ds/"data.csv").write_text(text(rows),encoding="utf8")
            (ds/"manifest.json").write_text(json.dumps({"schema":s}),encoding="utf8")
            first=create_snapshot(result,root,root,dict(dataset_id=s["dataset_id"],dataset_label="transfer synthetic",
                period="2026-11",scenario="actual",owner_external_id="transfer-only"))
            snap=load_snapshot(first["analysis_id"],root)
            j=json.loads(export_snapshot(snap,"json",root/"export").read_text())
            self.assertEqual(first["revision"],j["revision"])
            md=export_snapshot(snap,"md",root/"export").read_text()
            self.assertIn("外部入库",md)
            self.assertIn("发出后至收到前为在途资产，不属于经营费用",md)
            self.assertIn("91.43",md)
            ev={x["selector"]:x for x in snap["evidence"]}
            return ev

    def test_integrated_multihop_golden_and_both_original_events(self):
        s,r=case(); r.reverse()
        req,out=evaluate(s,r)
        self.assertEqual(len(req.entries),1)
        self.assertEqual(D(req.entries[0].amount),D("34.29"))
        self.assertEqual(req.entries[0].cost_type,"direct")
        rec=out["recognition"]; val=rec["inventory_valuation"]
        self.assertFalse(rec["can_claim_complete"])
        self.assertEqual(val["outputs"],dict(available_value="300.00",net_cogs="34.29",shrink="0.00",
                                             closing_value="265.71",reconciliation_difference="0.00"))
        self.assertEqual([x["outputs"]["closing_value"] for x in val["streams"]],["60.00","188.57","17.14"])
        transfers=val["transfers"]; self.assertEqual(transfers["next_transfer_history"],["AtoB","BtoC"])
        self.assertEqual(transfers["closed_value"],"91.43")
        self.assertEqual([x["amount"] for x in transfers["closed_pairs"]],["40.00","51.43"])
        self.assertTrue(all(len(x["source_refs"])==2 for x in transfers["closed_pairs"]))
        evidence=self.snapshot(s,r,out)
        src={parse_qs(ref["selector"])["source_id"][0]:ref["rows"][0]["record"]
             for ref in evidence.values() if ref["status"]=="captured" and ref["field"]=="transfer_id"}
        self.assertEqual(src,{"DOC1":6,"DOC3":5,"DOC4":4,"DOC5":3})
        quantities={parse_qs(ref["selector"])["source_id"][0]:ref["rows"][0]["values"]["quantity"]
                    for ref in evidence.values() if ref["status"]=="captured" and ref["field"]=="transfer_id"}
        self.assertEqual(quantities,{"DOC1":"4","DOC3":"4","DOC4":"3","DOC5":"3"})
        self.assertEqual(evidence["raw_inventory.transfer_policy"]["mapping"]["configuration"],TRANSFER_POLICY)
        self.assertEqual(evidence["raw_inventory.transfer_history"]["mapping"]["configuration"],[])

    def test_source_sale_before_transfer_keeps_pair_ref_index_and_original_expenses(self):
        s,r=case()
        sale={**r[0],"id":"PRE_SALE","source_id":"PRE_SALE_DOC","kind":"sale_issue","quantity":"1",
              "at":"2026-11-01T09:00:00Z"}
        sale.pop("transfer_id")
        req,out=evaluate(s,r+[sale])
        self.assertEqual(D(out["totals"]["total_cost"]),D("44.29"))
        self.assertEqual(sorted(x.amount for x in req.entries),[D("10"),D("34.29")])
        v=out["recognition"]["inventory_valuation"]
        self.assertEqual([t["amount"] for t in v["transfers"]["closed_pairs"]],["40.00","51.43"])
        pairs=[m for stream in v["streams"] for m in stream["stock_movements"] if m.get("transfer_id")]
        self.assertEqual(len(pairs),4)
        self.assertTrue(all(len(m["source_refs"])==2 and
                            len({ref["selector"] for ref in m["source_refs"]})==2 for m in pairs))
        self.assertEqual(v["outputs"]["closing_value"],"255.71")

    def test_no_expense_transfer_only_and_exact_exhaustion(self):
        s,r=case(); r=r[:2]
        r[0]["quantity"]=r[1]["quantity"]="10"
        req,out=evaluate(s,r)
        self.assertFalse(req.entries)
        val=out["recognition"]["inventory_valuation"]; a,b,_=val["streams"]
        self.assertEqual((a["outputs"]["closing_quantity"],a["outputs"]["closing_value"]),("0","0.00"))
        self.assertEqual((b["outputs"]["closing_quantity"],b["outputs"]["closing_value"]),("20","300.00"))
        self.assertEqual(val["transfers"]["closed_pairs"][0]["amount"],"100.00")
        self.assertEqual(out["totals"]["total_cost"],"0")
        self.assertEqual(val["outputs"]["available_value"],val["outputs"]["closing_value"])

    def test_zero_cent_asset_different_dates_arrival_and_external_receipt(self):
        s,r=case(); r=r[:2]
        r[0]["quantity"]=r[1]["quantity"]="1"
        opening=s["raw_inventory"]["streams"][0]["opening"]
        opening.update(quantity="3",value="0.01")
        ext={**r[1],"id":"EXT","source_id":"EXT_DOC","at":"2026-11-02T10:00:00Z",
             "kind":"receipt","quantity":"1","receipt_value":"8.00"}
        ext.pop("transfer_id")
        req,out=evaluate(s,r+[ext])
        val=out["recognition"]["inventory_valuation"]
        self.assertFalse(req.entries)
        self.assertEqual(val["transfers"]["closed_pairs"][0]["amount"],"0.00")
        self.assertEqual(val["outputs"]["available_value"],"208.01")
        self.assertEqual(val["outputs"]["closing_value"],"208.01")
        self.assertEqual(val["streams"][1]["outputs"]["closing_quantity"],"12")

    def test_fail_closed_on_bad_pairs_and_fraudulent_value(self):
        for k,msg in (
            ("qty","Transfer quantity mismatch"),("duplicate","Duplicate or partial"),
            ("single","Both transfer legs must close"),("cross_cutoff","Cross-period/future"),
            ("wrong_kind","Transfer ID on unrelated"),
            ("pending_issue","Insufficient stock"),("source_repeat","reused across inventory scopes"),
            ("duplicate_event","reused across inventory scopes"),("pair_case","Conflicting transfer ID"),
            ("clock","Ambiguous combined inventory"),("replay","Replayed transfer ID"),
            ("prearrival","Transfer receipt precedes dispatch"),("other_item","scope mismatch"),
            ("other_unit","scope mismatch"),("other_book","scope mismatch"),
            ("other_currency","scope mismatch"),("same_warehouse","scope mismatch"),
            ("opening","Transfers need all openings"),
            ("fake_value","Wrong/missing operation-specific fields"),
            ("disabled","invalid movement headers"),("clock_policy","deterministic chronology"),
        ):
            with self.subTest(k=k), self.assertRaisesRegex(ValueError,msg):
                s,r=case(); opt=s["raw_inventory"]
                if k=="qty":r[1]["quantity"]="3"
                if k=="duplicate":r.append({**r[1],"id":"extra","source_id":"extra-doc","sequence":"2"})
                if k=="single":r.pop(1)
                if k=="cross_cutoff":r[1]["at"]="2026-12-01T00:00:00Z"
                if k=="wrong_kind":r[-1]["transfer_id"]="zzz"
                if k=="pending_issue":r[-1]["at"]="2026-11-04T16:00:00Z"
                if k=="source_repeat":r[1]["source_id"]=r[0]["source_id"]
                if k=="duplicate_event":r[1]["id"]=r[0]["id"]
                if k=="pair_case":r[1]["transfer_id"]="atob"
                if k=="clock":r[1]["at"]=r[0]["at"]
                if k=="replay":opt["transfer_history"]=["ATOB"]
                if k=="prearrival":r[1]["at"]="2026-11-01T09:00:00Z"
                if k in ("other_item","other_unit"):
                    key,value=("item","LABEL") if k=="other_item" else ("uom","KG")
                    opt["streams"][1]["scope"][key]=opt["streams"][1]["opening"][key]=value
                    for mv in r:
                        if mv["warehouse"]=="WH_B": mv[key]=value
                if k=="other_book":r[1]["book"]="different"
                if k=="other_currency":r[1]["currency"]="USD"
                if k=="same_warehouse":r[1]["warehouse"]=r[0]["warehouse"]
                if k=="opening":opt["streams"][1]["opening"]["as_of"]="2026-11-01T00:00:00Z"
                if k=="fake_value":r[1]["receipt_value"]="444.00"
                if k=="disabled":
                    opt.pop("transfer_policy");opt.pop("transfer_history")
                if k=="clock_policy":opt.pop("chronology")
                evaluate(s,r)

    def test_composite_other_accounts_and_cfg_evidence(self):
        s,r=two_accounts()
        z,m=case(); single=z["raw_inventory"]; book=s["raw_composite"]["scope"]["book"]
        single["context"]["book"]=book
        for st in single["streams"]: st["opening"]["book"]=book
        for mv in m: mv.update(book=book,input_name="stock",posting_identity=mv["id"])
        s["raw_composite"]["inputs"]["stock"]={"raw_inventory":single}
        total_rows=[row for row in r if row["input_name"]!="stock"]+list(reversed(m))
        _,o=evaluate(s,total_rows)
        self.assertEqual(D(o["totals"]["total_cost"]),D("1948.59"))
        ev=self.snapshot(s,total_rows,o)
        self.assertEqual(ev["raw_composite.inputs.stock.raw_inventory.transfer_history"]["mapping"]["configuration"],[])
        self.assertTrue(any("input_name=stock" in x["selector"] and x["status"]=="captured"
                            and x["field"]=="transfer_id" for x in ev.values()))
