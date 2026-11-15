import calendar,json,tempfile,unittest
from datetime import date,timedelta
from decimal import Decimal as D
from pathlib import Path
from test_foreign_booked import fixture
from test_inventory_transfer import case
from test_composite_adapter import evaluate,text
from costing_outputs import create_snapshot,load_snapshot
from cost_core.object_budgets import membership_sha256
from costing_monthly_guard import MonthlyBindingClaim as Claim,MonthlyBindingPair as Pair,MonthlyRevisionPin as Pin,guarded_monthly_preview,MonthlyPreviewUnavailable
from costing_object_summary import project_object_only,verify_object_only_summary
OWNER='synthetic-authenticated-user'
def run(root,m,ch,debit):
 s,r=fixture(); e=s['raw_composite']; book=e['scope']['book']; i,rows=case(); v=i['raw_inventory']; v['context']['book']=book
 for t in v['streams']:t['opening']['book']=book
 for t in rows:t.update(book=book,input_name='stock',posting_identity=t['id'])
 e['inputs']['stock']={'raw_inventory':v}
 r=[x for x in r if x['input_name']!='stock']+rows[::-1]
 next(x for x in r if x['id']=='rent').update(amount=debit,fx_booked_amount=debit)
 group=dict(id='cold-chain',label='冷链',members=[dict(input_name=b,object_id=o) for b,o in [('period','A'),('stock','WH_C'),('power','ENERGY_E'),('water','ENERGY_W')]])
 e['object_rollups']=[group]
 if m!='2026-11':
  y,mo=map(int,m.split('-')); back=(date(y,mo,1)-timedelta(days=1)).isoformat()
  ends=f'{m}-{calendar.monthrange(y,mo)[1]}'
  def shift(x):
   if type(x)is dict:return {k:shift(z) for k,z in x.items()}
   if type(x)is list:return [shift(z) for z in x]
   if type(x)is str:return x.replace('2026-10-31',back).replace('2026-11-30',ends).replace('2026-11',m) if x.startswith('2026-11') else x.replace('2026-10-31',back)
   return x
  s,r=shift(s),shift(r);e=s['raw_composite']
 s['dataset_id']='csv-'+ch*16
 e['object_budgets']=dict(version=1,**e['scope'],rows=[dict(group_id='cold-chain',amount='1850.00',status='budgeted',source_id='synthetic-plan',membership_sha256=membership_sha256(e['scope'],group['id'],group['members']))])
 out=evaluate(s,r)[1]; rec=out['recognition']; stock=rec['inputs']['stock']['inventory_valuation']; fx=rec['inputs']['period']['foreign_booked_trace']
 expected=('1885.72','1871.44','21.44','14.28') if debit=='71.45' else ('1925.72','1901.44','51.44','24.28')
 assert [D(out['totals']['total_cost']),D(rec['object_rollups']['grouped_total']),D(rec['object_budgets']['summary']['comparable_variance']),D(rec['object_rollups']['ungrouped_total'])]==list(map(D,expected))
 assert stock['outputs']['net_cogs']=='34.29' and stock['outputs']['closing_value']=='265.71'
 assert [x['amount'] for x in stock['transfers']['closed_pairs']]==['40.00','51.43']
 assert rec['inputs']['power']['meter_billing']['unit']=='kWh' and rec['inputs']['water']['meter_billing']['unit']=='m³'
 assert {(x['origin_currency'],x['booked_amount']) for x in fx['rows']}=={('EUR','-14.32'),('USD',debit)}
 ds=root/'costing_datasets'/s['dataset_id'];ds.mkdir(parents=True)
 (ds/'data.csv').write_text(text(r));(ds/'manifest.json').write_text(json.dumps({'schema':s}))
 p=create_snapshot(out,root,root,dict(dataset_id=s['dataset_id'],period=m,scenario='actual',owner_external_id=OWNER))
 q=load_snapshot(p['analysis_id'],root);assert q['revision']==p['revision']
 keys={(x['field'],x['selector']) for x in q['evidence'] if x['status']=='captured'}
 assert all((f['field'],f['selector']) in keys for x in fx['rows'] for f in x['source_refs'])
 return q,book
def pin(s):return Pin(s['analysis_id'],s['revision'])
def check(root):
 prior,b=run(root,'2026-11','a','71.45');current,_=run(root,'2026-12','b','111.45')
 a,z=pin(prior),pin(current)
 class Authority:
  def load_pair(self,**kw):
   assert kw==dict(expected_owner=OWNER,current=z,previous=a)
   return Pair(*(Claim(OWNER,p,'synthetic-book-registry',b,'a'*64) for p in (z,a)))
 args=dict(current_snapshot=current,previous_snapshot=prior,current_pin=z,previous_pin=a,expected_owner=OWNER,authority=Authority())
 change=guarded_monthly_preview(**args)
 assert change['comparable_variance']=='30.00' and not change['can_claim_complete']
 try:guarded_monthly_preview(**{**args,'expected_owner':'foreign'})
 except MonthlyPreviewUnavailable:pass
 else:raise AssertionError('cross-owner accepted')
 o=project_object_only(current['analysis_id'],root,expected_revision=current['revision'],expected_workspace_owner=OWNER,object_id='ENERGY_E')
 verify_object_only_summary(o);assert o['object']['total_cost']=='1141.10' and 'source_refs' not in json.dumps(o)
 print('Integrated adapter→snapshots→guard→projection passed; original months',str(a.revision)[:12],str(z.revision)[:12])

class WholeCostFlow(unittest.TestCase):
    def test_independent_months_mixed_sources(self):
        with tempfile.TemporaryDirectory() as root:
            check(Path(root))
