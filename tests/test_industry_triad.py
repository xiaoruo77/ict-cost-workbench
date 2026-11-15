"""Three configurations, one adapter/engine; independent cents/physical oracle."""
from copy import deepcopy
from decimal import Decimal as D
import csv
import hashlib
import io
import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / 'fixtures' / 'industry_triad'
sys.path[:0] = [str(ROOT / 'scripts'), str(FIXTURES)]
from generate import build, csv_text, INDUSTRIES
from profile_contract import make_schema
from cost_core.csv_adapter import CsvCostingAdapter
from cost_core.costing import CostingEngine
from costing_outputs import create_snapshot, load_snapshot, export_snapshot, resolve_evidence


def evaluate(bundle, schema=None):
    schema = make_schema(bundle) if schema is None else schema
    return CostingEngine().analyze(CsvCostingAdapter().from_files(bundle['files'], schema))


def alter(bundle, schema, file, transform, repin=True):
    reader = csv.DictReader(io.StringIO(bundle['files'][file]))
    headers = reader.fieldnames
    rows = transform(list(reader))
    bundle['files'][file] = csv_text(rows, headers)
    if repin:
        schema['raw_composite']['source_hashes'][file] = hashlib.sha256(bundle['files'][file].encode()).hexdigest()


class IndustryTriad(unittest.TestCase):
    def assert_oracle(self, bundle, result):
        want = bundle['expected']
        self.assertEqual(D(result['totals']['total_cost']), D(want['total_cost']))
        self.assertTrue(result['ledger_reconciled'])
        self.assertEqual(D(result['totals']['unallocated_cost']), 0)
        self.assertFalse(result['recognition']['can_claim_complete'])
        self.assertEqual(result['recognition']['source_bundle']['plugin_version'], 'industry-triad-1.0.0')
        objs = {r['object_id']: r for r in result['objects']}
        for oid, expected in want['objects'].items():
            self.assertIn(oid, objs)
            self.assertEqual(D(objs[oid]['total_cost']), D(expected['total_cost']), oid)
            got = {k: D(v) for k, v in objs[oid]['cost_structure'].items() if D(v)}
            self.assertEqual(got, {k: D(v) for k, v in expected['cost_structure'].items() if D(v)}, oid)
        self.assertTrue(all(D(o['total_cost']) == 0 for k, o in objs.items() if k not in want['objects']))
        groups = {}
        for obj in objs.values():
            for key, amount in obj['cost_structure'].items():
                groups[key] = groups.get(key, D(0)) + D(amount)
        self.assertEqual({k: v for k, v in groups.items() if v}, {k: D(v) for k, v in want['cost_groups'].items() if D(v)})
        budgets = result['recognition']['object_budgets']
        self.assertEqual(D(budgets['summary']['comparable_budget']), D(want['budget_total']))
        self.assertEqual(D(budgets['summary']['comparable_variance']), D(want['budget_variance']))
        for row in budgets['rows']:
            self.assertEqual(D(row['variance']), D(want['objects'][row['group_label']]['variance']))
        view = result['business_views']
        required_count = {'pharma': 20, 'restaurant': 61, 'medical_devices': 18}[bundle['industry']]
        if want['variant'] == 'large':
            required_count *= 5 if bundle['industry'] == 'restaurant' else 6
        required = view['required_costs']
        self.assertEqual(required['status'], 'satisfied_for_declared_requirements')
        self.assertEqual(required['summary'], dict(required_count=required_count, observed_count=required_count,
                                                   explicit_zero_count=0, missing_count=0))
        self.assertTrue(all(row['source_refs'] for row in required['rows']))
        for key in ('asset_total', 'pnl_cost', 'period_expense', 'service_cost'):
            self.assertEqual(D(view[key]), D(want[key]), key)
        self.assertEqual(D(view['asset_total']) + D(view['pnl_cost']), D(want['total_cost']))
        actual_life = {r['object_id']: r for r in view['lifecycle']}
        self.assertEqual(set(actual_life), set(want['lifecycle']))
        for oid, values in want['lifecycle'].items():
            for field, amount in values.items():
                self.assertEqual(D(actual_life[oid][field]), D(amount), oid + ':' + field)
            self.assertEqual(actual_life[oid]['denominator_status'], 'provided')
        if 'settlement' in want:
            settlement = view['settlement']
            for key, value in want['settlement']['totals'].items():
                self.assertEqual(D(settlement['totals'][key]), D(value), key)
            for row in settlement['rows']:
                for key, value in want['settlement']['rows'][row['object_id']].items():
                    self.assertEqual(D(row[key]), D(value))
            self.assertEqual({row['object_id'] for row in settlement['rows']}, set(want['settlement']['rows']))
        if 'menu_dimensions' in want:
            self.assertEqual(set(want['menu_dimensions']), set(actual_life))
            self.assertEqual(len({(r['store'], r['channel'], r['dish']) for r in want['menu_dimensions'].values()}),
                             8 if want['variant'] == 'small' else 40)
            self.assertEqual({r['dish'] for r in want['menu_dimensions'].values()}, {'rice', 'noodles'})
            self.assertEqual({r['channel'] for r in want['menu_dimensions'].values()}, {'dine', 'delivery'})
            hq = {a['to_object']: D(a['amount']) for a in result['allocations'] if a['pool_id'] == 'shared:head_office'}
            self.assertEqual(hq, {target: D(r['amount']) for target, r in want['head_office_allocation'].items()})
            self.assertEqual(sum(hq.values()), D(want['cost_groups']['head_office']))
            self.assertTrue(all(want['objects'][oid]['classification'] == 'period' for oid in hq))
            self.assertTrue(all('head_office' not in objs[oid]['cost_structure'] for oid in actual_life))
        if 'inventory' in want:
            inv = result['recognition']['inputs']['stock']['inventory_valuation']
            for key in ('available_value', 'net_cogs', 'shrink', 'closing_value', 'reconciliation_difference'):
                self.assertEqual(D(inv['outputs'][key]), D(want['inventory'][key]))
            self.assertEqual(D(inv['transfers']['closed_value']), D(want['inventory']['closed_transfer_value']))
            self.assertTrue(all(len(r['source_refs']) == 2 for r in inv['transfers']['closed_pairs']))
        if 'raw_materials' in want:
            materials = want['raw_materials']
            ledger = result['recognition']['inputs']['raw_materials']
            valuation = ledger['inventory_valuation']
            output = valuation['outputs']
            monetary_fields = ('available_value', 'net_production_consumption', 'closing_value')
            for key in monetary_fields:
                self.assertEqual(D(output[key]), D(materials[key]))
            self.assertEqual(D(output['available_value']), D(output['net_production_consumption']) + D(output['closing_value']))
            self.assertEqual(D(output['net_cogs']), 0)
            self.assertEqual(D(output['shrink']), 0)
            self.assertEqual(D(ledger['ledger_recognized_total']), D(output['net_production_consumption']))
            self.assertEqual(len(valuation['streams']), len(materials['streams']))
            for stream in valuation['streams']:
                issue, returned = stream['cost_postings']
                oid = issue['production_object_id']
                self.assertEqual(returned['production_object_id'], oid)
                self.assertEqual(returned['original_issue_id'], issue['event_id'])
                self.assertGreater(D(issue['amount']), 0)
                self.assertLess(D(returned['amount']), 0)
                for key in (*monetary_fields, 'closing_quantity'):
                    self.assertEqual(D(stream['outputs'][key]), D(materials['streams'][oid][key]))
                self.assertEqual(D(stream['outputs']['net_production_quantity']), D(materials['streams'][oid]['net_quantity']))
                self.assertEqual(D(issue['amount']) + D(returned['amount']), D(objs[oid]['cost_structure'][issue['cost_element']]))
                self.assertEqual(len({ref['selector'] for ref in returned['source_refs'] if ref['kind'] == 'raw' and ref['field'] == 'quantity'}), 2)
        if 'dispositions' in want:
            guard = view['dispositions']
            self.assertEqual(guard['evidence_scope'], want['dispositions']['evidence_scope'])
            self.assertEqual(guard['identity_basis'], want['dispositions']['identity_basis'])
            self.assertEqual(guard['counts'], want['dispositions']['counts'])
        manifest = {r['name']: r for r in result['recognition']['source_bundle']['sources']}
        self.assertEqual(set(manifest), set(bundle['files']))
        for name, text in bundle['files'].items():
            self.assertEqual(manifest[name]['sha256'], hashlib.sha256(text.encode()).hexdigest())
            self.assertEqual(manifest[name]['row_count'], len(list(csv.DictReader(io.StringIO(text)))))
        self.assertEqual({r['stage'] for r in result['allocations']}, {1, 2})

    def test_three_small_scopes_against_independent_cents_and_quantities(self):
        for name in INDUSTRIES:
            with self.subTest(industry=name):
                b = build(name); start = time.perf_counter(); result = evaluate(b)
                self.assert_oracle(b, result)
                print(json.dumps(dict(industry=name, variant='small', total=b['expected']['total_cost'],
                                      source_rows=b['expected']['source_rows'], elapsed_s=round(time.perf_counter() - start, 4))))

    def test_three_varied_large_batches_and_all_source_manifests(self):
        for name in INDUSTRIES:
            with self.subTest(industry=name):
                b = build(name, True); start = time.perf_counter(); result = evaluate(b)
                self.assertGreater(sum(b['expected']['source_rows'].values()), 3000)
                self.assertGreater(len(set(r['amount'] for r in csv.DictReader(io.StringIO(b['files']['finance'])))), 80)
                self.assert_oracle(b, result)
                print(json.dumps(dict(industry=name, variant='large', total=b['expected']['total_cost'],
                                      source_rows=b['expected']['source_rows'], elapsed_s=round(time.perf_counter() - start, 4))))

    def test_same_revision_reopen_json_markdown_and_original_file_rows(self):
        for name in INDUSTRIES:
            with self.subTest(industry=name), TemporaryDirectory() as folder:
                root = Path(folder); b = build(name); schema = make_schema(b); result = evaluate(b, schema)
                ds = root / 'costing_datasets' / schema['dataset_id']; (ds / 'sources').mkdir(parents=True)
                for source, text in b['files'].items():
                    (ds / 'sources' / (source + '.csv')).write_text(text, encoding='utf-8', newline='')
                (ds / 'manifest.json').write_text(json.dumps({'schema': schema}), encoding='utf-8')
                snap = create_snapshot(result, root, root, dict(dataset_id=schema['dataset_id'], period='2026-11', scenario='actual',
                                                               owner_external_id='synthetic-triad-account', industry=name))
                old = load_snapshot(snap['analysis_id'], root)
                self.assertEqual(old['revision'], snap['revision'])
                self.assertEqual(old['result'], result)
                sources = {e['source_uri'] for e in old['evidence'] if e['status'] == 'captured'}
                self.assertTrue(all(e['status'] in {'captured','captured_mapping','synthetic','derived'}
                                    for e in old['evidence']),
                                [(e['selector'],e['status']) for e in old['evidence'] if e['status']=='selector_not_found'])
                self.assertEqual(sources, {'upload://sources/' + f + '.csv' for f in b['files']})
                for evidence in old['evidence']:
                    if evidence['status'] == 'captured':
                        self.assertEqual(len(evidence['rows']), 1)
                        source = evidence['source_uri'].split('/')[-1][:-4]
                        reader = list(csv.DictReader(io.StringIO(b['files'][source])))
                        original = evidence['rows'][0]
                        self.assertEqual(original['values'], reader[original['record'] - 2])
                out = root / 'exports'
                self.assertEqual(json.loads(export_snapshot(old, 'json', out).read_text(encoding='utf-8')), old)
                self.assertEqual(export_snapshot(old, 'md', out).read_text(encoding='utf-8'), old['report']['markdown'])
                self.assertIn(format(D(b['expected']['total_cost']), ',.2f'), old['report']['markdown'])
                with self.assertRaises(FileNotFoundError): load_snapshot(snap['analysis_id'], root / 'other-account')
                # Persisted revision integrity is a core check. Wrong requested
                # HTTP revisions are checked separately at the API boundary.
                stored = root / 'costing_snapshots' / (snap['analysis_id'] + '.json')
                corrupted = json.loads(stored.read_text(encoding='utf-8'))
                corrupted['revision'] = '0' * 64
                stored.write_text(json.dumps(corrupted), encoding='utf-8')
                with self.assertRaises(ValueError): load_snapshot(snap['analysis_id'], root)

    def test_each_industry_rejects_missing_money_units_duplicates_aliases_returns_and_tamper(self):
        for name in INDUSTRIES:
            for case in ('missing_money', 'unit', 'duplicate', 'alias', 'return_excess', 'period', 'tamper', 'zero_driver', 'budget_membership'):
                with self.subTest(industry=name, case=case), self.assertRaises((ValueError, ArithmeticError)):
                    b = build(name); s = make_schema(b)
                    if case == 'missing_money':
                        def edit(rows): rows[0].update(amount='', status='missing'); return rows
                        alter(b, s, 'finance', edit)
                    elif case == 'unit':
                        def edit(rows): rows[0]['unit'] = 'wrong_unit'; return rows
                        alter(b, s, 'events', edit)
                    elif case in ('duplicate', 'alias'):
                        def edit(rows):
                            extra = dict(rows[0])
                            if case == 'alias': extra['id'] = ''.join(chr(ord(c) + 0xFEE0) if '!' <= c <= '~' else c for c in extra['id'])
                            rows.append(extra); return rows
                        alter(b, s, 'finance', edit)
                    elif case == 'return_excess':
                        def edit(rows): next(r for r in rows if r['op'] == 'return')['quantity'] = '999999'; return rows
                        alter(b, s, 'events', edit)
                    elif case == 'period':
                        def edit(rows): rows[0]['period'] = '2026-12'; return rows
                        alter(b, s, 'events', edit)
                    elif case == 'tamper':
                        b['files']['finance'] = b['files']['finance'].replace('600.00', '600.01', 1) + '\n'
                    elif case == 'zero_driver':
                        rule = s['raw_composite']['allocation_rules'][-1]
                        rule['weights'] = {key: 0 for key in rule['weights']}
                    else:
                        s['raw_composite']['object_budgets']['rows'][0]['membership_sha256'] = '0' * 64
                    evaluate(b, s)

    def test_missing_and_explicit_zero_denominator_are_distinct_without_unit_cost(self):
        for name in INDUSTRIES:
            for missing in (False, True):
                with self.subTest(industry=name, missing=missing):
                    b = build(name); s = make_schema(b)
                    oid = next(o['id'] for o in b['descriptor']['objects'] if o['classification'] == 'production')
                    def edit(rows):
                        out = []
                        for row in rows:
                            if row['object_id'] == oid and row['op'] in ('sale', 'return'): continue
                            if row['object_id'] == oid and row['op'] == 'complete':
                                if missing: continue
                                row['quantity'] = '0'
                            out.append(row)
                        return out
                    alter(b, s, 'events', edit)
                    if 'dispositions' in b['files']:
                        # This valid zero-completion scenario removes the
                        # original sale/return, so remove its unit controls as
                        # well; dangling controls correctly fail the guard.
                        alter(b, s, 'dispositions', lambda rows: [r for r in rows if not (r['object_id'] == oid and r['action'] == 'return')])
                    view = next(r for r in evaluate(b, s)['business_views']['lifecycle'] if r['object_id'] == oid)
                    self.assertIsNone(view['qualified_unit_cost'])
                    self.assertEqual(view['denominator_status'], 'missing' if missing else 'explicit_zero')

    def test_restaurant_missing_zero_fee_evidence_and_duplicated_commission_are_rejected(self):
        for case in ('missing', 'missing_group', 'double_commission', 'excess_refund', 'wrong_transfer'):
            with self.subTest(case=case), self.assertRaises(ValueError):
                b = build('restaurant'); s = make_schema(b)
                if case == 'missing':
                    def edit(rows): return [r for r in rows if not (r['object_id'] == 'store-1-dine-settlement' and r['op'] == 'commission')]
                    alter(b, s, 'settlements', edit)
                elif case == 'missing_group':
                    alter(b, s, 'settlements', lambda rows: [r for r in rows if r['object_id'] != 'store-1-dine-settlement'])
                elif case == 'wrong_transfer':
                    def edit(rows): next(r for r in rows if r['kind'] == 'transfer_in')['quantity'] = '4'; return rows
                    alter(b, s, 'stock', edit)
                else:
                    def edit(rows):
                        row = next(r for r in rows if r['op'] == ('commission' if case == 'double_commission' else 'refund'))
                        row['amount'] = '999999.00'; return rows
                    alter(b, s, 'settlements', edit)
                evaluate(b, s)

    def test_scope_notices_preserve_unimplemented_pos_serial_and_recall_boundaries(self):
        required = {'pharma': 'capitalization_policy_external', 'restaurant': 'native_pos_unimplemented',
                    'medical_devices': 'recall_basis_missing'}
        for name, code in required.items():
            r = evaluate(build(name))
            self.assertIn(code, {n['code'] for n in r['business_views']['notices']})
            self.assertFalse(r['recognition']['can_claim_complete'])

    def test_missing_required_cost_stays_unknown_when_another_object_has_same_category(self):
        cases = {
            'pharma': ('batch-a', 'batch-b', 'materials'),
            'restaurant': ('store-1-dine-food-rice', 'store-1-dine-food-noodles', 'ingredients_consumed'),
            'medical_devices': ('device-batch-a', 'device-batch-b', 'parts'),
        }
        for name, (missing_obj, observed_obj, tag) in cases.items():
            with self.subTest(industry=name):
                b = build(name); s = make_schema(b)
                alter(b, s, 'raw_materials', lambda rows: [r for r in rows if r['production_object_id'] != missing_obj])
                result = evaluate(b, s)
                required = result['business_views']['required_costs']
                self.assertTrue(result['ledger_reconciled'])
                self.assertFalse(result['recognition']['can_claim_complete'])
                self.assertEqual(required['status'], 'incomplete')
                self.assertEqual(required['summary']['missing_count'], 1)
                self.assertEqual(required['missing'], [dict(object_id=missing_obj, element_id=tag)])
                rows = {(r['object_id'], r['element_id']): r for r in required['rows']}
                self.assertEqual(rows[missing_obj, tag]['status'], 'missing')
                self.assertIsNone(rows[missing_obj, tag]['amount'])
                self.assertEqual(rows[observed_obj, tag]['status'], 'observed')
                self.assertGreater(D(rows[observed_obj, tag]['amount']), 0)

    def test_each_industry_rejects_invalid_production_material_returns(self):
        for name in INDUSTRIES:
            for scenario in ('excess_quantity', 'wrong_batch', 'missing_original', 'duplicated_economic_key'):
                with self.subTest(industry=name, scenario=scenario), self.assertRaisesRegex(ValueError, 'inventory|economic'):
                    bundle = build(name)
                    schema = make_schema(bundle)

                    def invalidate(rows):
                        returned = next(row for row in rows if row['kind'] == 'production_return')
                        if scenario == 'excess_quantity':
                            returned['quantity'] = '999999'
                        elif scenario == 'wrong_batch':
                            returned['production_object_id'] = next(row['production_object_id'] for row in rows
                                if row['production_object_id'] and row['production_object_id'] != returned['production_object_id'])
                        elif scenario == 'missing_original':
                            returned['original_issue_id'] = 'nonexistent-material-issue'
                        else:
                            returned['economic_key'] = next(row['economic_key'] for row in rows if row['id'] == returned['original_issue_id'])
                        return rows

                    alter(bundle, schema, 'raw_materials', invalidate)
                    evaluate(bundle, schema)

    def test_medical_declared_unit_dispositions_reject_cross_action_reuse_and_missing_controls(self):
        for first, second in [('rework', 'scrap'), ('rework', 'return'), ('scrap', 'return')]:
            for alias in (False, True):
                with self.subTest(first=first, second=second, alias=alias), self.assertRaises(ValueError):
                    b = build('medical_devices'); s = make_schema(b)
                    def edit(rows):
                        original = next(r for r in rows if r['action'] == first)
                        duplicate = next(r for r in rows if r['action'] == second and r['object_id'] == original['object_id'])
                        value = original['item_id']
                        duplicate['item_id'] = ''.join(chr(ord(c) + 0xFEE0) if '!' <= c <= '~' else c for c in value) if alias else value
                        return rows
                    alter(b, s, 'dispositions', edit)
                    evaluate(b, s)
        for case in ('missing_return_control', 'wrong_ref', 'blank_identity_basis', 'wrong_rework_tag'):
            with self.subTest(case=case), self.assertRaises(ValueError):
                b = build('medical_devices'); s = make_schema(b)
                if case == 'missing_return_control':
                    def edit(rows):
                        victim = next(r for r in rows if r['action'] == 'return')
                        return [r for r in rows if r is not victim]
                    alter(b, s, 'dispositions', edit)
                elif case == 'wrong_ref':
                    def edit(rows): rows[0]['ref'] = 'EV-device-batch-b-return'; return rows
                    alter(b, s, 'dispositions', edit)
                elif case == 'blank_identity_basis':
                    s['business_views']['dispositions']['identity_basis'] = ''
                else:
                    s['business_views']['dispositions']['action_tags']['rework'] = 'parts'
                evaluate(b, s)


if __name__ == '__main__':
    unittest.main()
