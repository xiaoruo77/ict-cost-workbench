"""Explicit, independently billed metering branches in a single CNY submission."""
import copy
from decimal import Decimal as D
from pathlib import Path
from tempfile import TemporaryDirectory
from urllib.parse import parse_qs
import json
import unittest

from cost_core.csv_adapter import CsvCostingAdapter
from costing_outputs import create_snapshot,load_snapshot,export_snapshot
from test_composite_allocation import fixture
from test_composite_adapter import evaluate,text
from test_meter_adapter import config,line

def separate_meter(book, account, object_id, unit, until, rate_low, rate_hi):
    part=config(dict(version=1,currency="CNY",unit=unit,tier_scope="billing_account",usage_scale=3,
                     currency_scale=2,tiers=[dict(until=until,rate=rate_low),dict(until=None,rate=rate_hi)],
                     basis_months=0),account=account)
    part["scope"]["book"]=book
    for post in part["posting_map"].values(): post["object_id"]=object_id
    return part

def two_accounts():
    s,r=fixture()
    v=s["raw_composite"]
    v["version"]=3
    book=v["scope"]["book"]
    a=separate_meter(book,"PROVIDER_ELEC","ENERGY_E","kWh","1400","0.70","1.20")
    b=separate_meter(book,"PROVIDER_WATER","ENERGY_W","m³","500","1.05","1.60")
    v["inputs"]["power"]=dict(raw_meter=a)
    v["inputs"]["water"]=dict(raw_meter=b)
    def m(c,q,i,branch):
        return {**line(c,q,id=i,m="SHARED_LOCAL_METER_NAME"),"input_name":branch,"posting_identity":i}
    r[1:1]=[m(a,"732.5","power-a","power"),m(b,"580.125","water-a","water"),m(a,"801.75","power-b","power")]
    return s,r

