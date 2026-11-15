"""Contracts for strict recognition object declarations and bounded evaluation."""
import csv
import io
import json
import unittest
from decimal import Decimal
from cost_core.csv_adapter import CsvCostingAdapter
from cost_core.costing import CostingEngine

def fixture(count=1, targets=1, declared_quantity=None, evidence_size=20):
    stream=io.StringIO(newline="")
    headers=["id","book","period","currency","kind","status","amount","economic_key","evidence","tag","target_object_id","origin_period"]
    writer=csv.DictWriter(stream,fieldnames=headers,lineterminator="\n");writer.writeheader()
    total=Decimal(0)
    for index in range(count):
        amount=Decimal(index%17+1)/100;total+=amount
        writer.writerow({"id":f"source-{index:05}","book":"SYNTH","period":"2026-11","currency":"CNY","kind":"expense","status":"data","amount":str(amount),"economic_key":f"economic-{index:05}","evidence":"x"*evidence_size,"tag":"cost","target_object_id":f"service-{index%targets}","origin_period":""})
    declarations=[{"object_id":f"service-{n}","object_kind":"service"} for n in range(targets)]
    if declared_quantity is not None:
        for item in declarations:item.update(quantity=declared_quantity,quantity_unit="tonne_km")
    schema={"dataset_id":"csv-bounded-test","plugin_id":"csv-generic","period":"2026-11","currency":"CNY",
      "raw_recognition":{"version":2,"name":"bounded original events","context":{"book":"SYNTH","period":"2026-11","currency":"CNY"},
      "rules":[{"id":"cost","op":"recognize","source_tag":"cost","source_kind":"expense","posting_pool":"period"}],
      "outputs":["cost"],"excluded_tags":{},"ledger_basis":"recognized_expenses_incurred",
      "posting_map":{"cost":{"object_id_from":"target_object_id","object_kind":"service","record_type":"entry","cost_element":"cost","behavior":"unknown","cost_type":"direct"}},
      "target_objects":declarations}}
    return stream.getvalue(),schema,total

class RecognitionAdapterContracts(unittest.TestCase):
    def calculate(self,text,schema):
        request=CsvCostingAdapter().from_text(text,schema,source_uri="upload://sources/finance.csv")
        return request,CostingEngine().analyze(request)
    def test_direct_posting_uses_declared_service_quantity(self):
        text,schema,total=fixture(declared_quantity="100")
        try:request,result=self.calculate(text,schema)
        except ValueError as exc:self.fail("Valid explicit target declaration conflicts with implicit posting defaults: "+str(exc))
        target=next(x for x in request.objects if x.object_id=="service-0")
        self.assertEqual((target.quantity,target.quantity_unit),(Decimal("100"),"tonne_km"))
        self.assertEqual(Decimal(result["totals"]["total_cost"]),total)
        self.assertTrue(any(ref.kind=="configuration" and ref.field=="quantity" for ref in target.source_refs))
    def test_sparse_event_distribution_is_batched_without_losing_rows(self):
        text,schema,total=fixture(525,75)
        try:request,result=self.calculate(text,schema)
        except ValueError as exc:self.fail("Admitted source events must not depend on >32 records per target: "+str(exc))
        self.assertEqual(Decimal(result["totals"]["total_cost"]),total)
        self.assertTrue(result["ledger_reconciled"])
        refs={ref.selector for entry in request.entries for ref in entry.source_refs if ref.source_uri=="upload://sources/finance.csv"}
        self.assertEqual(len(refs),525)
        self.assertEqual(len(request.objects),75)
    def test_explicit_kind_conflict_still_rejected(self):
        text,schema,_=fixture(declared_quantity="100")
        schema["raw_recognition"]["target_objects"][0]["object_kind"]="cost_center"
        with self.assertRaisesRegex(ValueError,"conflict"):
            self.calculate(text,schema)
    def test_ordinary_csv_selling_price_conflict_still_rejected(self):
        text="object_id,object_kind,quantity,quantity_unit,currency,selling_price,cost_element,amount,period\nA,service,10,tonne_km,CNY,20,fuel,10.00,2026-11\nA,service,10,tonne_km,CNY,21,labor,20.00,2026-11\n"
        with self.assertRaisesRegex(ValueError,"including selling_price"):
            self.calculate(text,{"period":"2026-11","currency":"CNY"})
    def test_large_evidence_documents_remain_bounded(self):
        text,schema,total=fixture(200,200,evidence_size=4000)
        request,result=self.calculate(text,schema)
        self.assertEqual(Decimal(result["totals"]["total_cost"]),total)
        audit=request.recognition["bounded_evaluation"]
        self.assertLessEqual(audit["max_sources"],500)
        self.assertLessEqual(audit["max_rules"],500)
        self.assertLessEqual(audit["max_outputs"],100)
        self.assertLessEqual(audit["max_document_bytes"],512*1024)


