"""Synthetic museum branch/area oracle; no source files or third-party data."""
from collections import defaultdict
from decimal import Decimal as D, ROUND_HALF_UP
from pathlib import Path
from tempfile import TemporaryDirectory
from time import perf_counter
from urllib.parse import parse_qs
import csv, hashlib, io, json, sys

BOOK='SYNTH_MUSEUM_NETWORK'
MONTH='2026-11'
SCOPE={'book':BOOK,'period':MONTH,'currency':'CNY'}
DATASET='csv-8b2d38a90f607133'
WAREHOUSES=('annex','lobby','main','school')
CENTRES=('A0','A1','A2','A3','A4','A5','A6','A7')
AREAS={centre:125+35*n for n,centre in enumerate(CENTRES)}
CATEGORIES=(('security','accrual'),('programmes','expense'),('cleaning','expense'),
            ('conservation','expense'),('supplier-credit','credit'))

def fmt(value): return str(value.quantize(D('.01'),rounding=ROUND_HALF_UP))
def amount(cents): return fmt(D(cents)/100)
def roundc(value): return int(D(value).quantize(D('1'),rounding=ROUND_HALF_UP))
def partition(cents):
    weights=sum(AREAS.values()); a=abs(cents); parts=[]
    for i,office in enumerate(CENTRES):
        q,m=divmod(a*AREAS[office],weights);parts.append((q,m,i))
    leftover=a-sum(t[0] for t in parts)
    extra={x[2] for x in sorted(parts,key=lambda v:(-v[1],v[2]))[:leftover]}
    sign=1 if cents>=0 else -1
    return {CENTRES[i]:sign*(q+(i in extra)) for q,m,i in parts}
def csv_text(rows):
    f=io.StringIO(newline='')
    w=csv.DictWriter(f,fieldnames=sorted(set().union(*(row.keys() for row in rows))))
    w.writeheader();w.writerows(rows);return f.getvalue()
def member_hash(name,members):
    body={'ledger_scope':SCOPE,'group_id':name,'members':sorted([x['input_name'],x['object_id']] for x in members)}
    payload=json.dumps(body,ensure_ascii=False,sort_keys=True,separators=(',',':')).encode('utf-8')
    return hashlib.sha256(payload).hexdigest()
