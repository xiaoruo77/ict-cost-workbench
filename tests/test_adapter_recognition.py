import copy
import csv
import io
import json
from pathlib import Path
from decimal import Decimal as D
from urllib.parse import parse_qsl
import unittest
from cost_core.csv_adapter import CsvCostingAdapter
from cost_core.costing import CostingEngine

REF = Path(__file__).resolve().parents[1] / "fixtures" / "recognition"
RULE_POST = {"recognize","monthly_straight_line"}

def convert(doc, pool=False):
    cfg = {k:copy.deepcopy(v) for k,v in doc.items() if k!="sources"}
    specs = {r["id"]:dict(object_id="site_"+r["posting_pool"],object_kind="cost_center" if pool else "service",
                          record_type="pool" if pool else "entry",cost_element=r["id"],behavior="fixed",cost_type="indirect",
                          **({"pool_id":r["id"]} if pool else {})) for r in cfg["rules"] if r["op"] in RULE_POST}
    cfg.update(version=1,ledger_basis="recognized_expenses_incurred",posting_map=specs)
    f=io.StringIO(); names=list(doc["sources"][0]); names=sorted(set(names)|{k for x in doc["sources"] for k in x})
    w=csv.DictWriter(f,fieldnames=names);w.writeheader();w.writerows(doc["sources"])
    s=dict(period=cfg["context"]["period"],currency=cfg["context"]["currency"],raw_recognition=cfg)
    if pool:
        cfg["target_objects"]=[dict(object_id=o,object_kind="service") for o in ("A","B")]
        s["allocation_rules"]=[dict(pool_id=x,target_ids=["A","B"],weights={"A":3,"B":1}) for x in specs]
    return f.getvalue(),s

def fixture(name="manufacturing"):
    return json.loads((REF/(name+".json")).read_bytes())

def calculate(doc,pool=False):
    text, schema=convert(doc,pool)
    req=CsvCostingAdapter().from_text(text,schema)
    return req,CostingEngine().analyze(req)

