"""Exact provenance for new versioned contracts; legacy selectors stay stable."""
import json,tempfile,unittest
from pathlib import Path
from costing_outputs import _capture_ref
class ExtendedReportProvenanceTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name);self.ds="csv-0123456789abcdef"
        folder=self.root/"costing_datasets"/self.ds;folder.mkdir(parents=True)
        self.schema={"raw_composite":{"operational_inputs":{"metrics":[{"metric_id":"weight-distance","quantity_unit":"tonne_km"}]},"allocation_rules":[{"rule_id":"legacy"}]},
          "business_views":{"financial_ledgers":{"assets":{"accounts":{"fuel":{"kind":"inventory","unit":"litre"}}}}}}
        (folder/"manifest.json").write_text(json.dumps({"schema":self.schema}),encoding="utf-8")
        (folder/"sources").mkdir();(folder/"sources/shipments.csv").write_text("event_id,mass,mass_unit\nshipment-1,12000,kg\n",encoding="utf-8")
    def tearDown(self):self.tmp.cleanup()
    def capture(self,selector,field="configuration"):
        return _capture_ref({"dataset_id":self.ds,"source_uri":"upload://schema","field":field,"selector":selector,"kind":"configuration","formula":""},self.root,self.root,self.ds)
    def test_operational_nested_selector(self):
        ev=self.capture("raw_composite.operational_inputs.metrics[0].quantity_unit")
        self.assertEqual(ev["status"],"captured_mapping")
        self.assertEqual(ev["mapping"].get("configuration"),"tonne_km")
    def test_financial_nested_selector(self):
        ev=self.capture("business_views.financial_ledgers.assets.accounts.fuel.unit")
        self.assertEqual(ev["status"],"captured_mapping")
        self.assertEqual(ev["mapping"].get("configuration"),"litre")
    def test_invalid_financial_selector_fails_closed(self):
        ev=self.capture("business_views.financial_ledgers.assets.accounts.missing.unit")
        self.assertEqual(ev["status"],"selector_not_found")
    def test_extended_selector_expression_is_not_evaluated(self):
        ev=self.capture("business_views.financial_ledgers.__class__.__bases__[0]")
        self.assertEqual(ev["status"],"selector_not_found")
    def test_legacy_allocation_selector_unchanged(self):
        ev=self.capture("raw_composite.allocation_rules[0]","allocation_rules")
        self.assertEqual(ev["mapping"]["configuration"],{"rule_id":"legacy"})
    def test_original_alias_header_selects_exact_row(self):
        ev=_capture_ref({"dataset_id":self.ds,"source_uri":"upload://sources/shipments.csv","field":"mass","selector":"row=2&event_id=shipment-1","kind":"raw","formula":""},self.root,self.root,self.ds)
        self.assertEqual(ev["status"],"captured");self.assertEqual(ev["rows"][0]["values"]["mass"],"12000")
    def test_cross_dataset_reference_rejected(self):
        with self.assertRaisesRegex(ValueError,"其他数据集"):
            _capture_ref({"dataset_id":"csv-ffffffffffffffff","source_uri":"upload://schema","field":"x","selector":"business_views.financial_ledgers","kind":"configuration","formula":""},self.root,self.root,self.ds)
if __name__=="__main__":unittest.main()
