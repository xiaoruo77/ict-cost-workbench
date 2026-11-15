"""Production consumption cannot be supplied twice across independent files."""
import copy
import csv
import io
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / 'scripts'), str(ROOT / 'fixtures/industry_triad')]
from generate import build, csv_text
from profile_contract import make_schema
from cost_core.csv_adapter import CsvCostingAdapter
from cost_core.costing import CostingEngine


class ProductionBundleTests(unittest.TestCase):
    def test_old_composite_cannot_bypass_production_duplicate_protection(self):
        bundle = build('pharma')
        schema = make_schema(bundle)
        composite = schema['raw_composite']
        stock = composite['inputs']['raw_materials']['raw_inventory']
        finance = composite['inputs']['finance']['raw_recognition']
        legacy = dict(raw_composite=dict(version=3, scope=composite['scope'], inputs={
            'finance': {'raw_recognition': finance}, 'stock': {'raw_inventory': stock}}))
        with self.assertRaisesRegex(ValueError, 'named-file v4'):
            CsvCostingAdapter.validate_schema(legacy)

    def test_inventory_exclusive_element_rejects_a_second_financial_posting(self):
        bundle = build('pharma')
        schema = make_schema(bundle)
        schema['raw_composite'].pop('source_hashes')
        branch = schema['raw_composite']['inputs']['finance']['raw_recognition']
        template = next(rule for rule in branch['rules'] if rule.get('op') == 'recognize')
        tag = template['source_tag']
        rows = list(csv.DictReader(io.StringIO(bundle['files']['finance'])))
        source = next(row for row in rows if row['tag'] == tag)
        branch['posting_map'][template['id']]['cost_element'] = 'materials'
        # This record has a different ID and economic identity from stock.
        # The declared inventory-exclusive batch/category still rejects it.
        self.assertNotEqual(source['economic_key'], 'batch-a-material-issue')
        with self.assertRaisesRegex(ValueError, 'already supplied by inventory'):
            CsvCostingAdapter().from_files(bundle['files'], schema)

    def test_inventory_economic_alias_cannot_hide_in_a_different_cost_element(self):
        bundle = build('pharma')
        schema = make_schema(bundle)
        schema['raw_composite'].pop('source_hashes')
        rows = list(csv.DictReader(io.StringIO(bundle['files']['finance'])))
        stock = list(csv.DictReader(io.StringIO(bundle['files']['raw_materials'])))
        key = next(row['economic_key'] for row in stock if row['kind'] == 'production_issue')
        rows[0]['economic_key'] = key
        bundle['files']['finance'] = csv_text(rows, list(rows[0]))
        with self.assertRaisesRegex(ValueError, 'economic component'):
            CsvCostingAdapter().from_files(bundle['files'], schema)

    def test_branch_order_preserves_compiled_ownership_and_evidence(self):
        bundle = build('pharma')
        schema = make_schema(bundle)
        first = CsvCostingAdapter().from_files(bundle['files'], schema)
        ordered = list(schema['raw_composite']['inputs'].items())
        schema['raw_composite']['inputs'] = dict(reversed(ordered))
        second = CsvCostingAdapter().from_files(bundle['files'], schema)
        a = CostingEngine().analyze(first)
        b = CostingEngine().analyze(second)
        self.assertEqual(a['totals'], b['totals'])
        self.assertEqual(a['recognition']['object_rollups'], b['recognition']['object_rollups'])
        self.assertEqual(a['business_views']['lifecycle'], b['business_views']['lifecycle'])
