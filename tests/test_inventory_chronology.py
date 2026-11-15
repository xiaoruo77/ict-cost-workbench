"""Opt-in ordering is deterministic but never a replacement for source validation."""
import copy
import json
import unittest
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory
from urllib.parse import parse_qs

from cost_core.csv_adapter import CsvCostingAdapter
from cost_core.costing import CostingEngine
from cost_core.inventory_ingest import SORT_CHRONOLOGY
from costing_outputs import create_snapshot, load_snapshot, export_snapshot
from test_inventory_adapter import convert, fixture
from test_inventory_partition import batch, go
from test_composite_adapter import evaluate, text
from test_composite_meter_v3 import two_accounts


def analyze(raw, order=False):
    source,schema=convert(raw)
    if order:
        schema["raw_inventory"]["chronology"]=SORT_CHRONOLOGY
    req=CsvCostingAdapter().from_text(source,schema)
    return req,CostingEngine().analyze(req)


class InventoryChronology(unittest.TestCase):
    def test_default_unchanged_and_opt_in_reorder_with_original_refs(self):
        src=fixture()
        req,baseline=analyze(src)
        self.assertNotIn("chronology", baseline["recognition"]["inventory_valuation"])
        _,opts=analyze(src,True)
        self.assertEqual(opts["recognition"]["inventory_valuation"]["chronology"]
                         ["events_at_changed_stream_position"],0)
        src["transactions"].reverse()
        with self.assertRaisesRegex(ValueError,"Unordered or ambiguous"):
            analyze(src)
        req,got=analyze(src,True)
        result=got["recognition"]["inventory_valuation"]
        self.assertEqual(got["totals"],baseline["totals"])
        self.assertEqual(result["outputs"],baseline["recognition"]["inventory_valuation"]["outputs"])
        self.assertEqual(result["chronology"]["events_at_changed_stream_position"],4)
        ret=next(x for x in result["stock_movements"] if x["kind"]=="sale_return")
        self.assertEqual([parse_qs(x.selector)["row"][0] for x in req.entries[1].source_refs],["3","4"])
        self.assertEqual(parse_qs(ret["source_refs"][0]["selector"])["row"],["3"])
        self.assertFalse(got["recognition"]["can_claim_complete"])

    def test_per_stream_and_two_return_histories(self):
        op,rows=batch()
        op["streams"][1]["opening"]["seen_source_ids"]=["historic-one"]
        op["streams"][2]["opening"]["seen_source_ids"]=["historic-two"]
        _,base=go(op,rows)
        _,reordered=go(dict(op,chronology=SORT_CHRONOLOGY),list(reversed(rows)))
        self.assertEqual(reordered["totals"],base["totals"])
        # Single-stream period-crossing return still uses evidenced opening, not fictitious input row.
        historical=fixture("cross_month_return")
        req,result=analyze(historical,True)
        val=result["recognition"]["inventory_valuation"]
        self.assertEqual(len(req.entries[0].source_refs),1)
        self.assertLess(req.entries[0].amount,0)
        orig=val["cost_postings"][0]
        self.assertTrue(orig["original_source_id"])
        self.assertNotEqual(orig["original_issue_period"],val["context"]["period"])
        historical["opening"]["issues"][0].pop("evidence")
        with self.assertRaisesRegex(ValueError,"Missing required fields.*evidence"):
            analyze(historical,True)

    def test_two_prior_period_return_histories_keep_distinct_evidence(self):
        src=fixture("cross_month_return")
        dims=("warehouse","item","uom")
        context=src["context"]
        op=dict(operation=src["operation"],policy=src["policy"],
                context={k:v for k,v in context.items() if k not in dims},streams=[],
                chronology=SORT_CHRONOLOGY)
        per=[]
        for i,warehouse in enumerate(("WH_A","WH_B")):
            opening=copy.deepcopy(src["opening"])
            opening["warehouse"]=warehouse
            for name in ("seen_event_ids","seen_source_ids"):
                opening[name]=[f"{i}_{x}" for x in opening[name]]
            original=opening["issues"][0]
            original.update(warehouse=warehouse,id=f"{i}_{original['id']}",
                            source_id=f"{i}_{original['source_id']}")
            target=dict(object_id=f"inventory{i}",object_kind="product",cost_type="direct",behavior="variable")
            op["streams"].append(dict(scope={**{k:context[k] for k in dims},"warehouse":warehouse},
                                      opening=opening,posting_map={k:{**target,"cost_element":k}
                                                                  for k in ("cogs","shrink")}))
            moves=[{**x,"id":f"{i}_{x['id']}","source_id":f"{i}_{x['source_id']}","warehouse":warehouse}
                   for x in src["transactions"]]
            moves[1]["original_issue_id"]=original["id"]
            per.append(moves)
        received=[per[0][1],per[1][1],per[1][0],per[0][0]]
        req,r=go(op,received)
        inv=r["recognition"]["inventory_valuation"]
        self.assertEqual(inv["outputs"]["net_cogs"],"-79.20")
        self.assertEqual(inv["chronology"]["events_at_changed_stream_position"],4)
        self.assertEqual([x["cost_postings"][0]["original_source_id"] for x in inv["streams"]],
                         ["0_DOC_SALE_1:LINE_1","1_DOC_SALE_1:LINE_1"])
        self.assertEqual([parse_qs(x.source_refs[0].selector)["row"][0] for x in req.entries],["2","3"])
        self.assertTrue(all(len(x.source_refs)==1 for x in req.entries))
        received[0]["original_issue_id"]="1_SALE_1"
        with self.assertRaisesRegex(ValueError,"Undefined/forward"):
            go(op,received)

    def test_tie_cutoff_replay_and_forward_return_remain_invalid(self):
        src=fixture()
        a,b,*_=src["transactions"]
        b.update(at=a["at"],sequence=a["sequence"]+1)
        _,canonical=analyze(src)
        src["transactions"].reverse()
        self.assertEqual(analyze(src,True)[1]["totals"],canonical["totals"])
        for case,match in [
            ("tie","Unordered or ambiguous"),("cutoff","Cross-period"),
            ("replay","Duplicate/replayed"),("forward","Undefined/forward"),
            ("invalid_utc","Timestamp must be UTC"),
        ]:
            with self.subTest(case=case):
                stock=copy.deepcopy(src)
                ev=stock["transactions"]
                if case=="tie": ev[-2]["sequence"]=ev[-1]["sequence"]
                if case=="cutoff": ev[0]["at"]="2026-12-01T00:00:00Z"
                if case=="replay": ev[0]["source_id"]=ev[1]["source_id"]
                if case=="forward": ev[1]["at"]="2026-11-01T01:00:00Z"
                if case=="invalid_utc": ev[0]["at"]="2026-11-29T00:00:00+01:00"
                with self.assertRaisesRegex(ValueError,match):
                    analyze(stock,True)

    def test_cross_warehouse_return_and_history_collision_remain_invalid(self):
        op,rows=batch()
        op["chronology"]=SORT_CHRONOLOGY
        cross={**rows[1],"kind":"sale_return","id":"ret2","source_id":"doc-ret2","quantity":"1",
               "original_issue_id":"SALE_1","restock_status":"restockable","at":"2026-11-05T12:00:00Z"}
        with self.assertRaisesRegex(ValueError,"Undefined/forward"):
            go(op,[cross]+rows)
        op["streams"][1]["opening"]["seen_source_ids"]=[rows[0]["source_id"]]
        with self.assertRaisesRegex(ValueError,"reused across inventory scopes"):
            go(op,list(reversed(rows)))

    def snapshot(self, value,schema,result, expected_selector):
        with TemporaryDirectory() as name:
            root=Path(name); ds=root/"costing_datasets"/schema["dataset_id"]; ds.mkdir(parents=True)
            (ds/"data.csv").write_text(value,encoding="utf8")
            (ds/"manifest.json").write_text(json.dumps({"schema":schema}),encoding="utf8")
            original=create_snapshot(result,root,root,dict(dataset_id=schema["dataset_id"],dataset_label="order",
                             period="2026-11",scenario="actual",owner_external_id="chrono-qa"))
            saved=load_snapshot(original["analysis_id"],root)
            out=export_snapshot(saved,"md",root/"export").read_text()
            j=export_snapshot(saved,"json",root/"export").read_text()
            self.assertEqual(json.loads(j)["revision"], original["revision"])
            self.assertEqual(saved["revision"],original["revision"])
            self.assertIn("排序不修复原始凭证或账期错误",out)
            pol=next(e for e in saved["evidence"] if e["selector"]==expected_selector)
            self.assertEqual(pol["status"],"captured_mapping")
            self.assertEqual(pol["mapping"]["configuration"],SORT_CHRONOLOGY)
            return saved

    def test_standalone_policy_evidence_and_invalid_nonliteral_option(self):
        src=fixture(); src["transactions"].reverse()
        value,s=convert(src)
        for malformed in (True,None,{"sort":True},"sequence_only",""):
            with self.subTest(malformed=malformed), self.assertRaisesRegex(ValueError,"unsupported movement chronology"):
                CsvCostingAdapter().from_text(value,{**s,"raw_inventory":{**s["raw_inventory"],"chronology":malformed}})
        s["raw_inventory"]["chronology"]=SORT_CHRONOLOGY
        out=CostingEngine().analyze(CsvCostingAdapter().from_text(value,s))
        saved=self.snapshot(value,s,out,"raw_inventory.chronology")
        evidence={parse_qs(e["selector"]).get("source_id",[""])[0]:e for e in saved["evidence"] if e["status"]=="captured"}
        self.assertEqual(evidence["DOC_SHRINK_1:LINE_1"]["rows"][0]["record"],2)

    def test_composite_reorders_only_inventory_and_preserves_original_record_refs(self):
        s,rows=two_accounts()
        _,baseline=evaluate(s,rows)
        rows.reverse()
        with self.assertRaisesRegex(ValueError,"Unordered or ambiguous"):
            evaluate(s,rows)
        s["raw_composite"]["inputs"]["stock"]["raw_inventory"]["chronology"]=SORT_CHRONOLOGY
        req,result=evaluate(s,rows)
        self.assertEqual(result["totals"],baseline["totals"])
        cash={a["to_object"]:Decimal(a["amount"]) for a in result["allocations"]}
        self.assertEqual(cash,{"A":Decimal("90.00"),"B":Decimal("30.00")})
        physical={r["source_id"]:n for n,r in enumerate(rows,2) if r["input_name"]=="stock"}
        saved=self.snapshot(text(rows),s,result,"raw_composite.inputs.stock.raw_inventory.chronology")
        originals=[e for e in saved["evidence"] if "input_name=stock" in e["selector"] and e["status"]=="captured"]
        for e in originals:
            sel=parse_qs(e["selector"])
            self.assertEqual(e["rows"][0]["record"],physical[sel["source_id"][0]])
        self.assertEqual({parse_qs(e["selector"])["source_id"][0] for e in originals},set(physical))