def raw_graph(count=600):
    sources=[];rules=[]
    for n in range(count):
        sid=f"s-{n:05}";rid=f"r-{n:05}"
        sources.append({"id":sid,"book":"SYNTH","period":"2026-11","currency":"CNY","kind":"expense",
                        "status":"data","amount":str(Decimal(n%17+1)/100),"economic_key":f"e-{n:05}","evidence":"original row"})
        rules.append({"id":rid,"op":"recognize","source":sid,"posting_pool":"period"})
    ids=[r["id"] for r in rules];depth=0
    while len(ids)>32:
        groups=[]
        for index in range(0,len(ids),32):
            rid=f"sum-{depth}-{index//32}";rules.append({"id":rid,"op":"sum","inputs":ids[index:index+32]});groups.append(rid)
        ids=groups;depth+=1
    rules.append({"id":"total","op":"sum","inputs":ids})
    return {"name":"bounded DAG regression","context":{"book":"SYNTH","period":"2026-11","currency":"CNY"},
            "sources":sources,"rules":rules,"outputs":["total"],"excluded_sources":{}}

class BoundedRecognitionSecurity(unittest.TestCase):
    def bounded(self,raw):
        from cost_core.recognition_batches import run_bounded_document
        return run_bounded_document(raw)
    def invalid(self,raw,reason):
        out=self.bounded(raw);self.assertEqual(out["status"],"invalid",out)
        self.assertIn(reason,out["error"])
        self.assertEqual(out["outputs"],{})
    def test_small_document_semantics_equal_strict_evaluator(self):
        from cost_core.recognition_eval import run_document
        raw=raw_graph(35);expected=run_document(raw);actual=self.bounded(raw)
        actual.pop("bounded_evaluation")
        self.assertEqual(actual,expected)
    def test_cross_chunk_duplicate_economic_component_rejected(self):
        raw=raw_graph();raw["sources"][-1]["economic_key"]=raw["sources"][0]["economic_key"]
        self.invalid(raw,"Duplicate posting economic component")
    def test_cross_chunk_duplicate_source_id_rejected(self):
        raw=raw_graph();raw["sources"][-1]["id"]=raw["sources"][0]["id"]
        self.invalid(raw,"Duplicate source ID")
    def test_unused_source_not_silently_dropped(self):
        raw=raw_graph();extra={**raw["sources"][0],"id":"unused","economic_key":"unused"}
        raw["sources"].append(extra);self.invalid(raw,"Unclassified source coverage")
    def test_dead_overlapping_view_rejected(self):
        raw=raw_graph();raw["rules"].append({"id":"double","op":"sum","inputs":["r-00000","r-00000"]})
        self.invalid(raw,"Double recognition")
    def test_dead_cycle_rejected(self):
        raw=raw_graph();raw["rules"] += [{"id":"cycle-a","op":"sum","inputs":["cycle-b"]},{"id":"cycle-b","op":"sum","inputs":["cycle-a"]}]
        self.invalid(raw,"Cycle detected")
    def test_global_depth_limit_rejected(self):
        raw=raw_graph();previous="r-00000"
        for n in range(65):
            rid=f"depth-{n}";raw["rules"].append({"id":rid,"op":"sum","inputs":[previous]});previous=rid
        self.invalid(raw,"dependency depth limit")
    def test_over_100_declared_outputs_remains_rejected(self):
        raw=raw_graph();raw["outputs"]=[f"r-{n:05}" for n in range(101)]
        self.invalid(raw,"Too many outputs")
    def test_missing_excluded_source_remains_rejected(self):
        raw=raw_graph();row=raw["sources"][-1];row.update(status="missing",amount=None)
        raw["excluded_sources"][row["id"]]="missing is not zero"
        self.invalid(raw,"Missing source")
    def test_schedule_and_inventory_bridge_use_strict_operations(self):
        from cost_core.recognition_eval import run_document
        raw=raw_graph(35)
        for ident,kind,amount in [("basis","prepay_basis","1200.00"),("opening","opening_balance","30.00"),("closing","closing_balance","20.00")]:
            row={"id":ident,**raw["context"],"kind":kind,"status":"data","amount":amount,"economic_key":ident,"evidence":"approved synthetic record"}
            if kind=="prepay_basis":row["origin_period"]="2026-01"
            else:row["bridge_group"]="stock"
            raw["sources"].append(row)
        raw["rules"] += [{"id":"amortization","op":"monthly_straight_line","source":"basis","start":"2026-01","months":12,"posting_pool":"period"},
                         {"id":"inventory","op":"inventory_bridge","flow":"total","opening":"opening","closing":"closing"}]
        raw["outputs"]=["total","amortization","inventory"]
        actual=self.bounded(raw);actual.pop("bounded_evaluation")
        self.assertEqual(actual,run_document(raw))
        self.assertEqual(actual["outputs"]["amortization"],"100.00")
    def test_large_legal_subtotals_not_restricted_as_new_source_money(self):
        raw=raw_graph()
        for row in raw["sources"]:row["amount"]="999999999999999999.99"
        actual=self.bounded(raw);self.assertEqual(actual["status"],"valid",actual)
        self.assertEqual(Decimal(actual["outputs"]["total"]),Decimal("999999999999999999.99")*600)
    def test_every_observed_document_respects_strict_caps(self):
        raw=raw_graph();actual=self.bounded(raw);self.assertEqual(actual["status"],"valid")
        observed=actual["bounded_evaluation"]
        self.assertGreater(observed["evaluation_document_count"],1)
        for key,limit in [("sources",500),("rules",500),("outputs",100),("document_bytes",512*1024)]:
            self.assertLessEqual(observed["max_"+key],limit)


