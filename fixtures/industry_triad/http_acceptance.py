"""Real HTTP acceptance against an isolated instance, with independent fixtures.

Never targets production implicitly. Supply --base-url and a new evidence folder.
Only synthetic credentials are generated; cookies/passwords are not logged.
"""
import argparse
import copy
import csv
from decimal import Decimal as D
import hashlib
import io
import json
from pathlib import Path
import secrets
import sys
import time
import zipfile
import requests

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT / 'tests'), str(ROOT / 'scripts')]
from test_industry_triad import IndustryTriad


def run(base, fixtures, evidence):
    evidence.mkdir(parents=True, exist_ok=False)
    calls=[]; records=[]; oracle=IndustryTriad()
    def call(session, method, path, **kwargs):
        started=time.perf_counter()
        response=session.request(method,base+path,timeout=240,**kwargs)
        calls.append(dict(method=method,path=path,status=response.status_code,seconds=time.perf_counter()-started,bytes=len(response.content)))
        return response
    users=[]
    for role in ['owner','other']:
        session=requests.Session();session.trust_env=False
        name='triad_'+role+'_'+secrets.token_hex(5)
        response=call(session,'POST','/api/auth/register',json=dict(username=name,password='Synthetic!'+secrets.token_hex(16),name='Synthetic triad '+role))
        assert response.status_code==200,response.text
        session.headers['X-CSRF-Token']=session.cookies['lw_csrf'];users.append(session)
    alice,bob=users
    retained=[]
    def upload(files,schema,label):
        return call(alice,'POST','/api/platform/costing-bundle-import',data=dict(schema_json=json.dumps(schema,ensure_ascii=False),label=label),files=[('files',(name+'.csv',text.encode(),'text/csv')) for name,text in files.items()])
    for industry in ['pharma','restaurant','medical_devices']:
        for variant in ['small','large']:
            folder=fixtures/industry/('large' if variant=='large' else '')
            schema=json.loads((folder/'rules.json').read_text(encoding='utf8'))
            # Preserve original CRLF/LF and BOM for byte-level source pins.
            files={p.stem:p.read_bytes().decode('utf8') for p in folder.glob('*.csv')}
            expected=json.loads((folder/'expected.json').read_text(encoding='utf8'))
            start=time.perf_counter();response=upload(files,schema,'SYNTHETIC '+industry+' '+variant)
            assert response.status_code==200,response.text[:4000]
            body=response.json();snap=body['analysis'];elapsed=time.perf_counter()-start
            oracle.assert_oracle(dict(industry=industry,expected=expected,files=files),snap['result'])
            record=dict(industry=industry,variant=variant,source_rows=expected['source_rows'],total_cost=expected['total_cost'],cost_groups=expected['cost_groups'],asset_total=expected['asset_total'],pnl_cost=expected['pnl_cost'],elapsed_http_seconds=elapsed,analysis_id=snap['analysis_id'],revision=snap['revision'],status='passed',can_claim_entire_industry=False)
            record['budget_total'] = expected['budget_total']
            record['budget_variance'] = expected['budget_variance']
            for key in ('required_costs', 'separate_inventory_assets'):
                state = snap['result']['business_views'].get(key)
                if state is not None:
                    record[key] = state.get('summary', state) if isinstance(state, dict) else state
            records.append(record)
            out=evidence/industry/variant;out.mkdir(parents=True)
            (out/'snapshot.json').write_bytes(response.content)
            endpoint='/api/platform/costing-snapshots/'+snap['analysis_id'];pin=dict(revision=snap['revision'])
            assert call(bob,'GET',endpoint,params=pin).status_code==404
            assert call(alice,'GET',endpoint,params=dict(revision='0'*64)).status_code==409
            reloaded=call(alice,'GET','/api/platform/costing-analysis',params=dict(dataset_id=body['dataset']['dataset_id'],period=expected['scope']['period']))
            assert reloaded.status_code==200,reloaded.text[:1000]
            assert reloaded.json()['result']==snap['result']
            captured=[e for e in snap['evidence'] if e['source_uri'].startswith('upload://sources/')]
            assert captured and all(e['status']=='captured' and e['rows'] for e in captured)
            assert all(e['status'] in {'captured','captured_mapping','derived','synthetic'} for e in snap['evidence'])
            for item in (captured[:3] if variant=='large' else captured):
                ev=call(alice,'GET',endpoint+'/evidence/'+item['evidence_id'],params=pin)
                assert ev.status_code==200 and ev.json()['current_source_matches']
            if variant=='small':
                assert call(alice,'POST',endpoint+'/report',params=pin).status_code==200
                export_checks={}
                for fmt in ['json','md','docx','pdf']:
                    exported=call(alice,'GET',endpoint+'/export',params=dict(pin,format=fmt))
                    assert exported.status_code==200,exported.text[:2000]
                    path=out/('report.'+fmt);path.write_bytes(exported.content)
                    if fmt=='json':
                        document=exported.json();assert document==snap;text=json.dumps(document,ensure_ascii=False)
                    elif fmt=='md':text=exported.text;assert text==snap['report']['markdown']
                    elif fmt=='docx':
                        from docx import Document
                        doc=Document(path);text='\n'.join([p.text for p in doc.paragraphs]+[c.text for t in doc.tables for row in t.rows for c in row.cells])
                    else:
                        import fitz
                        with fitz.open(path) as pdf:
                            text='\n'.join(p.get_text() for p in pdf)
                            for page in [0,len(pdf)-1]:pdf[page].get_pixmap(matrix=fitz.Matrix(1,1)).save(str(out/f'pdf-page-{page+1}.png'))
                    compact=''.join(text.split())
                    assert snap['revision'] in compact,(fmt,'revision missing')
                    if fmt!='json':
                        for field in ['total_cost','asset_total','pnl_cost']:
                            assert format(D(expected[field]),',.2f') in text,(industry,fmt,field)
                        assert str(schema['plugin_version']) in text,(industry,fmt,'plugin_version')
                    (out/('report-'+fmt+'-extracted.txt')).write_text(text,encoding='utf8')
                    export_checks[fmt]=dict(sha256=hashlib.sha256(exported.content).hexdigest(),bytes=len(exported.content),revision_verified=True,amounts_verified=True)
                record['exports']=export_checks;retained.append(snap)
                failures=[]
                for case in ['missing','wrong_unit','duplicate','alias','return_excess','zero_driver','source_tamper','period','not_applicable','production_return_excess','production_wrong_batch']:
                    badfiles=dict(files);bad=copy.deepcopy(schema)
                    target = ('raw_materials' if case.startswith('production_') else
                              'events' if case in ['wrong_unit','return_excess','period'] else 'finance')
                    rows=list(csv.DictReader(io.StringIO(badfiles[target])));headers=list(rows[0])
                    if case=='missing':rows[0].update(amount='',status='missing')
                    elif case=='wrong_unit':rows[0]['unit']='wrong-unit'
                    elif case=='duplicate':rows.append(dict(rows[0]))
                    elif case=='alias':rows.append(dict(rows[0],id=''.join(chr(ord(c)+0xFEE0) if '!'<=c<='~' else c for c in rows[0]['id'])))
                    elif case=='return_excess':next(x for x in rows if x['op']=='return')['quantity']='999999'
                    elif case=='zero_driver':
                        rule=bad['raw_composite']['allocation_rules'][-1];rule['weights']={k:0 for k in rule['weights']}
                    elif case=='period':rows[0]['period']='2026-12'
                    elif case=='not_applicable':rows[0].update(amount='',status='not_applicable')
                    elif case=='production_return_excess':
                        next(row for row in rows if row['kind']=='production_return')['quantity']='999999'
                    elif case=='production_wrong_batch':
                        next(row for row in rows if row['kind']=='production_return')['production_object_id']='unapproved-batch'
                    else:rows[0]['amount']='12345.67'
                    raw=io.StringIO();writer=csv.DictWriter(raw,fieldnames=headers);writer.writeheader();writer.writerows(rows);badfiles[target]=raw.getvalue()
                    if case!='source_tamper':bad['raw_composite']['source_hashes'][target]=hashlib.sha256(badfiles[target].encode()).hexdigest()
                    failed=upload(badfiles,bad,'SYNTHETIC INVALID '+industry+' '+case)
                    detail = failed.json().get('detail', '')
                    assert failed.status_code == 422 and '成本数据校验失败' in detail, (
                        industry, case, failed.status_code, failed.text[:1000])
                    targetdir=out/'failures'/case;targetdir.mkdir(parents=True)
                    for filename,content in badfiles.items():(targetdir/(filename+'.csv')).write_text(content,encoding='utf8',newline='')
                    (targetdir/'rules.json').write_text(json.dumps(bad,ensure_ascii=False,indent=2),encoding='utf8')
                    (targetdir/'response.json').write_text(failed.text,encoding='utf8')
                    failures.append(dict(case=case,status=failed.status_code))
                record['failure_scenarios']=failures
            (out/'summary.json').write_text(json.dumps(record,ensure_ascii=False,indent=2),encoding='utf8')
            print(json.dumps(record,ensure_ascii=False),flush=True)
    for snap in retained:
        response=call(alice,'GET','/api/platform/costing-snapshots/'+snap['analysis_id'],params=dict(revision=snap['revision']))
        assert response.status_code==200 and response.json()==snap
    report=dict(status='passed',records=records,http_calls=calls,old_first_pharma_revision_reopened=retained[0]['revision'],browser_status='not_run_by_http_probe',production_status='untouched_not_deployed')
    (evidence/'http-acceptance.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf8')
    return report


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--base-url',required=True);parser.add_argument('--fixtures',type=Path,default=Path(__file__).resolve().parent);parser.add_argument('--evidence',type=Path,required=True);args=parser.parse_args()
    run(args.base_url.rstrip('/'),args.fixtures,args.evidence)
