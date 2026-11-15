"""Source-declared item disposition consistency, never serial traceability.

An opt-in, approved mutually-exclusive event scope binds declared item IDs to
existing rework financial records or scrap/return lifecycle records. No cost is
posted here and an arbitrary serial string does not become a verified identity.
"""
from collections import Counter
from dataclasses import asdict
from decimal import Decimal, InvalidOperation
import csv
import io
import unicodedata
from urllib.parse import urlencode

from .models import SourceRef


FIELDS={'id','object_id','item_id','action','ref'}
SCOPE={'book','period','currency'}
ACTIONS={'rework','scrap','return'}


def need(ok,message):
    if not ok:
        raise ValueError('dispositions: '+message)


def text(value,where,limit=256):
    need(isinstance(value,str) and 0<len(value)<=limit and value==value.strip()
         and not any(unicodedata.category(c).startswith('C') for c in value),where+': nonempty canonical text required')
    return value


def key(value):
    return unicodedata.normalize('NFKC',unicodedata.normalize('NFKC',value).casefold()).strip()


def decimal(value,where):
    try: result=Decimal(value)
    except (InvalidOperation,TypeError,ValueError): raise ValueError('dispositions: '+where+': finite decimal required') from None
    need(result.is_finite() and len(result.as_tuple().digits)<=30 and abs(result.adjusted())<=24,
         where+': finite bounded decimal required')
    return result


def _read(files,name,scope):
    need(isinstance(name,str) and name in files and isinstance(files[name],str),str(name)+': missing source text')
    need(len(files[name].encode('utf-8'))<=10*1024*1024,name+': source exceeds 10 MiB')
    try:
        reader=csv.DictReader(io.StringIO(files[name].lstrip('\ufeff')),strict=True)
        headers=reader.fieldnames or [];rows=list(reader)
    except csv.Error as exc: raise ValueError('dispositions: '+name+': invalid CSV: '+str(exc)) from None
    need(headers and all(headers) and len(headers)==len(set(headers)) and SCOPE<=set(headers),name+': invalid source headers')
    need(len(rows)<=20000,name+': record limit')
    for pos,row in enumerate(rows,2):
        where=f'{name}:row {pos}'
        need(None not in row and all(v is not None for v in row.values()),where+': ragged CSV')
        need(all(row[k]==v for k,v in scope.items()),where+': book/period/currency mismatch')
    return headers,rows


