"""One unchanged engine request must contain operational and financial facts."""
import copy
import csv
import hashlib
import io
import json
import unittest
from decimal import Decimal as D
from pathlib import Path
from cost_core.csv_adapter import CsvCostingAdapter
from cost_core.costing import CostingEngine

ROOT=Path(__file__).resolve().parents[1]/"fixtures/logistics_integrated"

def fixture():
    files={p.stem:p.read_text(encoding="utf-8") for p in ROOT.glob("*.csv")}
    schema=json.loads((ROOT/"rules.json").read_text(encoding="utf-8"))
    expected=json.loads((ROOT/"expected.json").read_text(encoding="utf-8"))
    return files,schema,expected

def calculate(files,schema):
    return CostingEngine().analyze(CsvCostingAdapter().from_files(files,schema))

class LogisticsIntegratedTests(unittest.TestCase):
    def test_operational_quantity_financial_assets_settlement_same_request(self):
        files,schema,expected=fixture();out=calculate(files,schema)
        self.assertEqual(D(out["totals"]["total_cost"]),D("1762.00"))
        self.assertEqual({k:D(v) for k,v in out["totals"]["cost_structure"].items()},
                         {k:D(v) for k,v in expected["cost_groups"].items()})
        obj=next(x for x in out["objects"] if x["object_id"]=="service-1")
        self.assertEqual(D(obj["quantity"]),D("12000"))
        self.assertEqual(obj["quantity_unit"],"tonne_km")
        self.assertLessEqual(abs(D(obj["unit_cost"])*D("12000")-D("1762.00")),D("0.00000000000000000000001"))
        origins={r["source_uri"] for r in obj["source_refs"]}
        self.assertIn("upload://sources/tms.csv",origins)
        self.assertIn("upload://sources/gps.csv",origins)
        self.assertEqual({x["name"] for x in out["recognition"]["source_bundle"]["sources"]},set(files))
        financial=out["business_views"]["financial_ledgers"]
        self.assertEqual(out["business_views"]["presentation_version"],4)
        self.assertEqual(financial["assets"]["totals"]["closing_value"],"13328.00")
        self.assertEqual(financial["settlement"]["totals"]["receivable_closing"],"500.00")
        self.assertEqual(financial["settlement"]["totals"]["payable_closing"],"450.00")
        self.assertEqual(financial["settlement"]["totals"]["agency_liability_closing"],"300.00")
        self.assertEqual(financial["settlement"]["totals"]["declared_net_revenue"],"500.00")
        self.assertEqual(financial["custody"]["rows"][0]["closing_quantity"],"27")
        self.assertEqual(financial["cost_bridge"]["asset_recognition_in_engine"],"912.00")
        self.assertEqual(D(out["business_views"]["asset_total"]),D(0))
        self.assertTrue(out["ledger_reconciled"])
        identity=json.loads((ROOT/"BUILD_IDENTITY.json").read_text(encoding="utf-8"))
        for name,digest in identity["base_five_csv_sha256"].items():
            self.assertEqual(hashlib.sha256((ROOT/name).read_bytes()).hexdigest(),digest)

    def test_service_report_hides_empty_production_table_with_versioned_compatibility(self):
        from costing_outputs import _report
        files, schema, _ = fixture()
        result = calculate(files, schema)
        snapshot = {"result": result,
                    "context": {"dataset_id": result["dataset_id"], "period": result["period"],
                                "scenario": result["scenario"]},
                    "analysis_id": "1" * 32, "revision": "2" * 64, "evidence": []}
        header = "| 对象 | 在制 | 完工库存 | 净销售成本 | 报废损失 | 合格单位成本 |"
        self.assertNotIn(header, _report(snapshot))
        result["business_views"]["presentation_version"] = 3
        self.assertIn(header, _report(snapshot))

    def test_object_scope_does_not_embed_book_wide_auxiliaries(self):
        files, schema, _ = fixture()
        request = CsvCostingAdapter().from_files(files, schema)
        result = CostingEngine().analyze(request, cost_object="service-1")
        self.assertEqual(result["recognition"]["operational_inputs"]["status"],
                         "requires_book_scope")
        self.assertNotIn("metrics", result["recognition"]["operational_inputs"])
        financial = result["business_views"]["financial_ledgers"]
        self.assertEqual(financial["status"], "requires_book_scope")
        self.assertIsNone(financial.get("settlement"))
        obj = next(r for r in result["objects"] if r["object_id"] == "service-1")
        self.assertIn("upload://sources/tms.csv", {r["source_uri"] for r in obj["source_refs"]})

    def test_mismatched_object_unit_rejected_with_identical_original_sources(self):
        f,s,_=fixture()
        s["raw_composite"]["operational_inputs"]["unit_registry"]["pallet_day"]={"dimensions":{"pallet":1,"time":1},"factor":"86400","quantity_kind":"pallet_time"}
        calculate(f,s)
        original=copy.deepcopy(s["raw_composite"]["source_hashes"])
        s["raw_composite"]["operational_inputs"]["object_bindings"][0]["quantity_unit"]="pallet_day"
        self.assertEqual(s["raw_composite"]["source_hashes"],original)
        with self.assertRaisesRegex(ValueError,"unit|dimension"):calculate(f,s)

    def test_operational_source_tamper_cannot_hide_behind_financial_match(self):
        f,s,_=fixture();calculate(f,s)
        f["tms"]=f["tms"].replace("12000","12001")
        with self.assertRaisesRegex(ValueError,"hash"):calculate(f,s)

if __name__=="__main__":unittest.main()