def make():
    rows=[]; branches={}; exp=defaultdict(int)
    templates=[]; post={}
    for tag,kind in CATEGORIES:
        templates.append(dict(id=tag,op='recognize',source_tag=tag,source_kind=kind,posting_pool='museum-operations'))
        post[tag]=dict(object_id_from='target_object_id',object_kind='service',record_type='entry',cost_element=tag,
                       cost_type='direct',behavior='variable')
    templates.append(dict(id='licence',op='monthly_straight_line',source_tag='annual-licence',source_kind='prepay_basis',
                         start='source_origin',months=12,posting_pool='museum-operations'))
    templates.append(dict(id='dep',op='monthly_straight_line',source_tag='asset-rig',source_kind='asset_basis',
                         start='source_origin',months=24,posting_pool='museum-operations'))
    for tag in ('licence','dep'):
        post[tag]=dict(object_id='studio-overhead',object_kind='cost_center',record_type='pool',pool_id=tag,
                       cost_element=tag,cost_type='indirect',behavior='fixed')
    scopes={**SCOPE,'period':'from_schema'}
    branches['period']=dict(raw_recognition=dict(version=2,name='museum cost plugin',context=scopes,rules=templates,
        outputs=[i['id'] for i in templates],excluded_tags={'cash':'payment memo only'},posting_map=post,
        ledger_basis='recognized_expenses_incurred',target_objects=[dict(object_id=i,object_kind='service') for i in CENTRES]),
        allocation_rules=[dict(pool_id=k,target_ids=list(CENTRES),weights=AREAS.copy()) for k in ('licence','dep')])
    def expense(event,tag,kind,cents,target='',origin=''):
        record=dict(**SCOPE,id=event,kind=kind,status='data',amount=amount(cents),economic_key=event,
                    evidence='abstract synthetic voucher '+event,tag=tag,input_name='period',posting_identity=event)
        if target:record['target_object_id']=target
        if origin:record['origin_period']=origin
        rows.append(record)
    for day in range(1,31):
        for i,area in enumerate(CENTRES):
            for line in range(10):
                tag,kind=CATEGORIES[(day+i+line)%5]
                cents=1250+((day*113+i*157+line*211)%1750)
                if kind=='credit':cents=-(cents//5)
                expense(f'P-{day:02d}-{area}-{line:02d}',tag,kind,cents,area)
                exp[area]+=cents
    expense('PLAN-LIC','annual-licence','prepay_basis',960000,origin='2026-01')
    expense('RIG-ASSET','asset-rig','asset_basis',5040000,origin='2026-01')
    expense('CASH-LIC','cash','cash_movement',960000)
    for k in CENTRES:exp[k]+=partition(80000)[k]+partition(210000)[k]
    meter_amounts={}
    for name,object_id,unit,nm,nh,fence,lo,hi in [('power','power-plant','kWh',5,16,'1800','0.72','1.08'),
                                                    ('water','water-system','m³',4,12,'600','1.43','2.10')]:
        account=f'museum-{name}'
        profile=dict(version=1,currency='CNY',unit=unit,tier_scope='billing_account',usage_scale=3,currency_scale=2,
                     tiers=[dict(until=fence,rate=lo),dict(until=None,rate=hi)],basis_months=0)
        branches[name]=dict(raw_meter=dict(profile=profile,scope={**scopes,'billing_account':account},
            posting_map={x:dict(object_id=object_id,object_kind='service',cost_type='direct',behavior='variable',cost_element=x)
                         for x in ('metered_usage','fixed_fee','vendor_credit')}))
        qty=D(0)
        for n in range(1,31):
            for m in range(nm):
                for t in range(nh):
                    ref=f'{name}-{n:02d}-{m}-{t:02d}'
                    q=D(390+(n*53+m*83+t*37)%1793)/1000
                    qty+=q
                    rows.append(dict(**SCOPE,id=ref,billing_account=account,kind='usage',evidence='synthetic interval meter '+ref,
                                meter=f'M{m}',unit=unit,qty=str(q),input_name=name,posting_identity=ref))
        t=min(qty,D(fence))*D(lo)+max(D(0),qty-D(fence))*D(hi)-D('12.34')
        credit=f'{name}-credit'
        rows.append(dict(**SCOPE,id=credit,billing_account=account,kind='vendor_credit',evidence='synthetic utility adjustment',
                         amount='-12.34',input_name=name,posting_identity=credit))
        meter_amounts[name]=roundc(t*100)
    inventory=dict(operation='moving_weighted_average',policy='line_half_up_minor_units_with_cumulative_original_issue_returns',
                   context={**SCOPE,'cutoff_at':'2026-11-30T23:59:59Z'},
                   chronology='sort_by_utc_timestamp_then_sequence',transfer_policy='closed_same_period_pairs_v1',
                   transfer_history=[],streams=[])
    branches['stock']=dict(raw_inventory=inventory)
    stock={}; counted=defaultdict(int); expenses=defaultdict(int); moves={}
    for j,wh in enumerate(WAREHOUSES):
        ix=dict(warehouse=wh,item='visitor-guide',uom='EA')
        q=100+5*j; v=q*(725+25*j); stock[wh]=[q,v]
        inventory['streams'].append(dict(scope=ix,opening=dict(**ix,book=BOOK,currency='CNY',as_of='2026-10-31T23:59:59Z',
             quantity=str(q),value=amount(v),status='data',evidence='synthetic opening point',issues=[],seen_event_ids=[],seen_source_ids=[]),
             posting_map={x:dict(object_id='shop-'+wh,object_kind='product',cost_type='direct',behavior='variable',cost_element=x)
                          for x in ('cogs','shrink')}))
    before=sum(s[1] for s in stock.values())
    def movement(n,wh,j,order,kind,q,**special):
        ident=f'S-{n:02d}-{j}-{order}'
        rows.append(dict(warehouse=wh,item='visitor-guide',uom='EA',book=BOOK,currency='CNY',id=ident,
                         source_id='orig-'+ident,posting_identity=ident,input_name='stock',at=f'2026-11-{n:02d}T10:00:00Z',
                         sequence=str(order),quantity=str(q),kind=kind,evidence='synthetic movement '+ident,**special))
    transfer_cents=0
    for day in range(1,31):
        for j,wh in enumerate(WAREHOUSES):
            b=1+6*j; purchased=6+(day+j)%4; cents=purchased*(610+3*day+23*j)
            movement(day,wh,j,b,'receipt',purchased,receipt_value=amount(cents))
            stock[wh][0]+=purchased;stock[wh][1]+=cents;counted['external']+=cents
            demand=4+(day+j)%3
            movement(day,wh,j,b+1,'sale_issue',demand)
            val=roundc(D(stock[wh][1])*demand/stock[wh][0]);stock[wh][0]-=demand;stock[wh][1]-=val;expenses[wh]+=val
            movement(day,wh,j,b+2,'sale_return',1,original_issue_id=f'S-{day:02d}-{j}-{b+1}',restock_status='restockable')
            credit=roundc(D(val)/demand);stock[wh][0]+=1;stock[wh][1]+=credit;expenses[wh]-=credit
            movement(day,wh,j,b+3,'shrink',1)
            loss=roundc(D(stock[wh][1])/stock[wh][0]);stock[wh][0]-=1;stock[wh][1]-=loss;expenses[wh]+=loss
        if day in (9,21):
            a,b=WAREHOUSES[day%4],WAREHOUSES[(day+1)%4];q=4+day%3
            move=roundc(D(stock[a][1])*q/stock[a][0]);stock[a][0]-=q;stock[a][1]-=move
            stock[b][0]+=q;stock[b][1]+=move;transfer_cents+=move
            movement(day,a,4,28,'transfer_out',q,transfer_id=f'TR-{day}')
            movement(day,b,5,30,'transfer_in',q,transfer_id=f'TR-{day}')
    close=sum(x[1] for x in stock.values())
    assert before+counted['external']==close+sum(expenses.values())
    groups=[];budgets=[];checklist={}
    cases=[(i,[dict(input_name='period',object_id=i)]+
              ([dict(input_name='stock',object_id='shop-'+WAREHOUSES[j])] if j<len(WAREHOUSES) else []),
              exp[i]+(expenses[WAREHOUSES[j]] if j<len(WAREHOUSES) else 0),180000+3000*j)
           for j,i in enumerate(CENTRES)]
    cases.append(('facilities',[dict(input_name='power',object_id='power-plant'),dict(input_name='water',object_id='water-system')],
                  sum(meter_amounts.values()),480000))
    comparisons={}
    for i,members,cost,plan in cases:
        groups.append(dict(id=i,label='Museum area '+i,members=members))
        budgets.append(dict(group_id=i,amount=amount(plan),status='budgeted',source_id='SYNTH-BUDGET-'+i,
                            membership_sha256=member_hash(i,members)))
        comparisons[i]=(amount(cost),amount(plan),amount(cost-plan))
        cats=[]
        def add(key,*matches):
            cats.append(dict(id=key,name=key,state='required',matches=[dict(input_name=x,element_id=y) for x,y in matches]))
        if i=='facilities':
            for b in ('power','water'): add(b,(b,'metered_usage'),(b,'vendor_credit'))
        else:
            for tag,_ in CATEGORIES:add(tag,('period',tag))
            add('overhead',('period','licence'),('period','dep'))
            if len(members)>1:add('guide-stock',('stock','cogs'),('stock','shrink'))
        checklist[i]=cats
    cfg=dict(dataset_id=DATASET,raw_composite=dict(version=3,scope=SCOPE,inputs=branches,object_rollups=groups,
        object_budgets=dict(version=1,**SCOPE,rows=budgets),category_checklist=dict(version=1,groups=checklist)))
    expected=dict(rows=len(rows),ledger=amount(sum(exp.values())+sum(expenses.values())+sum(meter_amounts.values())),
                  period=amount(sum(exp.values())),stock=amount(sum(expenses.values())),closing=amount(close),
                  closed_transfers=amount(transfer_cents),power=amount(meter_amounts['power']),
                  water=amount(meter_amounts['water']),group_checks=comparisons)
    return cfg,list(reversed(rows)),expected

def verify(result,oracle):
    info=result['recognition']
    assert fmt(D(result['totals']['total_cost']))==oracle['ledger'],(result['totals'],oracle)
    assert not info['can_claim_complete'] and result['ledger_reconciled']
    for branch in ('period','stock','power','water'):
        assert fmt(D(info['posted_pool_totals'][branch]))==oracle[branch]
    v=info['inputs']['stock']['inventory_valuation']
    assert v['outputs']['closing_value']==oracle['closing']
    assert v['transfers']['closed_value']==oracle['closed_transfers']
    assert len(v['transfers']['closed_pairs'])==2
    b=info['object_budgets']
    assert {x['group_id']:(x['actual_cost'],x['budget_cost'],x['variance']) for x in b['rows']}==oracle['group_checks'],b['rows']
    for g in info['category_checklist']['groups']:
        assert D(g['uncategorized_amount'])==0
        assert all(c['state']=='observed' for c in g['categories']),[(c['id'],c['state']) for c in g['categories']]

def run():
    from cost_core.csv_adapter import CsvCostingAdapter
    from cost_core.costing import CostingEngine
    from costing_outputs import create_snapshot,load_snapshot,export_snapshot
    t=perf_counter();schema,rows,oracle=make(); raw=csv_text(rows)
    s0=perf_counter()
    req=CsvCostingAdapter().from_text(raw,schema)
    s1=perf_counter();out=CostingEngine().analyze(req);verify(out,oracle);s2=perf_counter()
    with TemporaryDirectory() as temp:
        root=Path(temp);src=root/'costing_datasets'/DATASET;src.mkdir(parents=True)
        (src/'data.csv').write_text(raw,encoding='utf-8')
        (src/'manifest.json').write_text(json.dumps({'schema':schema}),encoding='utf-8')
        first=create_snapshot(out,root,root,dict(dataset_id=DATASET,dataset_label='Synthetic museum holdout',
                        period=MONTH,scenario='actual',owner_external_id='independent-qa'))
        s3=perf_counter()
        loaded=load_snapshot(first['analysis_id'],root);s4=perf_counter()
        doc=export_snapshot(loaded,'md',root/'out').read_text()
        exported=export_snapshot(loaded,'json',root/'out')
        assert len(doc)>1_000 and loaded['revision']==first['revision']==json.loads(exported.read_text())['revision']
        captured={int(parse_qs(e['selector'])['row'][0]) for e in loaded['evidence'] if e['status']=='captured'}
        assert captured==set(range(2,len(rows)+2)),(len(captured),len(rows))
        print(json.dumps({**oracle,'raw_bytes':len(raw.encode()),'json_bytes':exported.stat().st_size,'md_bytes':len(doc.encode()),
           'captured_original_rows':len(captured),'same_revision':True,'total_seconds':round(perf_counter()-t,3),
           'parse_seconds':round(s1-s0,3),'engine_seconds':round(s2-s1,3),'snapshot_seconds':round(s3-s2,3),
           'reload_seconds':round(s4-s3,3),'source_sha256':hashlib.sha256(raw.encode()).hexdigest()},sort_keys=True))
def run_http(port):
    # Explicit opt-in. The caller owns temporary service and authorization;
    # this routine never starts one or contacts a non-loopback destination.
    import httpx, secrets
    assert port.isascii() and port.isdecimal() and 18766<=int(port)<=18799
    schema,rows,known=make(); data=csv_text(rows).encode('utf-8')
    started=perf_counter()
    with httpx.Client(base_url='http://127.0.0.1:'+port,timeout=110,trust_env=False,follow_redirects=False) as c:
        n=secrets.token_hex(6); login=dict(username='qa'+n,password='Synthetic!'+n)
        for action in ('register','login'):
            answer=c.post('/api/auth/'+action,json=login)
            assert answer.status_code==200,(action,answer.status_code)
        token={'X-CSRF-Token':c.cookies['lw_csrf']}
        pre=perf_counter()
        reply=c.post('/api/platform/costing-import',files={'file':('museum-synthetic.csv',data,'text/csv')},
                      data={'schema_json':json.dumps(schema),'label':'Synthetic museum large QA'},headers=token)
        assert reply.status_code==200,(reply.status_code,reply.text[:250])
        finish=perf_counter()
        record=reply.json()['analysis']; key='/api/platform/costing-snapshots/'+record['analysis_id']
        verify(record['result'],known)
        reread=c.get(key); assert reread.status_code==200
        snap=reread.json(); assert snap['revision']==record['revision']
        exported=c.get(key+'/export?format=json'); assert exported.status_code==200
        assert exported.json()['revision']==record['revision']
        md=c.get(key+'/export?format=md'); assert md.status_code==200 and len(md.content)>1000
        found={int(parse_qs(e['selector'])['row'][0]) for e in snap['evidence'] if e['status']=='captured'}
        assert found==set(range(2,len(rows)+2)),(len(found),len(rows))
        print(json.dumps({**known,'raw_bytes':len(data),'json_bytes':len(exported.content),'md_bytes':len(md.content),
            'captured_original_rows':len(found),'same_revision':True,'elapsed_seconds':round(perf_counter()-started,3),
            'post_seconds':round(finish-pre,3),'source_sha256':hashlib.sha256(data).hexdigest()},sort_keys=True))
if __name__=='__main__':
    if sys.argv[1:2]==['--http'] and len(sys.argv)==3:run_http(sys.argv[2])
    elif len(sys.argv)==1:run()
    else:raise SystemExit('use: qa.py [--http explicit-temporary-loopback-port]')

