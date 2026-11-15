from decimal import Decimal as D
from pathlib import Path
from tempfile import TemporaryDirectory
import json
import unittest

from costing_outputs import create_snapshot,load_snapshot
from test_composite_adapter import setup,evaluate,text
from test_adapter_recognition import convert


def fixture():
    schema,rows=setup()
    scope=schema["raw_composite"]["scope"]
    inv=schema["raw_composite"]["inputs"]["stock"]
    rows=[m for m in rows if m["input_name"]=="stock"]
    raw={"name":"shared rent","context":scope,"excluded_sources":{},
         "sources":[{**scope,"id":"rent","kind":"expense","status":"data","amount":"120.00",
                     "economic_key":"rent","evidence":"lease"}],
         "rules":[{"id":"rent-rule","op":"recognize","source":"rent","posting_pool":"period"}],
         "outputs":["rent-rule"]}
    _,config=convert(raw,pool=True)
    config["raw_recognition"]["target_objects"][0].update(quantity="50",quantity_unit="m2")
    schema["raw_composite"].update(version=2,inputs={"stock":inv,"period":{
         "raw_recognition":config["raw_recognition"],"allocation_rules":config["allocation_rules"]}})
    rows.insert(1,{**raw["sources"][0],"input_name":"period","posting_identity":"expense-rent"})
    return schema,rows


class AllocatedComposite(unittest.TestCase):
    def test_exact_independent_pool_and_inventory(self):
        req,result=evaluate(*fixture())
        by_object={x["object_id"]:D(x["total_cost"]) for x in result["objects"]}
        self.assertEqual(by_object["A"],D("90.00"))
        self.assertEqual(by_object["B"],D("30.00"))
        self.assertEqual(by_object["SKU_1"],D("382.80"))
        self.assertEqual(D(result["totals"]["total_cost"]),D("502.80"))
        self.assertEqual(D(result["totals"]["unallocated_cost"]),0)
        self.assertEqual([p.pool_id for p in req.pools],["period:rent-rule"])
        self.assertEqual([d.driver_id for d in req.drivers],["period:csv-driver-1"])
        self.assertFalse(result["recognition"]["can_claim_complete"])

    def test_positive_driver_rounding_to_zero_keeps_decision_evidence(self):
        s,rows=fixture()
        rows[1]["amount"]="0.01"
        s["raw_composite"]["inputs"]["period"]["allocation_rules"][0]["weights"]={"A":1,"B":2}
        _,result=evaluate(s,rows)
        events={item["to_object"]:item for item in result["allocations"]}
        self.assertEqual(D(events["A"]["amount"]),0)
        self.assertEqual(events["A"]["components"],[])
        self.assertEqual(D(events["B"]["amount"]),D("0.01"))
        self.assertEqual(D(result["totals"]["total_cost"]),D("382.81"))
        self.assertEqual(D(result["totals"]["unallocated_cost"]),0)
        self.assertTrue(any("id=rent&input_name=period" in ref["selector"]
                            for ref in events["A"]["source_refs"]))
        self.assertTrue(any("allocation_rules[0]" in ref["selector"]
                            for ref in events["A"]["source_refs"]))

    def test_policy_and_target_rejects_fail_closed(self):
        for violation in ("stock_target","same_id","stage","no_rule","v1"):
            with self.subTest(violation=violation),self.assertRaises(ValueError):
                s,r=fixture()
                combined=s["raw_composite"]
                group=combined["inputs"]["period"]
                if violation=="stock_target": group["allocation_rules"][0]["target_ids"].append("SKU_1")
                if violation=="same_id": group["raw_recognition"]["target_objects"][0]["object_id"]="SKU_1"
                if violation=="stage": group["allocation_rules"][0]["stage"]=2
                if violation=="no_rule": group["allocation_rules"]=[]
                if violation=="v1": combined["version"]=1
                evaluate(s,r)

    def test_zero_driver_does_not_fake_successful_allocation(self):
        s,r=fixture()
        s["raw_composite"]["inputs"]["period"]["allocation_rules"][0]["weights"]={"A":0,"B":0}
        with self.assertRaisesRegex(ValueError,"Allocation driver total is zero for pool period:rent-rule"):
            evaluate(s,r)

    def test_captured_declared_quantity_and_policy_are_schema(self):
        s,rows=fixture()
        _,result=evaluate(s,rows)
        with TemporaryDirectory() as t:
            root=Path(t);d=root/"costing_datasets"/s["dataset_id"];d.mkdir(parents=True)
            (d/"data.csv").write_text(text(rows));(d/"manifest.json").write_text(json.dumps({"schema":s}))
            a=create_snapshot(result,root,root,dict(dataset_id=s["dataset_id"],dataset_label="fixture",
                              period="2026-11",scenario="actual",owner_external_id="qa"))
            b=load_snapshot(a["analysis_id"],root)
            self.assertEqual(a["revision"],b["revision"])
            refs={e["selector"]:e for e in b["evidence"]}
            qty=refs["raw_composite.inputs.period.raw_recognition.target_objects[0].quantity"]
            weights=refs["raw_composite.inputs.period.allocation_rules[0]"]
            self.assertEqual(qty["mapping"]["configuration"]["quantity"],"50")
            self.assertEqual(weights["mapping"]["configuration"]["weights"],{"A":3,"B":1})


if __name__=="__main__": unittest.main()
