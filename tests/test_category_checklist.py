from copy import deepcopy
from decimal import Decimal
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from cost_core.costing import CostingEngine
from costing_outputs import create_snapshot,load_snapshot,export_snapshot
from test_composite_adapter import evaluate,text
from test_object_rollups import example


def fixture():
    schema,raw=example()
    meter=next(row for row in raw if row["input_name"]=="power")
    scope={k:meter[k] for k in ("book","period","currency","billing_account","input_name")}
    raw.append(dict(scope,id="refund-test",kind="vendor_credit",amount="-1141.10",
                    evidence="refund evidence",posting_identity="refund-test"))
    def make(ident,state,*terms):
        row=dict(id=ident,name=ident,state=state,
                 matches=[dict(input_name=b,element_id=e) for b,e in terms])
        if state!="required":row["evidence"]="declared plugin basis, not verified"
        return row
    rows=[make("utilities","required",("power","metered_usage"),("power","vendor_credit")),
          make("labour","required",("period","labour")),
          make("audit-fee","declared_zero",("period","audit-fee")),
          make("disposal","not_applicable",("stock","disposal")),
          make("damages","not_applicable",("stock","shrink"))]
    schema["raw_composite"]["category_checklist"]={"version":1,"groups":{"product-worth":rows}}
    return schema,raw


class Checklist(unittest.TestCase):
    def test_actual_offset_zero_missing_na_declared_zero_conflict(self):
        s,r=fixture()
        plain=deepcopy(s)
        del plain["raw_composite"]["category_checklist"]
        prior=evaluate(plain,r)[1]
        request,out=evaluate(s,r)
        d=out["recognition"].pop("category_checklist")
        self.assertEqual(out,prior)
        self.assertNotIn("category_checklist",CostingEngine().analyze(request,cost_object="A")["recognition"])
        self.assertFalse(d["can_claim_complete"])
        a=d["groups"][0]
        self.assertEqual((a["group_total"],a["categorized_observed_amount"],a["uncategorized_amount"]),
                         ("1126.00","13.20","1112.80"))
        self.assertEqual(d["ungrouped_ledger_amount"],"30.00")
        self.assertEqual({(v["input_name"],v["element_id"]):Decimal(v["amount"])
                          for v in a["uncategorized_components"]},
                         {("period","rent-rule"):Decimal("90.00"), ("stock","cogs"):Decimal("369.60"),
                          ("water","metered_usage"):Decimal("653.20")})
        rows={x["id"]:(x["state"],x["amount"],x["component_count"]) for x in a["categories"]}
        self.assertEqual(rows,{"utilities":("observed","0.00",2),"labour":("missing",None,0),
            "audit-fee":("declared_zero","0.00",0),"disposal":("not_applicable",None,0),
            "damages":("conflict","13.20",1)})
        self.assertEqual(a["membership_sha256"],out["recognition"]["object_rollups"]["groups"][0]["membership_sha256"])

    def test_bad_config_rejected(self):
        for change,why in (("duplicate","overlapping selector"),("unknown","unknown branch"),
                           ("bad group","unknown or too many"),("no rollups","explicit final object"),
                           ("conflict","misplaced"),("missing evidence","missing assertion"),
                           ("id alias","aliased category"),("boolean version","bad version")):
            s,r=fixture(); v=s["raw_composite"]; c=v["category_checklist"]; rows=c["groups"]["product-worth"]
            if change=="duplicate":rows[1]["matches"][0]=rows[0]["matches"][0]
            if change=="unknown":rows[1]["matches"][0]["input_name"]="not-a-member"
            if change=="bad group":c["groups"]={"stranger":rows}
            if change=="no rollups":v.pop("object_rollups")
            if change=="conflict":rows[0]["evidence"]="unsupported"
            if change=="missing evidence":rows[2].pop("evidence")
            if change=="id alias":rows[1]["id"]="UTILITIES"
            if change=="boolean version":c["version"]=True
            with self.subTest(case=change),self.assertRaisesRegex(ValueError,why):
                evaluate(s,r)

    def test_snapshot_same_revision_original_evidence_and_immutable_schema(self):
        s,r=fixture()
        request,out=evaluate(s,r)
        expected=out["recognition"]["category_checklist"]
        modified=deepcopy(s)
        q=modified["raw_composite"]["category_checklist"]["groups"]["product-worth"]
        req= evaluate(modified,r)[0]
        q[0]["matches"].clear()
        q[0]["name"]="made up"
        self.assertEqual(CostingEngine().analyze(req)["recognition"]["category_checklist"],expected)
        with TemporaryDirectory() as d:
            root=Path(d)
            ds=root/"costing_datasets"/s["dataset_id"]
            ds.mkdir(parents=True)
            (ds/"data.csv").write_text(text(r),"utf-8")
            (ds/"manifest.json").write_text(json.dumps({"schema":s}),"utf-8")
            p=create_snapshot(out,root,root,dict(dataset_id=s["dataset_id"],period="2026-11",
                              scenario="actual",owner_external_id="synthetic-case"))
            original=load_snapshot(p["analysis_id"],root)
            self.assertEqual(p["revision"],original["revision"])
            j=json.loads(export_snapshot(original,"json",root/"exports").read_text())
            md=export_snapshot(original,"md",root/"exports").read_text()
            self.assertEqual(j["revision"],p["revision"])
            self.assertTrue(all(x in md for x in ("已观察","缺资料","声明为零","不适用","与账面冲突")))
            by_ref={(row["field"],row["selector"]):row for row in original["evidence"]}
            first=expected["groups"][0]["categories"][0]
            for ref in first["source_refs"]:
                e=by_ref[ref["field"],ref["selector"]]
                if e["field"]=="category_checklist":
                    self.assertEqual(e["status"],"captured_mapping")
                    self.assertEqual(e["mapping"]["configuration"],s["raw_composite"]["category_checklist"]["groups"]["product-worth"][0])
                else:
                    self.assertEqual(e["status"],"captured")

    def test_manifest_drift_rejected_before_snapshot_persistence(self):
        s,r=fixture()
        result=evaluate(s,r)[1]
        for what in ("assertion","selector","group membership","missing","equivalent formatting"):
            with self.subTest(what=what),TemporaryDirectory() as d:
                root=Path(d)
                ds=root/"costing_datasets"/s["dataset_id"]
                ds.mkdir(parents=True)
                (ds/"data.csv").write_text(text(r),"utf-8")
                modified=deepcopy(s)
                envelope=modified["raw_composite"]
                cells=envelope["category_checklist"]["groups"]["product-worth"]
                if what=="assertion": cells[2]["evidence"]="unrelated replacement after compute"
                elif what=="selector": cells[0],cells[2]=cells[2],cells[0]
                elif what=="group membership": envelope["object_rollups"][0]["members"][0]["object_id"]="B"
                elif what=="missing": cells.pop(2)
                (ds/"manifest.json").write_text(json.dumps({"schema":modified},sort_keys=True,indent=2),"utf-8")
                ctx=dict(dataset_id=s["dataset_id"],period="2026-11",scenario="actual",owner_external_id="synthetic-case")
                if what=="equivalent formatting":
                    snap=create_snapshot(result,root,root,ctx)
                    self.assertTrue(load_snapshot(snap["analysis_id"],root))
                    continue
                with self.assertRaisesRegex(ValueError,"成本类别清单"):
                    create_snapshot(result,root,root,ctx)
                self.assertFalse((root/"costing_snapshots").exists())
