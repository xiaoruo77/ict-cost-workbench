import copy
import csv
import io
import unittest
from decimal import Decimal as D
from cost_core.business_views import compile_business_views, evaluate_business_views
from cost_core.costing import CostingEngine


def text(rows):
    f=io.StringIO(); w=csv.DictWriter(f,fieldnames=list(rows[0]));w.writeheader();w.writerows(rows);return f.getvalue()


def fixture():
    scope=dict(book='SYNTHETIC',period='2026-11',currency='CNY')
    rows=[dict(id=i,object_id='B',op=op,quantity=q,unit='unit',ref=ref,**scope)
          for i,op,q,ref in [('s','start','100',''),('c','complete','80',''),('x','scrap','5',''),('a','sale','60',''),('r','return','10','a')]]
    cfg=dict(version=1,policy_source='synthetic approved homogeneous policy',classifications={'B':'production','P':'period'},lifecycle=dict(file='events',columns={k:k for k in ('id','object_id','op','quantity','unit','ref')},units={'B':'unit'}),notices=[])
    schema=dict(dataset_id='csv-0123456789abcdef',raw_composite=dict(scope=scope),business_views=cfg)
    result=dict(objects=[dict(object_id='B',total_cost='1000.00',source_refs=[],cost_components=[],cost_structure={'materials':'1000'}),dict(object_id='P',total_cost='100.00',source_refs=[],cost_components=[],cost_structure={'commission':'20','payment_fee':'1','labor':'79'})],totals=dict(total_cost='1100.00'))
    return schema,rows,result


class BusinessViewsTests(unittest.TestCase):
    def test_independent_quantity_and_money_bridge(self):
        schema,rows,result=fixture()
        plan=compile_business_views({'events':text(rows)},schema)
        out=evaluate_business_views(plan,result,CostingEngine._apportion)
        b=out['lifecycle'][0]
        self.assertEqual([D(b[k]) for k in ('wip','finished_inventory','cost_of_sales','scrap_loss')],list(map(D,['150','300','500','50'])))
        self.assertEqual(D(b['qualified_unit_cost']),10)
        self.assertEqual(D(out['asset_total']),450)
        self.assertEqual(D(out['pnl_cost']),650)
        self.assertEqual(b['source_refs'][0]['source_uri'],'upload://sources/events.csv')
        self.assertIn('row=2',b['source_refs'][0]['selector'])
        self.assertEqual(result['totals']['total_cost'],'1100.00')

    def test_zero_completion_is_not_unit_cost_zero(self):
        schema,rows,result=fixture()
        p=compile_business_views({'events':text(rows[:1])},schema)
        out=evaluate_business_views(p,result,CostingEngine._apportion)
        self.assertIsNone(out['lifecycle'][0]['qualified_unit_cost'])
        self.assertEqual(D(out['asset_total']),1000)

    def test_localized_failure_cases(self):
        for case in ('unit','duplicate','alias','scope','missing','overreturn','missing_ref','oversale','doubledisposal'):
            with self.subTest(case=case), self.assertRaises(ValueError):
                s,rows,r=fixture()
                if case=='unit':rows[0]['unit']='kg'
                elif case=='duplicate':rows.append(dict(rows[0]))
                elif case=='alias':rows.append(dict(rows[0],id='Ｓ'))
                elif case=='scope':rows[0]['period']='2026-12'
                elif case=='missing':rows[0]['quantity']=''
                elif case=='overreturn':rows[-1]['quantity']='61'
                elif case=='missing_ref':rows[-1]['ref']='unknown'
                elif case=='oversale':rows[-2]['quantity']='81'
                else:rows.append(dict(rows[2],id='again',quantity='20'))
                evaluate_business_views(compile_business_views({'events':text(rows)},s),r,CostingEngine._apportion)

    def test_minor_units_conserve_with_deterministic_residual(self):
        s,rows,r=fixture();r['objects'][0]['total_cost']='0.01';r['totals']['total_cost']='100.01'
        out=evaluate_business_views(compile_business_views({'events':text(rows)},s),r,CostingEngine._apportion)
        self.assertEqual(D(out['asset_total'])+D(out['pnl_cost']),D('100.01'))

    def test_settlement_separates_fees_revenue_and_cash(self):
        s,rows,r=fixture();scope=s['raw_composite']['scope']
        entries=[dict(id=i,object_id='P',op=op,amount=amt,ref=ref,**scope) for i,op,amt,ref in [('g','gross','200',''),('d','merchant_discount','10','g'),('p','platform_discount','5','g'),('f','refund','20','g'),('fee','commission','20','g'),('pay','payment_fee','1','g'),('cash','receipt','149','g')]]
        s['business_views']['settlement']=dict(file='settlements',columns={k:k for k in ('id','object_id','op','amount','ref')},commission_element='commission',payment_fee_element='payment_fee',merchant_discount_treatment='revenue_reduction',platform_discount_treatment='platform_compensated')
        files={'events':text(rows),'settlements':text(entries)}
        out=evaluate_business_views(compile_business_views(files,s),r,CostingEngine._apportion)
        t=out['settlement']['totals']
        self.assertEqual([D(t[k]) for k in ('net_revenue','expected_receipt','receipt_difference')],[D(170),D(149),D(0)])
        invalid = copy.deepcopy(s)
        invalid['business_views']['settlement']['payment_fee_element'] = 'ＣＯＭＭＩＳＳＩＯＮ'
        with self.assertRaisesRegex(ValueError, 'distinct cost elements'):
            compile_business_views(files, invalid)
        bad=copy.deepcopy(r);bad['objects'][1]['cost_structure']['commission']='40'
        with self.assertRaisesRegex(ValueError,'exactly one'):
            evaluate_business_views(compile_business_views(files,s),bad,CostingEngine._apportion)
        entries[3]['amount']='191';files['settlements']=text(entries)
        with self.assertRaisesRegex(ValueError,'excessive'):
            evaluate_business_views(compile_business_views(files,s),r,CostingEngine._apportion)
