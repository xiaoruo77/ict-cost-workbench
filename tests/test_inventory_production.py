"""Independent cents and quantity oracles for approved batch consumption."""
import copy
from decimal import Decimal as D
from urllib.parse import parse_qsl
import unittest

from cost_core.inventory_eval import PRODUCTION_POLICY, run_document
from test_inventory_adapter import convert, fixture
from test_inventory_partition import go
from cost_core.csv_adapter import CsvCostingAdapter


ELEMENT = dict(object_kind='product', cost_element='materials', cost_type='direct', behavior='variable')


def document():
    raw = fixture()
    scope = {k: raw['context'][k] for k in ('book','warehouse','item','uom','currency')}
    p = raw['context']['period']
    opening = raw['opening']
    opening.update(quantity='0', value='0.00', status='evidenced_zero', evidence='verified opening',
                   issues=[], seen_event_ids=[], seen_source_ids=[])
    raw['production_policy'] = PRODUCTION_POLICY

    def row(day, identity, kind, quantity, **details):
        return dict(scope, id=identity, source_id='raw-'+identity, at=f'{p}-{day:02}T10:00:00Z',
                    sequence=1, kind=kind, quantity=quantity, evidence='approved '+identity, **details)

    raw['transactions'] = [
        row(1, 'purchase', 'receipt', '16', receipt_value='80.00'),
        row(2, 'issue', 'production_issue', '10', production_object_id='batch-1', economic_key='issue-economic'),
        row(3, 'return', 'production_return', '2', production_object_id='batch-1', economic_key='return-economic',
            original_issue_id='issue', restock_status='restockable'),
        row(4, 'sale', 'sale_issue', '2'),
    ]
    return raw


def convert_production(raw, mappings=None):
    text, schema = convert(raw)
    schema['raw_inventory']['production_posting_map'] = mappings or {'batch-1': ELEMENT}
    return text, schema


class InventoryProductionTests(unittest.TestCase):
    def test_issue_return_and_sale_use_separate_ledgers_and_preserve_original_links(self):
        raw = document()
        req = CsvCostingAdapter().from_text(*convert_production(raw))
        result = req.recognition['inventory_valuation']
        self.assertEqual(result['outputs'], {
            'available_value':'80.00', 'net_cogs':'10.00', 'shrink':'0.00', 'closing_value':'30.00',
            'closing_quantity':'6', 'reconciliation_difference':'0.00',
            'net_production_consumption':'40.00', 'net_production_quantity':'8',
        })
        self.assertEqual([(entry.object_id,entry.element_id,entry.amount) for entry in req.entries],
                         [('batch-1','materials',D(50)),('batch-1','materials',D(-10)),('SKU_1','cogs',D(10))])
        original = {dict(parse_qsl(ref.selector)).get('source_id') for ref in req.entries[1].source_refs}
        self.assertTrue({'raw-issue','raw-return'} <= original)
        self.assertIn('raw_inventory.production_posting_map',[ref.selector for ref in req.entries[1].source_refs])
        self.assertEqual(req.recognition['ledger_recognized_total'],'50.00')
        self.assertEqual([row['id'] for row in result['next_state']['issues']], ['sale'])

    def test_partial_returns_recover_original_minor_units_then_current_weighted_average(self):
        raw = document(); purchase, issued, returned, _ = raw['transactions']
        purchase.update(quantity='3', receipt_value='0.10'); issued['quantity']='3'; returned['quantity']='1'
        last = dict(returned, id='return2', source_id='raw-return2', economic_key='return2-economic',
                    at=returned['at'].replace('-03T','-04T'), quantity='2')
        raw['transactions'] = [purchase, issued, returned, last]
        result = run_document(raw)
        self.assertEqual(result['status'],'valid')
        self.assertEqual([row['amount'] for row in result['cost_postings']], ['0.10','-0.03','-0.07'])
        self.assertEqual(result['outputs']['net_production_consumption'],'0.00')
        self.assertEqual(result['outputs']['closing_value'],'0.10')
        self.assertEqual(result['outputs']['closing_quantity'],'3')

    def test_wrong_batch_type_reference_over_return_no_policy_and_alias_fail(self):
        for change in ('wrong-batch','over-return','sale-as-issue','production-as-sale','forward','economic-alias','no-policy'):
            raw = document(); rows = raw['transactions']
            if change == 'wrong-batch': rows[2]['production_object_id']='another'
            if change == 'over-return': rows[2]['quantity']='11'
            if change == 'sale-as-issue': rows[2]['original_issue_id']='sale'
            if change == 'production-as-sale':
                rows[2]['kind']='sale_return'; rows[2].pop('production_object_id');rows[2].pop('economic_key')
            if change == 'forward': rows[2]['original_issue_id']='not-yet-issued'
            if change == 'economic-alias': rows[2]['economic_key']='ＩＳＳＵＥ-ＥＣＯＮＯＭＩＣ'
            if change == 'no-policy': raw.pop('production_policy')
            with self.subTest(change=change):
                invalid = run_document(raw)
                self.assertEqual(invalid['status'],'invalid')
                self.assertEqual(invalid['outputs'],{})

    def test_two_raw_item_streams_feed_same_batch_and_closing_inventory_is_separate(self):
        raw = document(); op = {k:raw[k] for k in ('operation','policy','production_policy')}
        op['context'] = {k:raw['context'][k] for k in ('book','period','currency','cutoff_at')}
        op['streams'] = []; rows = []
        for n in range(2):
            scope = {'warehouse':raw['context']['warehouse'], 'item':f'raw-item-{n}', 'uom':f'unit-{n}'}
            target = dict(ELEMENT, object_id=f'retail-{n}')
            op['streams'].append(dict(scope=scope, opening=dict(raw['opening'],**scope),
                                      posting_map={key:dict(target,cost_element=key) for key in ('cogs','shrink')},
                                      production_posting_map={'batch-1': ELEMENT}))
            for move in raw['transactions'][:3]:
                row = dict(move, **scope, id=f'{move["id"]}-{n}', source_id=f'{move["source_id"]}-{n}')
                for key in ('economic_key','original_issue_id'):
                    if key in row: row[key] += f'-{n}'
                rows.append(row)
        req, result = go(op, rows)
        balance = result['recognition']['inventory_valuation']['outputs']
        self.assertEqual({k: balance[k] for k in ('available_value','net_production_consumption','closing_value')},
                         {'available_value':'160.00','net_production_consumption':'80.00','closing_value':'80.00'})
        self.assertNotIn('net_production_quantity',balance)
        self.assertEqual({entry.object_id for entry in req.entries},{'batch-1'})

    def test_unmapped_batch_fails_and_empty_consumption_is_not_an_entry(self):
        raw = document()
        with self.assertRaisesRegex(ValueError,'undeclared production batch'):
            CsvCostingAdapter().from_text(*convert_production(raw,{'different':ELEMENT}))
        raw['transactions'] = raw['transactions'][:1]
        req = CsvCostingAdapter().from_text(*convert_production(raw))
        self.assertEqual(req.entries,())
        self.assertEqual(req.recognition['inventory_valuation']['outputs']['net_production_consumption'],'0.00')


if __name__ == '__main__':
    unittest.main()