class Integrated(unittest.TestCase):
    def test_manufacturing_recognition_is_not_reposted(self):
        req,out=calculate(fixture());r=out["recognition"]
        self.assertEqual(len(req.entries),7)
        self.assertEqual({k:D(v) for k,v in r["posted_pool_totals"].items()}, {"production":1900,"period":250})
        self.assertEqual({k:D(r["outputs"][k]) for k in ("production_cost","completed_goods_cost","cost_of_sales","pnl_cost")},
                         dict(production_cost=1900,completed_goods_cost=1700,cost_of_sales=1500,pnl_cost=1750))
        self.assertEqual(D(out["totals"]["total_cost"]),2150)
        self.assertEqual(r["ledger_basis"],"recognized_expenses_incurred")
        self.assertEqual({x["source_id"] for x in r["memo_sources"]},{"machine_cash","rent_cash","wages_cash"})
        self.assertTrue(all(x["treatment"]=="non_posting_overlapping_view" for x in r["derived_views"].values()))
        q=next(e for e in req.entries if e.element_id=="depreciation")
        s=dict(parse_qsl(q.source_refs[0].selector))
        self.assertEqual(s["id"],"machine_basis")
        sources=fixture()["sources"]
        self.assertEqual(sources[int(s["row"])-2]["id"],"machine_basis")
        self.assertTrue(out["ledger_reconciled"])

    def test_compiled_raw_postings_use_existing_allocation(self):
        q,r=calculate(fixture(),True)
        self.assertEqual(len(q.pools),7)
        tot={z["object_id"]:D(z["total_cost"]) for z in r["objects"]}
        self.assertEqual((tot["A"],tot["B"]),(D("1612.50"),D("537.50")))
        self.assertEqual(D(r["totals"]["unallocated_cost"]),0)
        self.assertTrue(all(all(x["source_refs"] for x in a["components"]) for a in r["allocations"]))

    def test_new_saas_same_typed_interface(self):
        req,p=calculate(fixture("saas"))
        self.assertEqual(D(p["totals"]["total_cost"]),sum(e.amount for e in req.entries))
        self.assertIn("recognition",p);self.assertEqual(p["recognition"]["status"],"valid")
        self.assertTrue(p["ledger_reconciled"])

    def test_unchanged_legacy_path(self):
        raw="object_id,currency,cost_element,amount\nsite,CNY,materials,1000\nsite,CNY,wages,600\n"
        a=CostingEngine().analyze(CsvCostingAdapter().from_text(raw,dict(period="2026-11")))
        self.assertEqual(D(a["totals"]["total_cost"]),1600)
        self.assertNotIn("recognition",a)

    def test_reserved_transformations_and_ambiguous_scopes_fail(self):
        template=fixture()
        for case in ("duplicate","cross_book","cross_month","unknown","missing","no_map","view_as_posting","mismatched_outer"):
            with self.subTest(case=case),self.assertRaises(ValueError):
                a=copy.deepcopy(template)
                if case=="duplicate":a["sources"].append(dict(a["sources"][0]))
                elif case=="cross_book":a["sources"][0]["book"]="OTHER"
                elif case=="cross_month":a["sources"][0]["period"]="2026-12"
                elif case=="unknown": a["rules"][0]["op"]="exec"
                elif case=="missing":a["sources"][0].update(status="missing",amount=None)
                text,s=convert(a)
                if case=="no_map":s["raw_recognition"]["posting_map"].pop("materials")
                if case=="view_as_posting":s["raw_recognition"]["posting_map"]["pnl_cost"]=dict(s["raw_recognition"]["posting_map"]["materials"])
                if case=="mismatched_outer":s["period"]="2026-12"
                CsvCostingAdapter().from_text(text,s)

    def test_load_repeats_against_original_bytes(self):
        # Same public adapter function is used by private upload preflight and
        # CsvCostingPlugin reload. No generated ledger is inserted into storage.
        text,s=convert(fixture())
        options=dict(source_uri="upload://data.csv",schema_uri="upload://schema")
        a=CsvCostingAdapter().from_text(text,s,**options)
        b=CsvCostingAdapter().from_text(text,json.loads(json.dumps(s)),**options)
        self.assertEqual(a,b)

    def test_real_snapshot_captures_original_rows_and_exports_bridge(self):
        import tempfile
        from costing_outputs import create_snapshot,load_snapshot,export_snapshot,resolve_evidence
        csv_text,s=convert(fixture())
        d="csv-123456789abcdef0";s["dataset_id"]=d
        result=CostingEngine().analyze(CsvCostingAdapter().from_text(csv_text,s))
        with tempfile.TemporaryDirectory() as t:
            root=Path(t);ds=root/"costing_datasets"/d;ds.mkdir(parents=True)
            (ds/"data.csv").write_text(csv_text,encoding="utf8")
            (ds/"manifest.json").write_text(json.dumps({"schema":s}),encoding="utf8")
            cfg=dict(dataset_id=d,dataset_label="fixture",period=s["period"],scenario="actual",owner_external_id="synthetic")
            snap=create_snapshot(result,root,root,cfg)
            reread=load_snapshot(snap["analysis_id"],root)
            self.assertEqual(snap,reread)
            md=export_snapshot(reread,"md",root/"export").read_text()
            for w in ("成本确认与期间结转","2,150.00","1,750.00","完工成本","桥接展示，不再次入账"):
                self.assertIn(w,md)
            j=json.loads(export_snapshot(reread,"json",root/"export").read_bytes())
            self.assertEqual(j["revision"],snap["revision"])
            ev=next(x for x in snap["evidence"] if "machine_basis" in x["selector"])
            self.assertEqual(ev["rows"][0]["values"]["id"],"machine_basis")
            self.assertEqual(ev["rows"][0]["value"],"2400")
            (ds/"data.csv").write_text(csv_text.replace("2400","2401"),encoding="utf8")
            output=resolve_evidence(reread,ev["evidence_id"],root,root)
            self.assertFalse(output["current_source_matches"])
            self.assertEqual(output["rows"][0]["value"],"2400")

    def test_template_reused_across_changed_source_ids_rows_and_costs(self):
        old=fixture(); _,v2=convert(old)
        opt=v2["raw_recognition"]; opt["version"]=2
        kinds={r["id"]:r["kind"] for r in old["sources"]}
        for x in opt["rules"]:
            if x["op"] in RULE_POST:
                key=x.pop("source")
                x.update(source_tag=key,source_kind=kinds[key])
            if x["op"]=="inventory_bridge":
                x["opening_tag"]=x.pop("opening");x["closing_tag"]=x.pop("closing")
        opt["excluded_tags"]=opt.pop("excluded_sources")
        freeze=json.dumps(v2,sort_keys=True,ensure_ascii=False)
        r=old["sources"]
        def raw(rows):
            f=io.StringIO();w=csv.DictWriter(f,fieldnames=sorted({k for x in rows for k in x}))
            w.writeheader();w.writerows(rows);return f.getvalue()
        rows=[dict(v,tag=v["id"]) for v in r]
        def engine(rows):
            a=CostingEngine().analyze(CsvCostingAdapter().from_text(raw(rows),v2))
            self.assertEqual(json.dumps(v2,sort_keys=True,ensure_ascii=False),freeze)
            return a
        a=engine(rows)
        rows2=[dict(v,id=f"source-UTC-{i:02}") for i,v in enumerate(reversed(rows))]
        b=engine(rows2)
        self.assertEqual(a["recognition"]["outputs"],b["recognition"]["outputs"])
        self.assertEqual(D(a["totals"]["total_cost"]),D(b["totals"]["total_cost"]))
        self.assertEqual(a["recognition"]["profile_version"],2)
        row=next(v for v in rows2 if v["tag"]=="materials_used")
        row2=dict(row,id="another-consumption",economic_key="mat-receipt-another",amount="100")
        c=engine([*rows2,row2])
        self.assertEqual(D(c["recognition"]["outputs"]["production_cost"]),2000)
        self.assertEqual(D(c["recognition"]["outputs"]["pnl_cost"]),1850)
        self.assertEqual(D(c["totals"]["total_cost"]),2250)
        other=next(v for v in rows2 if v["tag"]=="wip_open")
        with self.assertRaisesRegex(ValueError,"one opening_balance"):
            engine([*rows2,dict(other,id="bad-anchor",economic_key="bad-anchor")])
        with self.assertRaises(ValueError):
            engine([dict(v,kind="cash_flow") if v["tag"]=="materials_used" else v for v in rows2])

    def test_unposted_positive_and_negative_never_net_to_complete(self):
        doc=fixture()
        for n,kind,amount in (("env","expense","100"),("rebate","credit","-100")):
            doc["sources"].append(dict(doc["sources"][0],id=n,kind=kind,economic_key=n,amount=amount))
            doc["excluded_sources"][n]="missing policy"
        _,r=calculate(doc)
        self.assertTrue(r["ledger_reconciled"])
        self.assertFalse(r["recognition"]["can_claim_complete"])
        self.assertEqual(r["recognition"]["source_recognition_status"],"partial")
        self.assertEqual({x["source_id"] for x in r["recognition"]["unrecognized_nonzero_sources"]},{"env","rebate"})

    def test_same_template_with_many_pool_sources_and_changed_batch_period(self):
        import random
        ctx={"book":"D","period":"from_schema","currency":"CNY"}
        opt=dict(version=2,name="shared seat pool",context=ctx,rules=[
             dict(id="leases",op="recognize",source_tag="license",source_kind="expense",posting_pool="shared"),
             ],outputs=["leases"],excluded_tags={},ledger_basis="recognized_expenses_incurred",
             posting_map=dict(leases=dict(record_type="pool",pool_id="pool",object_id="team",object_kind="cost_center",
                                         cost_element="license",behavior="fixed",cost_type="indirect")),
             target_objects=[dict(object_id=k,object_kind="service") for k in ("A","B")])
        template=json.dumps(opt,sort_keys=True)
        for period,items in (("2026-10",[("G1","100"),("G2","200")]),("2026-11",[("Q3","300"),("Q1","20"),("Q2","40")])):
            with self.subTest(period=period):
                rows=[dict(id=i,book="D",period=period,currency="CNY",kind="expense",status="data",
                            amount=x,economic_key="K"+i,evidence="synthetic",tag="license") for i,x in items]
                random.Random(46).shuffle(rows)
                f=io.StringIO();w=csv.DictWriter(f,fieldnames=list(rows[0]));w.writeheader();w.writerows(rows)
                cfg=dict(raw_recognition=opt,period=period,allocation_rules=[dict(pool_id="pool",target_ids=["A","B"],weights={"A":3,"B":1})])
                a=CsvCostingAdapter().from_text(f.getvalue(),cfg)
                r=CostingEngine().analyze(a)
                v=sum(D(x) for _,x in items)
                self.assertEqual(len(a.pools),1)
                self.assertEqual(a.pools[0].amount,v)
                self.assertEqual(len(a.pools[0].source_refs),len(rows))
                self.assertEqual({x["to_object"]:D(x["amount"]) for x in r["allocations"]},{"A":v*D(".75"),"B":v*D(".25")})
                self.assertEqual(json.dumps(opt,sort_keys=True),template)

    def test_v2_dynamic_contract_objects_and_allocation_status(self):
        ctx={"book":"D","period":"from_schema","currency":"CNY"}
        opt=dict(version=2,name="dynamic objects",context=ctx,rules=[
             dict(id="work",op="recognize",source_tag="deliver",source_kind="expense",posting_pool="contract"),
             ],outputs=["work"],excluded_tags={},ledger_basis="recognized_expenses_incurred",
             posting_map=dict(work=dict(record_type="entry",object_id_from="target_object_id",object_kind="service",
                                         cost_element="labor",behavior="fixed",cost_type="direct")))
        raw=io.StringIO(); rows=[dict(id=i,book="D",period="2026-10",currency="CNY",kind="expense",status="data",
                            amount=x,economic_key="key/"+i,evidence="synthetic",tag="deliver",target_object_id=obj)
               for i,x,obj in (("W1","90","agree-882"),("W2","10","agree-882"),("W3","75","agree-123"))]
        w=csv.DictWriter(raw,fieldnames=list(rows[0]));w.writeheader();w.writerows(rows)
        req=CsvCostingAdapter().from_text(raw.getvalue(),dict(raw_recognition=opt,period="2026-10"))
        out=CostingEngine().analyze(req)
        self.assertEqual({v["object_id"]:D(v["total_cost"]) for v in out["objects"]},{"agree-882":100,"agree-123":75})
        self.assertEqual(out["recognition"]["allocation_status"],"not_required")
        a,s=convert(fixture(),True)
        del s["allocation_rules"][0]
        an=CostingEngine().analyze(CsvCostingAdapter().from_text(a,s))
        self.assertEqual(an["recognition"]["allocation_status"],"needs_review")
        self.assertTrue(an["ledger_reconciled"])
        self.assertFalse(an["recognition"]["can_claim_complete"])
