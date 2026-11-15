import csv
from copy import deepcopy
from decimal import Decimal
import io
import unittest
from urllib.parse import parse_qsl

from cost_core.csv_adapter import CsvCostingAdapter
from cost_core.costing import CostingEngine
from test_inventory_adapter import fixture

def batch():
    data=fixture()
    op={k:data[k] for k in ("operation","policy","context")}
    op["context"]={k:v for k,v in op["context"].items() if k not in ("warehouse","item","uom")}
    op["streams"]=[]; batches=[]
    for i,(w,item,u,qty,value) in enumerate((("WH_A","SKU_1","EA","20","240.00"),
                                            ("WH_B","SKU_1","EA","30","900.00"),
                                            ("WH_A","SKU_2","BOX","12","120.00"))):
        scope=dict(warehouse=w,item=item,uom=u)
        opening={**data["opening"],**scope,"quantity":qty,"value":value}
        opts=dict(object_id=f"stock{i}",object_kind="product",cost_type="direct",behavior="variable")
        op["streams"].append(dict(scope=scope,opening=opening,posting_map={k:{**opts,"cost_element":k}
                                                                        for k in ("cogs","shrink")}))
        if i==0:
            ms=deepcopy(data["transactions"])
        else:
            ms=[{**data["transactions"][1],**scope,"id":f"sale{i}","source_id":f"doc-sale{i}",
                 "quantity":"4" if i==1 else "2"}]
        batches.append(ms)
    # Deliberately interleave scopes while preserving each stream's own chronology.
    return op,[batches[0][0],batches[1][0],batches[0][1],batches[2][0],*batches[0][2:]]

def ingest(op,ms):
    file=io.StringIO()
    wr=csv.DictWriter(file,fieldnames=sorted({k for d in ms for k in d}))
    wr.writeheader();wr.writerows(ms)
    return file.getvalue(),{"raw_inventory":op,"dataset_id":"csv-123456789abcdef0"}

def go(op,ms):
    text,schema=ingest(op,ms)
    req=CsvCostingAdapter().from_text(text,schema)
    return req,CostingEngine().analyze(req)

class Partition(unittest.TestCase):
    def test_exact_groups_no_lot_or_quantity_sharing(self):
        op,ms=batch();req,r=go(op,ms)
        inv=r["recognition"]["inventory_valuation"]
        assert "closing_quantity" not in inv["outputs"]
        self.assertEqual(inv["outputs"],dict(available_value="1680.00",net_cogs="509.60",shrink="13.20",
                                            closing_value="1157.20",reconciliation_difference="0.00"))
        exp=[("WH_A","SKU_1","21","277.20"),("WH_B","SKU_1","26","780.00"),("WH_A","SKU_2","10","100.00")]
        self.assertEqual([(g["context"]["warehouse"],g["context"]["item"],
                           g["outputs"]["closing_quantity"],g["outputs"]["closing_value"]) for g in inv["streams"]],exp)
        self.assertEqual(r["totals"]["total_cost"],"522.80")
        self.assertFalse(r["recognition"]["can_claim_complete"])
        self.assertTrue(r["ledger_reconciled"])
        by_object={o["object_id"]:Decimal(o["total_cost"]) for o in r["objects"]}
        self.assertEqual(by_object,{"stock0":Decimal("382.80"),"stock1":Decimal("120.00"),"stock2":Decimal("20.00")})
        refs=[(s,dict(parse_qsl(x.selector))["row"]) for s in req.entries for x in s.source_refs]
        self.assertIn("3",{n for _,n in refs})
        self.assertIn("5",{n for _,n in refs})

    def test_stream_specific_return_positive_and_negative(self):
        op,ms=batch()
        move={**ms[1],"kind":"sale_return","id":"my-ret","source_id":"doc-ret","quantity":"1",
              "original_issue_id":"sale1","restock_status":"restockable","at":"2026-11-05T12:00:00Z"}
        _,r=go(op,ms+[move]);w=r["recognition"]["inventory_valuation"]["streams"][1]
        self.assertEqual(w["outputs"]["net_cogs"],"90.00")
        for wrong in ("SALE_1","sale2"):
            with self.subTest(wrong=wrong),self.assertRaisesRegex(ValueError,"Undefined/forward"):
                go(op,ms+[{**move,"original_issue_id":wrong}])

    def test_global_replay_undeclared_uom_and_no_transfers(self):
        for mode in ("source","event","uom","transfer","target","historic","opening"):
            op,ms=batch()
            if mode=="source": ms[1]["source_id"]=ms[2]["source_id"]
            if mode=="event": ms[1]["id"]=ms[2]["id"]
            if mode=="uom": ms[1]["uom"]="BOX"
            if mode=="transfer": ms[1]["kind"]="transfer"
            if mode=="target": op["streams"][1]["posting_map"]["cogs"]["object_id"]="stock0"
            if mode=="historic": op["streams"][1]["opening"]["seen_source_ids"]=[ms[0]["source_id"]]
            if mode=="opening": op["streams"][1]["opening"]["warehouse"]="WH_A"
            with self.subTest(mode=mode),self.assertRaises(ValueError):
                go(op,ms)

    def test_no_activity_still_has_declared_opening(self):
        op,ms=batch()
        _,r=go(op,[m for m in ms if m["item"]!="SKU_2"])
        inv=r["recognition"]["inventory_valuation"]
        self.assertEqual(inv["streams"][2]["outputs"]["closing_value"],"120.00")
        self.assertEqual(inv["streams"][2]["outputs"]["closing_quantity"],"12")
        self.assertEqual(inv["streams"][2]["outputs"]["net_cogs"],"0.00")
        self.assertEqual(Decimal(next(x for x in r["objects"] if x["object_id"]=="stock2")["total_cost"]),0)

    def test_snapshot_captures_each_opening_and_no_quantity_total(self):
        from tempfile import TemporaryDirectory
        from pathlib import Path
        import json
        from costing_outputs import create_snapshot,load_snapshot,export_snapshot
        op,ms=batch()
        text,schema=ingest(op,ms)
        request=CsvCostingAdapter().from_text(text,schema)
        result=CostingEngine().analyze(request)
        with TemporaryDirectory() as t:
            root=Path(t)
            dataset=root/"costing_datasets"/schema["dataset_id"]
            dataset.mkdir(parents=True)
            (dataset/"data.csv").write_text(text)
            (dataset/"manifest.json").write_text(json.dumps({"schema":schema}))
            initial=create_snapshot(result,root,root,dict(dataset_id=schema["dataset_id"],dataset_label="分流",
                                         period=op["context"]["period"],scenario="actual",owner_external_id="qa"))
            snap=load_snapshot(initial["analysis_id"],root)
            self.assertEqual(snap["revision"],initial["revision"])
            openings=[e["mapping"]["configuration"] for e in snap["evidence"]
                      if e["selector"].startswith("raw_inventory.streams[")]
            self.assertEqual({(x["warehouse"],x["item"],x["value"]) for x in openings},
                             {("WH_A","SKU_1","240.00"),("WH_B","SKU_1","900.00"),("WH_A","SKU_2","120.00")})
            md=export_snapshot(snap,"md",root/"export").read_text()
            for phrase in ("只汇总同币金额，不相加库存数量","1,157.20","780.00","SKU_2"):
                self.assertIn(phrase,md)

if __name__=="__main__":
    unittest.main()