def compile_dispositions(files,schema,source_uris=None):
    business=schema.get('business_views')
    if business is None:
        return None
    need(isinstance(business,dict),'invalid business_views configuration')
    cfg=business.get('dispositions')
    if cfg is None:
        return None
    need(isinstance(cfg,dict) and {'file','columns','identity_basis'}<=set(cfg)
         <={'file','columns','identity_basis','action_tags'},'invalid configuration fields')
    basis=text(cfg['identity_basis'],'identity_basis',1024)
    columns=cfg['columns']
    need(isinstance(columns,dict) and set(columns)==FIELDS
         and all(isinstance(v,str) and v.strip() for v in columns.values())
         and len(set(columns.values()))==len(columns) and not set(columns.values())&SCOPE,
         'invalid disposition field mapping')
    tags=cfg.get('action_tags',{})
    need(isinstance(tags,dict) and set(tags)<={'rework'},'unsupported action_tags')
    if 'rework' in tags: text(tags['rework'],'action_tags.rework',80)
    scope=schema.get('raw_composite',{}).get('scope')
    need(isinstance(scope,dict) and set(scope)==SCOPE,'common ledger scope required')
    classifications=schema.get('business_views',{}).get('classifications',{})
    need(isinstance(classifications,dict) and classifications,'declared cost objects required')
    dataset=schema.get('dataset_id','csv-unregistered')
    uris=source_uris or {}
    def ref(file,pos,identity,field,id_field='id'):
        return asdict(SourceRef(dataset_id=dataset,source_uri=uris.get(file,f'upload://sources/{file}.csv'),
                  field=field,selector=urlencode({'row':pos,id_field:identity})))

    # These are indexes into ORIGINAL sources. They are not generated posting
    # coordinates and no amount is recalculated by this consistency control.
    financial={}; financial_ids=set(); required_rework=set()
    for name,branch in schema.get('raw_composite',{}).get('inputs',{}).items():
        if not isinstance(branch,dict) or 'raw_recognition' not in branch:
            continue
        option=branch['raw_recognition']
        if option.get('version')!=2:
            # v1 has no declared tag-to-source/object binding. Do not infer it.
            continue
        file=branch.get('file');headers,rows=_read(files,file,scope)
        mapped=branch.get('columns',{})
        field=lambda k:mapped.get(k,k)
        need({field(k) for k in ('id','tag','amount','kind','status')}<=set(headers),
             str(file)+': original financial identity/tag/value columns required')
        for pos,raw in enumerate(rows,2):
            sid=text(raw[field('id')],f'{file}:row {pos} id',4096)
            need(key(sid) not in financial_ids,f'{file}:row {pos}: duplicate or aliased financial ID')
            financial_ids.add(key(sid))
            tag=raw[field('tag')].strip(); targets=set(); matches=[]
            for rule in option.get('rules',[]):
                if rule.get('op')!='recognize' or rule.get('source_tag')!=tag:
                    continue
                spec=option.get('posting_map',{}).get(rule.get('id'),{})
                if spec.get('object_id_from')=='target_object_id':
                    target=raw.get(field('target_object_id'),'').strip()
                else:
                    target=spec.get('object_id','')
                if target: targets.add(target)
                matches.append((rule,spec))
            recognized=(len(matches)==1 and len(targets)==1 and tag not in option.get('excluded_tags',{})
                        and raw[field('status')].strip() in {'data','zero'}
                        and raw[field('kind')].strip()==matches[0][0].get('source_kind')
                        and matches[0][1].get('record_type')=='entry')
            amount=decimal(raw[field('amount')],f'{file}:row {pos} amount') if recognized else None
            item={'object_id':next(iter(targets)) if len(targets)==1 else None,'tag':tag,
                  'recognized':recognized,'amount':amount,
                  'source_ref':ref(file,pos,sid,field('amount'),field('id'))}
            financial[sid]=item
            if tags.get('rework')==tag:
                need(recognized and amount>=0,f'{file}:row {pos}: approved rework tag must bind a nonnegative recognized direct financial source')
                if amount>0: required_rework.add(sid)

    events={}; event_ids=set(); required_events={}
    lifecycle=schema.get('business_views',{}).get('lifecycle')
    if lifecycle is not None:
        need(isinstance(lifecycle,dict) and isinstance(lifecycle.get('columns'),dict),'invalid lifecycle binding')
        file=lifecycle.get('file');headers,rows=_read(files,file,scope);mapped=lifecycle['columns']
        need({'id','object_id','op','quantity'}<=set(mapped) and set(mapped.values())<=set(headers),
             'invalid lifecycle source mapping')
        for pos,raw in enumerate(rows,2):
            sid=text(raw[mapped['id']],f'{file}:row {pos} id',4096)
            need(key(sid) not in event_ids,f'{file}:row {pos}: duplicate or aliased event ID')
            event_ids.add(key(sid));action=raw[mapped['op']].strip()
            qty=decimal(raw[mapped['quantity']],f'{file}:row {pos} quantity')
            events[sid]={'object_id':raw[mapped['object_id']].strip(),'action':action,
                         'source_ref':ref(file,pos,sid,mapped['quantity'],mapped['id'])}
            if action in {'scrap','return'}:
                need(qty>=0 and qty==qty.to_integral_value(),f'{file}:row {pos}: disposition identity requires integer nonnegative unit quantity')
                required_events[sid]=int(qty)

    headers,rows=_read(files,cfg['file'],scope)
    need(set(headers)==set(columns.values())|SCOPE,'unknown or missing disposition columns')
    need(rows,'declared disposition source has no records')
    seen_ids=set();seen_items={};records=[];counts=Counter();reference_counts=Counter();rework_refs=set()
    for pos,raw in enumerate(rows,2):
        where=f'{cfg["file"]}:row {pos}'
        row={k:text(raw[v],where+' '+k,4096 if k=='ref' else 256) for k,v in columns.items()}
        need(key(row['id']) not in seen_ids,where+': duplicate or aliased disposition ID')
        seen_ids.add(key(row['id']))
        item_key=key(row['item_id']);need(item_key not in seen_items,
              where+': item already disposed at row '+str(seen_items.get(item_key))+'; mutually exclusive disposition scope')
        seen_items[item_key]=pos
        action=row['action'];need(action in ACTIONS,where+': unsupported action')
        need(row['object_id'] in classifications,where+': unknown object')
        if action=='rework':
            need('rework' in tags,where+': missing approved rework action tag; no inferred mapping')
            original=financial.get(row['ref'])
            need(original is not None and original['recognized'] and original['amount']>=0
                 and original['tag']==tags['rework'],where+': reference is not approved recognized rework financial source')
            rework_refs.add(row['ref'])
        else:
            original=events.get(row['ref'])
            need(original is not None and original['action']==action,where+': reference event action mismatch or missing original event')
            reference_counts[row['ref']]+=1
        need(original['object_id']==row['object_id'],where+': original object mismatch')
        records.append({**row,'canonical_item_id':item_key,'source_refs':[
             ref(cfg['file'],pos,row['id'],columns['item_id'],columns['id']),original['source_ref']]})
        counts[action]+=1
    need(required_rework<=rework_refs,'missing disposition controls for positive recognized rework sources: '+','.join(sorted(required_rework-rework_refs)))
    need(all(reference_counts[sid]==qty for sid,qty in required_events.items()),
         'disposition identity count must equal every scrap/return original event quantity')
    return {'version':1,'identity_basis':basis,
            'evidence_scope':'source_declared_disposition_identity;not_serial_trace',
            'policy':'one_disposition_per_declared_item_in_this_approved_event_scope',
            'records':records,'counts':{a:counts[a] for a in sorted(ACTIONS)},
            'coverage':{'rework':'complete_declared_positive_sources' if 'rework' in tags else 'not_configured_no_inference',
                        'scrap_return':'complete_declared_integer_event_quantities'},
            'source_refs':[asdict(SourceRef(dataset_id=dataset,source_uri='upload://schema',field='business_views',
                              selector='business_views',kind='configuration'))]}
