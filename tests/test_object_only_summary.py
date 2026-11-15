"""Opt-in minimal projection never reuses a mixed-scope snapshot as output."""
import copy
from decimal import Decimal
from hashlib import sha256
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from cost_core.costing import CostingEngine
from cost_core.csv_adapter import CsvCostingAdapter
from costing_object_summary import ObjectSummaryError, project_object_only, verify_object_only_summary
from costing_outputs import create_snapshot, export_snapshot, load_snapshot
from test_composite_adapter import text
from test_object_budgets import budget_example

def digest(value):
    body=dict(value); body.pop("revision",None)
    return sha256(json.dumps(body,ensure_ascii=False,sort_keys=True,separators=(",",":")).encode()).hexdigest()


class ObjectOnlySummary(unittest.TestCase):
    def setUp(self):
        self.temp=TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.workspace=Path(self.temp.name)
        self.owner="summary-qa-owner-sensitive"
        self.mixed="marker_UNSELECTED_branch_evidence"
        self.attachment="marker_HOSTILE_selected_metadata"
        self.s,self.rows=budget_example()
        self.target="ENERGY_E"
        power=next(row for row in self.rows if row["input_name"]=="power")
        water=next(row for row in self.rows if row["input_name"]=="water")
        water["evidence"]=self.mixed
        a={k:power[k] for k in ("book","period","currency","billing_account","input_name")}
        self.rows.append(dict(a,id="reverse-charge",kind="vendor_credit",amount="-1200.00",
                              evidence="marker_PRIVATE_selected_raw_evidence",posting_identity="rebate-net"))
        ds=self.workspace/"costing_datasets"/self.s["dataset_id"]; ds.mkdir(parents=True)
        (ds/"data.csv").write_text(text(self.rows))
        (ds/"manifest.json").write_text(json.dumps({"schema":self.s}))

    def snapshot(self, *, selected="", mutate=None, context_edit=None):
        request=CsvCostingAdapter().from_text(text(self.rows),self.s)
        result=CostingEngine().analyze(request,cost_object=selected)
        for row in result["objects"]:
            if row["object_id"]==self.target:
                row["client_attachment"]={"unrelated_notice":self.attachment}
        ctx=dict(dataset_id=self.s["dataset_id"],period="2026-11",scenario="actual",
                 owner_external_id=self.owner,dataset_label="marker_PRIVATE_dataset_label")
        if selected:ctx["cost_object"]=selected
        if mutate is not None:mutate(result)
        if context_edit is not None:context_edit(ctx)
        return create_snapshot(result,self.workspace,self.workspace,ctx)

    def project(self, s, **kwargs):
        args=dict(expected_revision=s["revision"],expected_workspace_owner=self.owner,object_id=self.target)
        args.update(kwargs)
        return project_object_only(s["analysis_id"],self.workspace,**args)

    def test_real_full_and_filtered_sources_yield_same_only_projection(self):
        full=self.snapshot()
        mixed=self.snapshot(selected=self.target)
        before={f:f.read_bytes() for f in self.workspace.rglob("*") if f.is_file()}
        projection=self.project(full)
        self.assertEqual(projection,self.project(mixed))
        self.assertEqual(projection,dct:=dict(
            schema_version="costing-object-only-summary/1",summary_scope="object_only_v1",
            basis="stored_final_object_metrics_no_source_dossier",period="2026-11",
            currency="CNY",scenario="actual",object=dict(
                object_id="ENERGY_E",name="ENERGY_E",kind="service",quantity=None,quantity_unit="unit",
                total_cost="-58.90",unit_cost=None,revenue=None,revenue_status="missing",
                operating_margin=None),revision=projection["revision"]))
        self.assertEqual(projection["revision"],digest(dct))
        verify_object_only_summary(projection)
        raw=json.dumps(projection,ensure_ascii=False)
        for forbidden in (self.mixed,self.attachment,self.owner,full["analysis_id"],full["revision"],
                          self.s["dataset_id"],"marker_PRIVATE_selected_raw_evidence","marker_PRIVATE_dataset_label",
                          "PROVIDER_ELEC","PROVIDER_WATER","source_refs","evidence","rollups","budgets",
                          "recognition","coverage","routes"):
            self.assertNotIn(forbidden,raw)
        self.assertEqual(before,{f:f.read_bytes() for f in self.workspace.rglob("*") if f.is_file()})
        self.assertEqual(full,load_snapshot(full["analysis_id"],self.workspace))
        self.assertEqual(mixed,load_snapshot(mixed["analysis_id"],self.workspace))

    def test_default_exports_still_private_mixed_scope_exact_bytes(self):
        s=self.snapshot(selected=self.target)
        before=export_snapshot(s,"json",self.workspace/"old-before").read_bytes()
        _=self.project(s)
        old=load_snapshot(s["analysis_id"],self.workspace)
        after=export_snapshot(old,"json",self.workspace/"old-after").read_bytes()
        self.assertEqual(before,after)
        self.assertIn(self.mixed.encode(),after)
        self.assertIn("PROVIDER_WATER",old["report"]["markdown"])
        self.assertEqual(self.project(s,object_id="ENERGY_E")["object"]["total_cost"],"-58.90")

    def test_denies_wrong_revision_owner_unrelated_object_and_workspace(self):
        s=self.snapshot(selected=self.target)
        errors=[]
        for kwargs in ({"expected_revision":"c"*64},{"expected_workspace_owner":"other"},
                       {"object_id":"A"},{"object_id":{"match":"ENERGY_E"}}):
            with self.subTest(kwargs=kwargs),self.assertRaises(ObjectSummaryError) as err:
                self.project(s,**kwargs)
            errors.append(str(err.exception))
        with TemporaryDirectory() as different:
            root=Path(different)
            with self.assertRaises(ObjectSummaryError) as err:
                project_object_only(s["analysis_id"],root,expected_revision=s["revision"],
                                    expected_workspace_owner=self.owner,object_id=self.target)
            errors.append(str(err.exception))
            self.assertEqual(list(root.iterdir()),[])
        self.assertEqual(len(set(errors)),1)

    def test_raw_snapshot_tamper_and_improper_selected_values_are_not_projected(self):
        # Marks in ignored fields are omitted; nested payloads in promised metrics are refused.
        for val in ({"B":100},"NaN","1.6e4",True,[], "1"*161):
            with self.subTest(val=val):
                def bad(res):
                    next(row for row in res["objects"] if row["object_id"]==self.target)["total_cost"]=val
                s=self.snapshot(mutate=bad)
                with self.assertRaises(ObjectSummaryError):self.project(s)
        s=self.snapshot()
        p=self.workspace/"costing_snapshots"/(s["analysis_id"]+".json")
        data=json.loads(p.read_text())
        data["result"]["objects"][0]["name"]="changed without revision"
        p.write_text(json.dumps(data))
        with self.assertRaises(ObjectSummaryError):self.project(s)

    def test_strict_output_validation_checks_unknown_keys_revision_and_numbers(self):
        receipt=self.project(self.snapshot())
        for mutate in ("amount","nested","extra","link","unavailable_revenue","bad_control_unit"):
            bad=copy.deepcopy(receipt)
            if mutate=="amount":bad["object"]["total_cost"]="-58.91"
            if mutate=="nested":bad["object"]["total_cost"]={"amount":5}
            if mutate=="extra":bad["object"]["not_whitelisted"]="foreign"
            if mutate=="link":bad["source_snapshot_id"]="foreign"
            if mutate=="unavailable_revenue":bad["object"]["revenue"]="0.00"
            if mutate=="bad_control_unit":bad["object"]["quantity_unit"]="kWh\u200b"
            if mutate!="amount":bad["revision"]=digest(bad)
            with self.subTest(mutate=mutate),self.assertRaises(ObjectSummaryError):
                verify_object_only_summary(bad)

    def test_positive_denominator_and_null_revenue_are_preserved_only_for_selected(self):
        s=self.snapshot()
        got=self.project(s,object_id="A")["object"]
        self.assertEqual((Decimal(got["total_cost"]),Decimal(got["quantity"]),Decimal(got["unit_cost"])),
                         (Decimal(90),Decimal(50),Decimal("1.8")))
        self.assertEqual(got["quantity_unit"],"m2")
        self.assertIsNone(got["revenue"])
        zero=self.project(s,object_id="site_period")["object"]
        self.assertEqual(Decimal(zero["total_cost"]),0)
        self.assertIsNone(zero["unit_cost"])
        self.assertEqual(zero["revenue_status"],"not_applicable")

    def test_actual_high_precision_and_complete_revenue_projected_without_selling_price(self):
        sid="csv-0123456789abcdef"
        raw=("object_id,currency,cost_element,amount,quantity,selling_price,quantity_unit\n"
             "unit-alpha,CNY,materials,1.00000001,3,2.50,kg\n")
        schema=dict(period="2026-11",dataset_id=sid)
        ws=self.workspace/"costing_datasets"/sid; ws.mkdir(parents=True)
        (ws/"data.csv").write_text(raw)
        (ws/"manifest.json").write_text(json.dumps({"schema":schema}))
        result=CostingEngine().analyze(CsvCostingAdapter().from_text(raw,schema))
        source=create_snapshot(result,self.workspace,self.workspace,
                     dict(dataset_id=sid,period="2026-11",scenario="actual",owner_external_id=self.owner))
        v=self.project(source,object_id="unit-alpha")["object"]
        self.assertEqual((v["total_cost"],v["unit_cost"],v["revenue"],v["operating_margin"]),
                ("1.00000001","0.3333333366666666666666666667","7.50","6.49999999"))
        self.assertEqual(v["revenue_status"],"complete")
        self.assertNotIn("selling_price",v)

if __name__=="__main__":unittest.main()
