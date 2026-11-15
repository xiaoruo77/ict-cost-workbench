import copy
import unittest
from decimal import Decimal
from cost_core.csv_adapter import CsvCostingAdapter
from cost_core.costing import CostingEngine
from test_composite_adapter import text
from test_meter_adapter import config, line


def fixture(second_account):
    files = {}; inputs = {}
    for name, account in [('one', 'A'), ('two', second_account)]:
        rule = config(account=account)
        for target in rule['posting_map'].values():
            target['object_id'] = name
        files[name] = text([line(rule, 150000, id=name)])
        inputs[name] = {'file': name, 'raw_meter': rule}
    scope = {k: v for k, v in rule['scope'].items() if k in {'book', 'period', 'currency'}}
    scope['period'] = '2026-11'
    return files, {'period': '2026-11', 'raw_composite': {'version': 4, 'scope': scope, 'inputs': inputs}}


class BundleMeterIdentityTests(unittest.TestCase):
    def test_repeated_account_and_unicode_alias_cannot_double_bill(self):
        for account in ['A', 'a', 'Ａ']:
            with self.subTest(account=account), self.assertRaisesRegex(ValueError, 'billing account'):
                CsvCostingAdapter().from_files(*fixture(account))

    def test_separate_declared_billing_accounts_remain_independent(self):
        files, schema = fixture('B')
        result = CostingEngine().analyze(CsvCostingAdapter().from_files(files, schema))
        self.assertEqual(Decimal(result['totals']['total_cost']), Decimal('1300'))
        self.assertEqual({row['object_id']: Decimal(row['total_cost']) for row in result['objects']},
                         {'one': Decimal('650'), 'two': Decimal('650')})
