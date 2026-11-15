import csv
import io
from urllib.parse import parse_qsl
import unittest

from cost_core.costing import CostingEngine
from cost_core.csv_adapter import CsvCostingAdapter
from cost_core.required_costs import compile_required_costs, check_required_costs


def fixture(*, include_second=False, mapped=False):
    scope = {'book': 'SYNTH', 'period': '2026-11', 'currency': 'CNY'}
    rows = [dict(**scope, id='materials-a', kind='expense', status='data', amount='60.00',
                 economic_key='materials-a', evidence='approved voucher 1', tag='materials', target_object_id='a')]
    if include_second:
        rows.append(dict(rows[0], id='materials-b', economic_key='materials-b', target_object_id='b',
                         status='evidenced_zero', evidence='approved zero voucher', amount='0.00'))
    headers = {'id': 'voucher', 'amount': 'booked_amount'} if mapped else {}
    stream = io.StringIO()
    writer = csv.DictWriter(stream, [headers.get(k, k) for k in rows[0]])
    writer.writeheader()
    writer.writerows({headers.get(k, k): v for k, v in row.items()} for row in rows)
    recognition = {'version': 2, 'name': 'test', 'context': scope, 'excluded_tags': {},
                   'rules': [{'id':'materials', 'op':'recognize', 'source_tag':'materials',
                              'source_kind':'expense', 'posting_pool':'production'}], 'outputs':['materials'],
                   'ledger_basis':'recognized_expenses_incurred',
                   'posting_map': {'materials': {'object_id_from':'target_object_id', 'object_kind':'product',
                                                 'record_type':'entry', 'cost_element':'materials',
                                                 'behavior':'variable', 'cost_type':'direct'}},
                   'target_objects': [{'object_id':i, 'object_kind':'product'} for i in ('a','b')]}
    schema = {'dataset_id': 'csv-1111111111111111', 'raw_composite': {'version':4, 'scope':scope,
               'inputs': {'finance': {'file':'finance', 'raw_recognition':recognition, 'columns':headers}}}}
    files = {'finance': stream.getvalue()}
    request = CsvCostingAdapter().from_files(files, schema)
    schema['business_views'] = {'classifications': {'a':'production', 'b':'production'},
                                'required_costs': {'a':['materials'], 'b':['materials']}}
    return files, schema, request


class RequiredCostTests(unittest.TestCase):
    def test_same_cost_tag_in_other_batch_cannot_fill_missing_data(self):
        files, schema, request = fixture()
        plan = compile_required_costs(files, schema, recognized_entries=request.entries)
        report = check_required_costs(plan, CostingEngine().analyze(request)['objects'])
        self.assertEqual(report['status'], 'incomplete')
        self.assertEqual(report['missing'], [{'object_id':'b','element_id':'materials'}])
        self.assertEqual([(r['status'],r['amount']) for r in report['rows']], [('observed','60.00'),('missing',None)])

    def test_raw_recognition_zero_survives_component_netting_in_coverage(self):
        files, schema, _ = fixture(include_second=True, mapped=True)
        schema['business_views'].update(version=1, policy_source='synthetic period data policy',
                                        classifications={'a':'period', 'b':'period'})
        request = CsvCostingAdapter().from_files(files, schema)
        result = CostingEngine().analyze(request)
        self.assertEqual(next(obj for obj in result['objects'] if obj['object_id'] == 'b')['cost_components'], [])
        report = result['business_views']['required_costs']
        self.assertEqual(report['status'], 'satisfied_for_declared_requirements')
        zero = report['rows'][1]
        self.assertEqual((zero['status'], zero['amount']), ('explicit_zero','0.00'))
        ref = next(item for item in zero['source_refs'] if item['kind'] == 'raw')
        self.assertEqual((ref['source_uri'], ref['field']), ('upload://sources/finance.csv','booked_amount'))
        self.assertEqual(dict(parse_qsl(ref['selector'])), {'row':'3', 'voucher':'materials-b'})

    def test_zero_component_and_unrecognized_zero_file_do_not_create_zero_evidence(self):
        files, schema, request = fixture(include_second=True)
        result = CostingEngine().analyze(request)
        row = next(obj for obj in result['objects'] if obj['object_id'] == 'b')
        row['cost_components'] = [{'element_id':'materials','amount':'0.00','source_refs':[]}]
        plan = compile_required_costs(files, schema)
        self.assertEqual(check_required_costs(plan, [row])['rows'][0]['status'], 'missing')
        files['finance'] = files['finance'].replace('approved zero voucher', '')
        plan = compile_required_costs(files, schema, recognized_entries=request.entries)
        self.assertEqual(check_required_costs(plan, [row])['rows'][0]['status'], 'missing')

    def test_offsetting_cost_components_are_observed_and_not_an_explicit_zero(self):
        files, schema, _ = fixture()
        plan = compile_required_costs(files, schema)
        obj = {'object_id':'a','cost_components': [
            {'element_id':'materials','amount':amount,'source_refs':[]} for amount in ('10.00','-10.00')]}
        row = check_required_costs(plan, [obj])['rows'][0]
        self.assertEqual((row['status'], row['amount']), ('observed','0.00'))

    def test_absent_configuration_and_invalid_object_or_aliased_element(self):
        self.assertIsNone(compile_required_costs({}, {}))
        self.assertIsNone(check_required_costs(None, []))
        files, schema, _ = fixture()
        for requirements in ({'unknown':['materials']},{'a':['materials','ＭＡＴＥＲＩＡＬＳ']},{'a':[]}):
            schema['business_views']['required_costs'] = requirements
            with self.subTest(requirements=requirements), self.assertRaises(ValueError):
                compile_required_costs(files, schema)


if __name__ == '__main__':
    unittest.main()
