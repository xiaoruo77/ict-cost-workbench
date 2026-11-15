import copy,csv,hashlib,io,sys,unittest
from decimal import Decimal,getcontext
from pathlib import Path
from urllib.parse import parse_qs
from cost_core.operational_ingest import compile_operational_bundle,operational_files

# This fixture generator has no tested-engine dependency; fixed expected formulas
# are independently stated and are not derived from compiler output.
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/"fixtures"/"operational_exports"))
from generate_operational_fixture import fixture

class OperationalIngestTests(unittest.TestCase):
    def setUp(self):
        self.files,self.spec,self.scope,self.expected=fixture()
    def run_compile(self):
        return compile_operational_bundle(self.spec,self.files,self.scope,dataset_id="csv-ops-tests")
    def mutate(self,name,change,update_hash=True):
        reader=csv.DictReader(io.StringIO(self.files[name]));headers=reader.fieldnames;rows=list(reader);change(rows)
        s=io.StringIO(newline="");w=csv.DictWriter(s,fieldnames=headers,lineterminator="\n");w.writeheader();w.writerows(rows)
        self.files[name]=s.getvalue()
        if update_hash:self.spec["source_hashes"][name]=hashlib.sha256(self.files[name].encode()).hexdigest()
    def test_independent_quantities_driver_relations_and_scope(self):
        precision=getcontext().prec
        result=self.run_compile()
        self.assertEqual(getcontext().prec,precision)
        self.assertEqual({x["object_id"]:x["quantity"] for x in result["objects"]},self.expected["quantities"])
        self.assertEqual(result["drivers"]["regional-to-completed-services"]["weights"],self.expected["driver_weights"])
        self.assertEqual(result["metadata"]["source_rows"],self.expected["source_rows"])
        self.assertEqual(result["metadata"]["unit_validation_status"],"source_verified")
        self.assertEqual(result["metadata"]["relations"][0]["matched_events"],20)
        self.assertEqual(result["generated_inputs"],{})
        self.assertEqual(result["metadata"]["scope"],self.scope)
    def test_mapped_original_id_selector_and_all_source_coverage(self):
        result=self.run_compile()
        refs=[]
        for metric in result["metadata"]["metrics"].values():
            for x in metric.values():refs.extend(x["source_refs"])
        refs.extend(result["metadata"]["relations"][0]["source_refs"])
        found={name:set() for name in self.files}
        for ref in refs:
            name=ref["source_uri"].split("/")[-1][:-4]
            rows=list(csv.DictReader(io.StringIO(self.files[name])))
            q=parse_qs(ref["selector"]);self.assertIn("event_id",q);self.assertNotIn("id",q)
            idx=int(q["row"][0]);self.assertEqual(rows[idx-2]["event_id"],q["event_id"][0]);self.assertIn(ref["field"],rows[idx-2])
            found[name].add(idx)
        for name,n in self.expected["source_rows"].items():self.assertEqual(found[name],set(range(2,n+2)))
    def test_raw_kilograms_to_tonnes_and_metres_to_kilometres(self):
        self.mutate("gps",lambda rows:[r.update(odometer_start="1000000",odometer_end="2000000",distance_unit="m") for r in rows])
        result=self.run_compile()
        self.assertEqual(result["metadata"]["metrics"]["road-volume"]["route-self"]["quantity"],"12000")
    def test_duration_clips_month_and_explicit_open_end(self):
        self.mutate("wms",lambda rows:rows[0].update(storage_end=""))
        self.spec["metrics"][1]["factors"][1]["open_end"]="scope_end"
        self.assertEqual(self.run_compile()["metadata"]["metrics"]["storage-volume"]["warehouse-A"]["quantity"],"300")
    def test_missing_end_without_approved_policy_rejected(self):
        self.mutate("wms",lambda rows:rows[0].update(storage_end=""))
        with self.assertRaisesRegex(ValueError,"missing end"):self.run_compile()
    def test_wrong_financial_binding_unit_rejected(self):
        self.spec["object_bindings"][0]["quantity_unit"]="pallet_day"
        with self.assertRaisesRegex(ValueError,"declared unit differs"):self.run_compile()
    def test_wrong_output_unit_dimension_rejected(self):
        self.spec["metrics"][0]["unit"]="pallet_day"
        with self.assertRaisesRegex(ValueError,"output unit dimensions"):self.run_compile()
    def test_wrong_raw_unit_rejected_even_when_hash_valid(self):
        self.mutate("tms",lambda rows:rows[0].update(weight_unit="pallet"))
        with self.assertRaisesRegex(ValueError,"raw unit dimension"):self.run_compile()
    def test_unapproved_unit_rejected(self):
        self.mutate("gps",lambda rows:rows[0].update(distance_unit="mile"))
        with self.assertRaisesRegex(ValueError,"not in approved"):self.run_compile()
    def test_source_tamper_rejected(self):
        self.mutate("tms",lambda rows:rows[0].update(weight="15000"),False)
        with self.assertRaisesRegex(ValueError,"source hash mismatch"):self.run_compile()
    def test_book_period_currency_mismatch_rejected(self):
        for key,value in [("book","OTHER"),("period","2026-12"),("currency","USD")]:
            with self.subTest(key=key):
                self.setUp();self.mutate("tms",lambda rows:rows[0].update({key:value}))
                with self.assertRaisesRegex(ValueError,"cross-book/period/currency"):self.run_compile()
    def test_duplicate_and_aliased_event_ids_rejected(self):
        for duplicate in ["TMS-001","tms-001","ＴＭＳ-001"]:
            with self.subTest(duplicate=duplicate):
                self.setUp();self.mutate("tms",lambda rows:rows[1].update(event_id=duplicate))
                with self.assertRaisesRegex(ValueError,"duplicate or aliased event"):self.run_compile()
    def test_unmatched_join_rejected(self):
        self.mutate("tms",lambda rows:rows[0].update(leg_id="MISSING"))
        with self.assertRaisesRegex(ValueError,"unmatched operational join"):self.run_compile()
    def test_duplicate_join_keys_rejected(self):
        self.mutate("gps",lambda rows:rows[1].update(leg_id="leg-1"))
        with self.assertRaisesRegex(ValueError,"duplicate/aliased join"):self.run_compile()
    def test_negative_cumulative_distance_rejected(self):
        self.mutate("gps",lambda rows:rows[0].update(odometer_end="0"))
        with self.assertRaisesRegex(ValueError,"negative cumulative"):self.run_compile()
    def test_bad_date_range_and_naive_timestamp_rejected(self):
        for values,reason in [({"storage_end":"2026-01-01T00:00:00Z"},"end precedes"),({"storage_start":"2026-11-01T00:00:00"},"timezone-aware")]:
            with self.subTest(values=values):
                self.setUp();self.mutate("wms",lambda rows:rows[0].update(values))
                with self.assertRaisesRegex(ValueError,reason):self.run_compile()
    def test_metric_reversal_original_missing_or_excess_rejected(self):
        for values,reason in [({"original_event_id":"OTHER"},"original not found"),({"weight":"15000"},"exceeds original")]:
            with self.subTest(values=values):
                self.setUp();self.mutate("tms",lambda rows:rows[2].update(values))
                with self.assertRaisesRegex(ValueError,reason):self.run_compile()
    def test_return_original_missing_identity_and_excess_rejected(self):
        for values,reason in [({"original_order_id":"OTHER"},"original record not found"),({"sku":"OTHER"},"identity mismatch"),({"quantity":"2"},"return exceeds")]:
            with self.subTest(values=values):
                self.setUp();self.mutate("returns",lambda rows:rows[0].update(values))
                with self.assertRaisesRegex(ValueError,reason):self.run_compile()
    def test_repeated_partial_return_cumulative_cap(self):
        def change(rows):
            rows[1].update(original_order_id=rows[0]["original_order_id"])
        self.mutate("returns",change)
        with self.assertRaisesRegex(ValueError,"return exceeds"):self.run_compile()
    def test_driver_zero_denominator_rejected(self):
        self.mutate("activity",lambda rows:[r.update(work_end=r["work_start"]) for r in rows])
        with self.assertRaisesRegex(ValueError,"zero operational driver"):self.run_compile()
    def test_missing_metric_is_unknown_not_zero(self):
        self.spec["object_bindings"][2]["required"]=False
        self.mutate("wms",lambda rows:rows[0].update(status="missing",pallets=""))
        result=self.run_compile();row=result["metadata"]["metrics"]["storage-volume"]["warehouse-A"]
        self.assertEqual(row["status"],"missing");self.assertIsNone(row["quantity"])
        obj=next(x for x in result["objects"] if x["object_id"]=="warehouse-A");self.assertIsNone(obj["quantity"])
    def test_required_missing_rejected_not_zero(self):
        self.mutate("wms",lambda rows:rows[0].update(status="missing",pallets=""))
        with self.assertRaisesRegex(ValueError,"required operational quantity missing"):self.run_compile()
    def test_explicit_zero_keeps_source_evidence(self):
        self.mutate("wms",lambda rows:rows[0].update(pallets="0"))
        row=self.run_compile()["metadata"]["metrics"]["storage-volume"]["warehouse-A"]
        self.assertEqual(row["quantity"],"0");self.assertEqual(row["status"],"explicit_zero");self.assertTrue(row["source_refs"])
    def test_unmapped_source_rows_rejected(self):
        self.spec["metrics"][0]["match"]={"service_id":"route-self"}
        with self.assertRaisesRegex(ValueError,"unmapped operational rows"):self.run_compile()
    def test_static_invalid_mapping_rejected_even_when_missing(self):
        self.spec["object_bindings"][2]["required"]=False
        self.spec["metrics"][1]["factors"][0]["field"]="typo"
        self.mutate("wms",lambda rows:rows[0].update(status="missing"))
        with self.assertRaisesRegex(ValueError,"mapped factor column missing"):self.run_compile()
    def test_duplicate_column_alias_rejected(self):
        self.files["tms"]=self.files["tms"].replace("service_id,book","EVENT_ID,book",1)
        self.spec["source_hashes"].pop("tms")
        with self.assertRaisesRegex(ValueError,"duplicate/aliased column"):self.run_compile()
    def test_unknown_spec_field_rejected(self):
        self.spec["guess"]=True
        with self.assertRaisesRegex(ValueError,"unknown fields"):self.run_compile()
    def test_standard_registry_dimension_and_factor_redefinition_rejected(self):
        for name,change in [("tonne_km",{"dimensions":{"pallet":1,"time":1}}),("kg",{"factor":"0.001"}),("pallet_day",{"dimensions":{"mass":1,"length":1},"factor":"1000000"})]:
            with self.subTest(name=name,change=change):
                self.setUp();self.spec["unit_registry"][name].update(change)
                with self.assertRaisesRegex(ValueError,"standard unit"):self.run_compile()
    def test_reserved_unit_alias_cannot_change_meaning(self):
        self.spec["unit_registry"]["order"]["aliases"]=["kg"]
        with self.assertRaisesRegex(ValueError,"standard unit alias"):self.run_compile()
    def test_alias_cycle_or_shadowing_rejected(self):
        self.spec["unit_registry"]["order"]["aliases"]=["completed_order"]
        self.spec["unit_registry"]["completed_order"]["aliases"]=["order"]
        with self.assertRaisesRegex(ValueError,"duplicate unit/alias"):self.run_compile()
    def test_invalid_conversion_factors_rejected(self):
        for factor in ["0","-1","NaN","Infinity","1e100",True]:
            with self.subTest(factor=factor):
                self.setUp();self.spec["unit_registry"]["kg"]["factor"]=factor
                with self.assertRaises(ValueError):self.run_compile()
    def test_business_count_kinds_not_automatically_interchangeable(self):
        self.mutate("orders",lambda rows:rows[0].update(unit="processed_return"))
        with self.assertRaisesRegex(ValueError,"quantity_kind"):self.run_compile()
    def test_custom_business_unit_kind_is_required(self):
        self.spec["unit_registry"]["order"].pop("quantity_kind")
        with self.assertRaisesRegex(ValueError,"quantity_kind"):self.run_compile()
    def test_source_verified_boundary_does_not_assert_vendor_authenticity(self):
        boundary=self.run_compile()["metadata"]["boundary"]
        self.assertIn("declared unit contract only",boundary)
        self.assertIn("not verified",boundary)
    def test_malformed_mapping_types_are_located_value_errors(self):
        cases=[
          lambda s:s["sources"]["tms"]["fields"].update(id=[]),
          lambda s:s["metrics"][0].update(source=[]),
          lambda s:s["metrics"][0]["joins"][0].update(source=[]),
          lambda s:s["metrics"][0]["factors"][0].update(source=[]),
          lambda s:s["relations"][0].update(source=[]),
          lambda s:s["relations"][0].update(same_fields={"sku":[]}),
          lambda s:s["object_bindings"][0].update(metric_id=[]),
          lambda s:s["driver_bindings"][0].update(metric_id=[]),
        ]
        for change in cases:
            with self.subTest(change=change):
                self.setUp();change(self.spec)
                with self.assertRaisesRegex(ValueError,"operational"):self.run_compile()
    def test_fiscal_month_fixed_timezone_boundaries(self):
        self.spec["period_timezone"]="+08:00"
        self.mutate("wms",lambda rows:rows[0].update(storage_start="2026-10-31T16:00:00Z",storage_end="2026-11-30T16:00:00Z"))
        row=self.run_compile()["metadata"]["metrics"]["storage-volume"]["warehouse-A"]
        self.assertEqual(row["quantity"],"300")
    def test_missing_or_invalid_period_timezone_rejected(self):
        for zone in [None,"not-a-zone","+25:00"]:
            with self.subTest(zone=zone):
                self.setUp()
                if zone is None:self.spec.pop("period_timezone")
                else:self.spec["period_timezone"]=zone
                with self.assertRaisesRegex(ValueError,"period_timezone"):self.run_compile()
    def test_object_id_alias_across_source_files_rejected(self):
        self.mutate("activity",lambda rows:rows[0].update(service_id="ROUTE-SELF"))
        with self.assertRaisesRegex(ValueError,"aliased operational object"):self.run_compile()
    def test_bad_raw_unit_error_locates_original_row_and_field(self):
        self.mutate("tms",lambda rows:rows[0].update(weight_unit="pallet"))
        with self.assertRaises(ValueError) as found:self.run_compile()
        self.assertIn("tms row=2 id=TMS-001",str(found.exception))
        self.assertIn("weight_unit",str(found.exception))
    def test_operational_files_only_declared_auxiliary_sources(self):
        self.assertEqual(operational_files(self.spec),set(self.files))
if __name__=="__main__":unittest.main()