class RepeatedMeterV3(unittest.TestCase):
    def test_two_accounts_are_separately_valued_and_evidenced(self):
        s,rows=two_accounts()
        _,out=evaluate(s,rows)
        # Independent tariff arithmetic: 1400×0.70 + 134.25×1.20;
        # 500×1.05 + 80.125×1.60. No quantity or pricing is pooled.
        power=D(1400)*D(".70")+D("134.25")*D("1.2")
        water=D(500)*D("1.05")+D("80.125")*D("1.60")
        self.assertEqual((power,water),(D("1141.10"),D("653.20")))
        self.assertEqual(D(out["totals"]["total_cost"]),D("2297.10"))
        rec=out["recognition"]
        self.assertEqual(rec["composite_version"],3)
        self.assertEqual({k:D(v) for k,v in rec["posted_pool_totals"].items()},
                         dict(stock=D("382.80"),period=D("120"),power=power,water=water))
        self.assertEqual({a["to_object"]:D(a["amount"]) for a in out["allocations"]},{"A":D(90),"B":D(30)})
        self.assertFalse(rec["can_claim_complete"])
        self.assertTrue(out["ledger_reconciled"])
        for name,qty,unit,account in (
            ("power","1534.25","kWh","PROVIDER_ELEC"),("water","580.125","m³","PROVIDER_WATER")):
            block=rec["inputs"][name]["meter_billing"]
            self.assertEqual(block["unit"],unit)
            self.assertEqual(block["scope"]["billing_account"],account)
            self.assertEqual(D(block["groups"][account]["quantity"]),D(qty))
        with TemporaryDirectory() as t:
            root=Path(t); ds=root/"costing_datasets"/s["dataset_id"]; ds.mkdir(parents=True)
            (ds/"data.csv").write_text(text(rows),encoding="utf-8")
            (ds/"manifest.json").write_text(json.dumps({"schema":s}),encoding="utf-8")
            snapshot=create_snapshot(out,root,root,dict(dataset_id=s["dataset_id"],dataset_label="Two providers",
                              period="2026-11",scenario="actual",owner_external_id="meter-v3-tests"))
            loaded=load_snapshot(snapshot["analysis_id"],root)
            self.assertEqual(snapshot["revision"],loaded["revision"])
            j=json.loads(export_snapshot(loaded,"json",root/"exports").read_bytes())
            self.assertEqual(j["revision"],snapshot["revision"])
            ev={(v["selector"]):v for v in loaded["evidence"]}
            for branch,unit in (("power","kWh"),("water","m³")):
                q=ev[f"raw_composite.inputs.{branch}.raw_meter.profile"]
                self.assertEqual(q["mapping"]["configuration"]["unit"],unit)
            original={(p["input_name"][0],p.get("id",[None])[0]):p["row"][0]
                      for v in loaded["evidence"] if v["status"]=="captured" and
                      "input_name" in (p:=parse_qs(v["selector"]))}
            self.assertEqual(original["power","power-a"],"3")
            self.assertEqual(original["water","water-a"],"4")
            self.assertEqual(original["power","power-b"],"5")
            md=export_snapshot(loaded,"md",root/"exports").read_text()
            for item in ("不同账户或单位的数量不合计","PROVIDER_ELEC","PROVIDER_WATER","kWh","m³","1,141.10","653.20"):
                self.assertIn(item,md)

    def test_old_versions_still_reject_repeated_modes(self):
        for old in (1,2):
            s,r=two_accounts(); v=s["raw_composite"]; v["version"]=old
            # Only three branches, so it reaches duplicate mode validation.
            v["inputs"].pop("period")
            r=[z for z in r if z["input_name"]!="period"]
            with self.subTest(old=old), self.assertRaisesRegex(ValueError,"mode repeated"):
                evaluate(s,r)

    def test_reject_unreadable_scope_or_overlap_and_keep_global_dedupe(self):
        cases=[
          ("account_alias","duplicate meter billing account"),
          ("actual_account","mixed book, currency, account"),
          ("account_hidden_zero_width","meter account contains control or formatting"),
          ("account_hidden_control","meter account contains control or formatting"),
          ("period_scope","meter branch scope mismatch"),
          ("book_scope","meter branch scope mismatch"),
          ("currency_scope","meter branch scope mismatch"),
          ("target_collision","disjoint object IDs"),
          ("native_source_collision","duplicate original source ID"),
          ("posting_collision","duplicate posting identity"),
          ("wrong_unit","mixed or unsupported measurement unit"),
          ("root_trust","undeclared options"),
          ("allocation_collision","rule must stay within branch"),
          ("repeated_inventory","mode repeated"),
          ("repeated_recognition","mode repeated"),
        ]
        for case,pattern in cases:
            s,rows=two_accounts(); v=s["raw_composite"]; branches=v["inputs"]
            water=branches["water"]["raw_meter"]
            w=next(z for z in rows if z["input_name"]=="water")
            p=next(z for z in rows if z["input_name"]=="power")
            if case=="account_alias": water["scope"]["billing_account"]="ＰＲＯＶＩＤＥＲ＿ＥＬＥＣ"
            if case=="account_hidden_zero_width": water["scope"]["billing_account"]="PROVIDER_\u200bELEC"
            if case=="account_hidden_control": water["scope"]["billing_account"]="PROVIDER_\u001fELEC"
            if case=="actual_account": w["billing_account"]=p["billing_account"]
            if case=="period_scope": water["scope"]["period"]="2026-12"
            if case=="book_scope": water["scope"]["book"]="different"
            if case=="currency_scope": water["scope"]["currency"]="USD"
            if case=="target_collision":
                for x in water["posting_map"].values(): x["object_id"]="ENERGY_E"
            if case=="native_source_collision": w["id"]=p["id"]
            if case=="posting_collision": w["posting_identity"]=p["posting_identity"]
            if case=="wrong_unit": w["unit"]="kWh"
            if case=="root_trust": s["trusted_authority_id"]="claimed"
            if case=="allocation_collision": branches["period"]["allocation_rules"][0]["target_ids"].append("ENERGY_E")
            if case.startswith("repeated_"):
                branches["extra"]=copy.deepcopy(branches["stock" if case=="repeated_inventory" else "period"])
            with self.subTest(case=case),self.assertRaisesRegex(ValueError,pattern):
                evaluate(s,rows)

    def test_branch_limits_allow_bounded_four_meters(self):
        s,rows=two_accounts(); v=s["raw_composite"]; scope=v["scope"]
        template=v["inputs"]["water"]
        def new(i):
            name=f"account{i}"
            c=copy.deepcopy(template)
            raw=c["raw_meter"]
            raw["scope"]["billing_account"]=name
            for p in raw["posting_map"].values(): p["object_id"]=name
            v["inputs"][name]=c
            rows.append({**line(raw,10,id=name),"input_name":name,"posting_identity":name})
        new(3); new(4)
        _,out=evaluate(s,rows)
        self.assertEqual(D(out["totals"]["total_cost"]),D("2318.10"))
        with self.assertRaisesRegex(ValueError,"too many meter branches"):
            no_stock=copy.deepcopy(s); no_stock["raw_composite"]["inputs"].pop("stock")
            v=no_stock["raw_composite"]; new(5)
            evaluate(no_stock,[r for r in rows if r["input_name"]!="stock"])
        v=s["raw_composite"]; new(5)
        with self.assertRaisesRegex(ValueError,"requires two to six named sources"):
            evaluate(s,rows)

    def test_meter_rows_and_band_expansion_are_bounded_aggregate(self):
        for samples,tiers,pattern in ((6001,1,"aggregate meter record limit"),(95,64,"aggregate meter band evidence limit")):
            s,rows=two_accounts(); branches=s["raw_composite"]["inputs"]
            rows=[r for r in rows if r["input_name"] not in ("power","water")]
            for branch in ("power","water"):
                raw=branches[branch]["raw_meter"]
                raw["profile"].update(tier_scope="meter",tiers=[dict(until=str(i),rate="1") for i in range(1,tiers)]
                                      +[dict(until=None,rate="2")])
                for i in range(samples):
                    sid=f"{branch}-{i}"
                    rows.append({**line(raw,1,id=sid,m=f"meter-{i}"),"input_name":branch,"posting_identity":sid})
            with self.subTest(samples=samples),self.assertRaisesRegex(ValueError,pattern):
                evaluate(s,rows)