class OperationalRecognitionIntegration(unittest.TestCase):
    def configured(self):
        from pathlib import Path
        import copy
        folder=Path(__file__).resolve().parents[1]/"fixtures"/"operational_exports"
        spec=json.loads((folder/"operational_inputs.json").read_text(encoding="utf-8"))
        spec["driver_bindings"]=[]
        text,inner,_=fixture()
        text=text.replace("SYNTH,","SYNTH_LOGISTICS,").replace("service-0","route-self")
        rec=inner["raw_recognition"]
        rec["context"]["book"]="SYNTH_LOGISTICS"
        rec["target_objects"][0]["object_id"]="route-self"
        schema={"dataset_id":"csv-operational-contract","plugin_id":"csv-generic","period":"2026-11","currency":"CNY",
                "raw_composite":{"version":4,"scope":dict(rec["context"]),
                  "inputs":{"finance":{"file":"finance","raw_recognition":rec}},"operational_inputs":spec}}
        files={p.stem:p.read_text(encoding="utf-8") for p in folder.glob("*.csv")}
        files["finance"]=text
        return files,schema
    def test_original_operational_sources_bind_before_cost_calculation(self):
        files,schema=self.configured()
        request=CsvCostingAdapter().from_files(files,schema)
        result=CostingEngine().analyze(request)
        self.assertEqual(Decimal(result["totals"]["total_cost"]),Decimal("0.01"))
        self.assertEqual(request.recognition["operational_inputs"]["unit_validation_status"],"source_verified")
        target=next(o for o in request.objects if o.object_id=="route-self")
        self.assertEqual((target.quantity,target.quantity_unit),(Decimal("12000"),"tonne_km"))
        self.assertTrue(any(r.kind=="raw" and r.source_uri.startswith("upload://sources/") and "event_id" in r.selector for r in target.source_refs))
        # No invented in-memory target.quantity selector may enter a snapshot.
        self.assertFalse(any(".target_objects[0].quantity" in r.selector for r in target.source_refs))
    def test_operational_source_rejects_inconsistent_financial_unit(self):
        files,schema=self.configured()
        schema["raw_composite"]["inputs"]["finance"]["raw_recognition"]["target_objects"][0]["quantity_unit"]="pallet_day"
        with self.assertRaisesRegex(ValueError,"operational unit conflicts"):
            CsvCostingAdapter().from_files(files,schema)
    def test_operational_source_rejects_inconsistent_financial_quantity(self):
        files,schema=self.configured()
        schema["raw_composite"]["inputs"]["finance"]["raw_recognition"]["target_objects"][0]["quantity"]="11999"
        with self.assertRaisesRegex(ValueError,"operational quantity conflicts"):
            CsvCostingAdapter().from_files(files,schema)
    def test_unknown_operational_driver_binding_rejected(self):
        files,schema=self.configured()
        schema["raw_composite"]["operational_inputs"]["driver_bindings"]=[{
            "rule_id":"missing-allocation-rule","metric_id":"shared-work-hours","quantity_unit":"hour",
            "target_ids":["route-self","route-outsourced","warehouse-A","delivery-city","reverse-return"]}]
        with self.assertRaisesRegex(ValueError,"unknown allocation rule"):
            CsvCostingAdapter().from_files(files,schema)

if __name__=="__main__":unittest.main()
