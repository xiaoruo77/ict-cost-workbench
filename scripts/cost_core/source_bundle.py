"""Version 4 named original sources and a shared allocation DAG.

This module composes existing recognition/inventory/meter compilers and the
existing CostingEngine. It never computes an alternative cost ledger.
"""
from dataclasses import replace
from decimal import Decimal, localcontext
from collections.abc import Mapping
import csv
import hashlib
import io
import json
from urllib.parse import parse_qsl, urlencode

from .composite_ingest import (find_mode, mode_fields, need, token,
                               real_refs, source_collision_key, SCOPE_FIELDS)
from .costing import CostObject, CostPool, CostDriver, AllocationRule
from .models import SourceRef
from .object_rollups import compile_plan as compile_rollups, validate as validate_rollups
from .object_budgets import compile_plan as compile_budgets, validate as validate_budgets
from . import category_checklist
from .meter_eval import MAX_ROWS as MAX_METER_ROWS


def _view_files(value):
    if isinstance(value, dict):
        result = {value['file']} if 'file' in value and isinstance(value['file'], str) else set()
        return result | set().union(*(_view_files(v) for v in value.values()), set())
    if isinstance(value, list):
        return set().union(*(_view_files(v) for v in value), set())
    return set()


def validate_bundle_schema(adapter, schema):
    need(set(schema) <= {'dataset_id','plugin_id','currency','period','scenario','raw_composite',
                         'industry','label','business_views','plugin_version'}, 'unsupported v4 top-level field')
    need('plugin_version' not in schema or isinstance(schema['plugin_version'],str) and schema['plugin_version'].strip(),
         'plugin_version must be a nonempty string')
    env = schema['raw_composite']
    need(type(env) is dict and {'version','scope','inputs'} <= set(env) and
         type(env['version']) is int and env['version'] == 4, 'invalid v4 envelope')
    need(set(env) <= {'version','scope','inputs','objects','incoming_pools','allocation_rules',
                     'pool_options','object_rollups','object_budgets','category_checklist','source_hashes','operational_inputs'},
         'unsupported v4 envelope field')
    scope = env['scope']
    need(type(scope) is dict and set(scope) == SCOPE_FIELDS and
         all(isinstance(v,str) and v.strip() for v in scope.values()) and scope['currency']=='CNY',
         'v4 requires common book, period and two-decimal CNY')
    need(schema.get('period',scope['period']) == scope['period'] and
         schema.get('currency',scope['currency']) == scope['currency'] and
         schema.get('scenario','actual') == 'actual', 'outer scope mismatch')
    if 'operational_inputs' in env:
        from .operational_ingest import operational_files
        operational_files(env['operational_inputs'])
    sections = env['inputs']
    need(type(sections) is dict and 1 <= len(sections) <= 6, 'v4 requires one to six cost sources')
    need(len({str(k).casefold() for k in sections})==len(sections),'aliased input names')
    used = set()
    meter_accounts = set()
    for name, cfg in sections.items():
        need(token(name) and type(cfg) is dict, 'invalid v4 input name')
        mode = find_mode(cfg)
        need(set(cfg) <= {'file','columns',mode} and 'file' in cfg and token(cfg['file']),
             f'invalid v4 fields for {name}; allocations must be global')
        need(cfg['file'] not in used, f'file consumed by multiple cost branches: {cfg["file"]}')
        used.add(cfg['file'])
        cols = cfg.get('columns', {})
        req, opt = mode_fields(mode,cfg)
        need(type(cols) is dict and set(cols) <= req|opt and
             all(isinstance(v,str) and v.strip() for v in cols.values()) and
             len(set(cols.values())) == len(cols), f'invalid column mapping for {name}')
        adapter.validate_schema({mode:cfg[mode], 'period':scope['period'],'currency':scope['currency']})
        if mode == 'raw_meter':
            bill = cfg[mode]['scope']
            account = bill['billing_account']
            need(bill['book'] == scope['book'] and bill['currency'] == scope['currency']
                 and bill['period'] in ('from_schema', scope['period']), f'{name}: meter scope mismatch')
            canonical = source_collision_key(account, 128, 0)
            need(canonical not in meter_accounts, f'{name}: duplicate or aliased meter billing account')
            meter_accounts.add(canonical)
            need(len(meter_accounts) <= 4, 'too many meter billing accounts')
    adapter.validate_schema({'allocation_rules':env.get('allocation_rules',[]),
                             'pool_options':env.get('pool_options',{})})
    for name in ('objects','incoming_pools','allocation_rules'):
        need(type(env.get(name,[])) is list and len(env.get(name,[])) <= 1000, f'invalid {name}')
    rule_fields={'rule_id','pool_id','target_ids','method','driver_id','driver_name','driver_unit',
                 'description','weights','weight_factors','stage','rounding','allow_partial'}
    for n,rule in enumerate(env.get('allocation_rules',[])):
        need(set(rule) <= rule_fields,f'allocation_rules[{n}]: undeclared fields')
    if 'object_rollups' in env: validate_rollups(env['object_rollups'],sections)
    if 'object_budgets' in env:
        validate_budgets(env['object_budgets'],scope,env.get('object_rollups'))
    if 'category_checklist' in env:
        category_checklist.validate(env['category_checklist'],scope,env.get('object_rollups'))
    hashes = env.get('source_hashes',{})
    need(type(hashes) is dict and all(token(k) and isinstance(v,str) and len(v)==64 and
         all(c in '0123456789abcdef' for c in v) for k,v in hashes.items()), 'invalid source hashes')


def _mapped_refs(value, fields):
    if isinstance(value, SourceRef):
        if value.kind == 'configuration' or value.source_uri.startswith(('schema://', 'upload://schema', 'mapping://')):
            return value
        # Each named file already identifies its branch. input_name belongs to
        # the old combined CSV, not to these original independent headers.
        selector={fields.get(k,k):v for k,v in parse_qsl(value.selector,keep_blank_values=True)
                  if k!='input_name'}
        return replace(value,field=fields.get(value.field,value.field),selector=urlencode(selector))
    if isinstance(value,dict): return {k:_mapped_refs(v,fields) for k,v in value.items()}
    if isinstance(value,(tuple,list)): return type(value)(_mapped_refs(v,fields) for v in value)
    return value


def compile_bundle(adapter, files, schema, *, source_uris=None, schema_uri='upload://schema'):
    adapter.validate_schema(schema)
    env = schema['raw_composite']; scope = env['scope']; sections = env['inputs']
    need(env['version'] == 4, 'from_files requires raw_composite v4')
    need(isinstance(files,Mapping) and 1 <= len(files) <= 16 and all(token(k) for k in files),
         'invalid named files')
    need(len({k.casefold() for k in files})==len(files),'aliased source file names')
    expected = {cfg['file'] for cfg in sections.values()} | _view_files(schema.get('business_views',{}))
    if 'operational_inputs' in env:
        from .operational_ingest import operational_files
        expected |= operational_files(env['operational_inputs'])
    need(set(files) == expected,
         f'named file mismatch; missing={sorted(expected-set(files))}, extra={sorted(set(files)-expected)}')
    source_uris = ({k:f'upload://sources/{k}.csv' for k in files} if source_uris is None else dict(source_uris))
    need(set(source_uris)==set(files) and all(isinstance(v,str) and v.strip() for v in source_uris.values())
         and len(set(source_uris.values()))==len(files), 'source URIs must identify each original file uniquely')
    parsed = {}; manifest = []; bytes_total = 0
    for name, content in files.items():
        need(isinstance(content,str),f'{name}: CSV must be text')
        raw = content.encode('utf-8'); bytes_total += len(raw)
        need(len(raw)<=10*1024*1024 and bytes_total<=32*1024*1024, 'source bundle size limit')
        try:
            reader = csv.DictReader(io.StringIO(content.lstrip('\ufeff')),strict=True)
            headers = reader.fieldnames; rows = list(reader)
        except csv.Error as exc: raise ValueError(f'composite: {name}: invalid CSV: {exc}') from None
        need(headers and all(headers) and len(headers)==len(set(headers)),f'{name}: invalid CSV headers')
        need(rows and len(rows)<=100000,f'{name}: empty or excessive rows')
        need(all(None not in r and all(v is not None for v in r.values()) for r in rows),f'{name}: ragged CSV')
        sha = hashlib.sha256(raw).hexdigest()
        need(name not in env.get('source_hashes',{}) or env['source_hashes'][name]==sha,
             f'{name}: source hash mismatch')
        parsed[name]=(headers,rows)
        manifest.append({'name':name,'source_uri':source_uris[name],'sha256':sha,
                         'row_count':len(rows),'headers':headers})
    need(set(env.get('source_hashes',{})) <= set(files),'hash declaration names unknown source')
    # Operational data is validated before any financial object can enter the
    # combined ledger. Do not fabricate target_objects in an in-memory schema:
    # that would create references to selectors absent from the uploaded rules.
    from .csv_adapter import _number
    dataset = schema.get('dataset_id', 'csv-unregistered')
    operational = None
    operational_objects = {}
    operational_drivers = {}
    if 'operational_inputs' in env:
        from .operational_ingest import compile_operational_bundle
        operational = compile_operational_bundle(env['operational_inputs'], files, scope, dataset_id=dataset)
        need(not operational.get('generated_inputs'), 'operational financial generation is not enabled')
        uri_map = {f'upload://sources/{name}.csv': uri for name, uri in source_uris.items()}
        uri_map['upload://schema'] = schema_uri
        def remap(value):
            if isinstance(value, SourceRef):
                return replace(value, source_uri=uri_map.get(value.source_uri, value.source_uri))
            if isinstance(value, dict):
                return {k: uri_map.get(v, v) if k == 'source_uri' and isinstance(v, str) else remap(v)
                        for k, v in value.items()}
            if isinstance(value, list):
                return [remap(v) for v in value]
            if isinstance(value, tuple):
                return tuple(remap(v) for v in value)
            return value
        operational = remap(operational)
        for value in operational.get('objects', []):
            ident = value['object_id']
            need(ident not in operational_objects, 'duplicate operational object binding')
            operational_objects[ident] = value
        operational_drivers = operational.get('drivers', {})
        actual_rules = {r.get('rule_id', f'bundle-rule-{n}') for n, r in enumerate(env.get('allocation_rules', []))}
        need(set(operational_drivers) <= actual_rules, 'operational driver binding names unknown allocation rule')

    def op_refs(items):
        return tuple(x if isinstance(x, SourceRef) else SourceRef(**x) for x in items)

    def bind_operational_object(obj, declarations=()):
        bound = operational_objects.get(obj.object_id)
        if bound is None:
            return obj
        quantity = _number(bound.get('quantity'), 'operational object.quantity', optional=True)
        unit = bound['quantity_unit']
        need(bound.get('currency', scope['currency']) == obj.currency,
             f'operational currency conflicts with object {obj.object_id}')
        for declared in declarations:
            if declared.get('object_id') != obj.object_id:
                continue
            if declared.get('quantity') not in (None, ''):
                need(_number(declared['quantity'], 'declared object.quantity') == quantity,
                     f'operational quantity conflicts with explicit object {obj.object_id} declaration')
            if declared.get('quantity_unit') not in (None, ''):
                need(declared['quantity_unit'] == unit,
                     f'operational unit conflicts with explicit object {obj.object_id} declaration')
        need(obj.quantity is None or obj.quantity == quantity,
             f'operational quantity conflicts with object {obj.object_id}')
        need(obj.quantity_unit in ('unit', unit),
             f'operational unit conflicts with object {obj.object_id}')
        for key in ('name', 'kind'):
            need(key not in bound or bound[key] == getattr(obj, key),
                 f'operational {key} conflicts with object {obj.object_id}')
        mappings = env['operational_inputs'].get('object_bindings', [])
        pos = next(n for n, item in enumerate(mappings) if item['object_id'] == obj.object_id)
        binding_ref = SourceRef(dataset_id=dataset, source_uri=schema_uri, field='object_bindings',
              selector=f'raw_composite.operational_inputs.object_bindings[{pos}]', kind='configuration')
        refs = tuple(dict.fromkeys(obj.source_refs + op_refs(bound.get('source_refs', [])) + (binding_ref,)))
        return replace(obj, quantity=quantity, quantity_unit=unit, source_refs=refs)

    objects={}; owners={}; entries=[]; pools=[]; audits={}; total_rows=0; identities=set(); economic=set(); first=None
    production_pairs = set()
    nonproduction_pairs = set()
    recognition_owners = {}
    classifications = (schema.get('business_views') or {}).get('classifications', {})
    meter_rows = meter_bands = 0
    for name,cfg in sorted(sections.items()):
        file=cfg['file']; headers,rows=parsed[file]; total_rows+=len(rows)
        need(total_rows<=15000,'bundle cost source row limit')
        mode=find_mode(cfg); required,optional=mode_fields(mode,cfg); mapping=cfg.get('columns',{})
        mapped={k:mapping.get(k,k) for k in required|optional}
        need(len(set(mapped.values()))==len(mapped),f'{name}: ambiguous mapped headers')
        need({mapped[k] for k in required} <= set(headers) <= set(mapped.values()),
             f'{name}: missing or unmapped source headers')
        if mode == 'raw_meter':
            profile = cfg[mode]['profile']
            groups = (len({row[mapped['meter']].strip() for row in rows
                           if row[mapped['kind']].strip() == 'usage'})
                      if profile['tier_scope'] == 'meter' else 1)
            meter_rows += len(rows)
            meter_bands += groups * len(profile['tiers'])
            need(meter_rows <= MAX_METER_ROWS, 'aggregate meter record limit')
            need(meter_bands <= 12000, 'aggregate meter band evidence limit')
        out=io.StringIO(); writer=csv.DictWriter(out,fieldnames=[k for k in sorted(mapped) if mapped[k] in headers])
        writer.writeheader()
        for pos,row in enumerate(rows,2):
            normalized={k:row[v] for k,v in mapped.items() if v in row}
            identity=normalized.get('source_id' if mode=='raw_inventory' else 'id','').strip()
            key=source_collision_key(identity,4096,pos)
            need(key not in identities,f'{name} row {pos}: duplicate or aliased original source ID {identity}')
            identities.add(key); writer.writerow(normalized)
            if mode == 'raw_inventory' and normalized.get('kind', '').strip() in {'production_issue', 'production_return'}:
                component = normalized.get('economic_key', '').strip()
                key = source_collision_key(component, 4096, pos)
                need(key not in economic, f'{name} row {pos}: duplicate or aliased economic component {component}')
                economic.add(key)
        inner={k:v for k,v in schema.items() if k in {'dataset_id','plugin_id'}}
        inner.update(period=scope['period'],currency=scope['currency'],**{mode:cfg[mode]})
        req=adapter.from_text(out.getvalue(),inner,source_uri=source_uris[file],schema_uri=schema_uri)
        block=req.recognition
        actual=(block.get('context') if mode=='raw_recognition' else
                block['inventory_valuation']['context'] if mode=='raw_inventory' else block['meter_billing']['scope'])
        need(all(actual.get(k)==v for k,v in scope.items()),f'{name}: book, period or currency mismatch')
        # Distinct IDs cannot disguise a repeated recognized economic posting
        # in another source. Memo cash sharing a key remains non-posting.
        if mode=='raw_recognition':
            for posting in block['posted_costs']:
                for component in posting['economic_components']:
                    key=source_collision_key(component,4096,0)
                    need(key not in economic,f'{name}: duplicate or aliased recognized economic component {component}')
                    economic.add(key)
        branch_production = set()
        if mode == 'raw_inventory' and 'production_policy' in cfg[mode]:
            inventory = cfg[mode]
            for stream in inventory.get('streams', [inventory]):
                for object_id, target in stream.get('production_posting_map', {}).items():
                    need(classifications.get(object_id) == 'production',
                         f'{name}: inventory consumption requires a declared production object {object_id}')
                    branch_production.add((object_id, target['cost_element']))
            production_pairs.update(branch_production)
        for entry in req.entries:
            pair = (entry.object_id, entry.element_id)
            if pair not in branch_production:
                nonproduction_pairs.add(pair)
        row_map={n:n for n in range(2,len(rows)+2)}
        def refs(v): return _mapped_refs(real_refs(v,row_map,name,mode,source_uris[file],schema_uri),mapped)
        for obj in req.objects:
            obj=replace(obj,source_refs=refs(obj.source_refs))
            declarations = cfg.get('raw_recognition', {}).get('target_objects', [])
            obj=bind_operational_object(obj, declarations)
            if obj.object_id in objects:
                old=objects[obj.object_id]
                need(replace(old,source_refs=())==replace(obj,source_refs=()),
                     f'{name}: conflicting object definition {obj.object_id}')
                obj=replace(obj,source_refs=tuple(dict.fromkeys(old.source_refs+obj.source_refs)))
            objects[obj.object_id]=obj; owners.setdefault(obj.object_id,name)
            if mode == 'raw_recognition':
                recognition_owners.setdefault(obj.object_id, name)
        entries.extend(replace(e,entry_id=f'{name}:{e.entry_id}',source_refs=refs(e.source_refs)) for e in req.entries)
        pools.extend(replace(p,pool_id=f'{name}:{p.pool_id}',source_refs=refs(p.source_refs)) for p in req.pools)
        need(not(req.rules or req.drivers),'v4 branch allocation must be declared globally')
        audits[name]=refs(block)
        if first is None: first=req
    for object_id, _ in production_pairs:
        if object_id in recognition_owners:
            owners[object_id] = recognition_owners[object_id]
    dataset=schema.get('dataset_id','csv-unregistered')
    def config_ref(field,pos):
        return SourceRef(dataset_id=dataset,source_uri=schema_uri,field=field,
                         selector=f'raw_composite.{field}[{pos}]',kind='configuration')
    from .csv_adapter import _number, _factor_weights, _integer, _boolean
    for n,raw in enumerate(env.get('objects',[])):
        need(type(raw) is dict and {'object_id'} <= set(raw) <=
             {'object_id','name','kind','quantity','quantity_unit','currency'},'invalid global object')
        ident=raw['object_id']; need(isinstance(ident,str) and ident.strip() and ident not in objects,'duplicate global object')
        objects[ident]=CostObject(object_id=ident,name=raw.get('name',ident),kind=raw.get('kind','cost_center'),
             quantity=_number(raw.get('quantity'),'object.quantity',optional=True),
             quantity_unit=raw.get('quantity_unit','unit'),currency=raw.get('currency',scope['currency']),
             source_refs=(config_ref('objects',n),))
        owners[ident]='configuration'
    for ident, bound in operational_objects.items():
        if ident in objects:
            objects[ident] = bind_operational_object(objects[ident], env.get('objects', []))
        else:
            need(isinstance(bound.get('kind'), str) and bound['kind'].strip(),
                 f'new operational object {ident} requires explicit kind')
            obj = CostObject(object_id=ident, name=bound.get('name', ident), kind=bound['kind'],
                             currency=bound.get('currency', scope['currency']))
            objects[ident] = bind_operational_object(obj)
            owners[ident] = 'operational'
    for n,raw in enumerate(env.get('incoming_pools',[])):
        need(type(raw) is dict and {'pool_id','owner_id','element_id'} <= set(raw) <=
             {'pool_id','owner_id','element_id','name'},'invalid incoming pool')
        pools.append(CostPool(pool_id=raw['pool_id'],owner_id=raw['owner_id'],name=raw.get('name',raw['pool_id']),
             element_id=raw['element_id'],amount=Decimal(0),currency=scope['currency'],include_incoming=True,
             source_refs=(config_ref('incoming_pools',n),)))
    options=env.get('pool_options',{})
    need(set(options)<={p.pool_id for p in pools},'pool_options references unknown pool')
    pools=[replace(p,include_incoming=_boolean(options[p.pool_id].get('include_incoming'),'include_incoming',p.include_incoming),
                   variable_fraction=_number(options[p.pool_id].get('variable_fraction'),'variable_fraction',optional=True)
                       if 'variable_fraction' in options[p.pool_id] else p.variable_fraction)
           if p.pool_id in options else p for p in pools]
    rules=[]; drivers=[]
    for n,raw in enumerate(env.get('allocation_rules',[])):
        method=raw.get('method','driver'); did=raw.get('driver_id',f'bundle-driver-{n}')
        need(not _boolean(raw.get('allow_partial'),'allow_partial'),'v4 requires complete allocation drivers')
        ref=(config_ref('allocation_rules',n),)
        rule_ident = raw.get('rule_id', f'bundle-rule-{n}')
        bound_driver = operational_drivers.get(rule_ident)
        need(bound_driver is None or method not in {'direct', 'equal'},
             f'operational driver cannot bind {method} rule {rule_ident}')
        if method not in {'direct','equal'}:
            if bound_driver is not None:
                need('weight_factors' not in raw, 'operational and literal weight factors cannot both define a driver')
                values = {k: _number(v, f'operational weights.{k}') for k, v in bound_driver['weights'].items()}
                if 'weights' in raw:
                    declared = {k: _number(v, f'weights.{k}') for k, v in raw['weights'].items()}
                    need(declared == values, f'operational driver weights conflict with rule {rule_ident}')
                need('driver_unit' not in raw or raw['driver_unit'] == bound_driver['unit'],
                     f'operational driver unit conflicts with rule {rule_ident}')
                ref += op_refs(bound_driver.get('source_refs', []))
            else:
                values=(_factor_weights(raw,f'allocation_rules[{n}]') if 'weight_factors' in raw else
                        {k:_number(v,f'weights.{k}') for k,v in raw.get('weights',{}).items()})
            need(set(values)==set(raw['target_ids']) and all(v>=0 for v in values.values()) and
                 sum(values.values(),Decimal(0))>0, f'allocation_rules[{n}]: missing, negative or zero driver denominator')
            drivers.append(CostDriver(driver_id=did,name=raw.get('driver_name',did),unit=bound_driver['unit'] if bound_driver else raw.get('driver_unit','weight'),
                                      values=values,source_refs=ref))
        rules.append(AllocationRule(rule_id=raw.get('rule_id',f'bundle-rule-{n}'),pool_id=raw['pool_id'],
             target_ids=tuple(raw['target_ids']),method=method,driver_id=did if method not in {'direct','equal'} else '',
             stage=_integer(raw.get('stage'),'stage',1),rounding=_integer(raw.get('rounding'),'rounding',2),
             description=raw.get('description','')))
    _validate_dag(objects,pools,rules,drivers)
    normalize_pair = lambda pair: tuple(source_collision_key(part, 4096, 0) for part in pair)
    exclusive = {normalize_pair(pair) for pair in production_pairs}
    overlap = sorted(pair for pair in nonproduction_pairs if normalize_pair(pair) in exclusive)
    need(not overlap, f'production cost element is already supplied by inventory: {overlap}')
    pool_by_id = {pool.pool_id: pool for pool in pools}
    incoming_elements = {}
    allocation_pairs = set()
    for rule in sorted(rules, key=lambda item: item.stage):
        pool = pool_by_id[rule.pool_id]
        elements = {pool.element_id} if pool.amount else set()
        if pool.include_incoming:
            elements.update(incoming_elements.get(pool.owner_id, set()))
        for target in rule.target_ids:
            incoming_elements.setdefault(target, set()).update(elements)
            allocation_pairs.update((target, element) for element in elements)
    need(not (exclusive & {normalize_pair(pair) for pair in allocation_pairs}),
         'allocation cannot duplicate an inventory-supplied production cost element')
    by_rule={r.pool_id:n for n,r in enumerate(rules)}
    pools=[replace(p,source_refs=tuple(dict.fromkeys(p.source_refs+
                    (config_ref('allocation_rules',by_rule[p.pool_id]),)))) for p in pools]
    with localcontext() as ctx:
        ctx.prec=60
        total=sum((e.amount for e in entries+pools),Decimal(0))
        need(total==sum((Decimal(str(b['ledger_recognized_total'])) for b in audits.values()),Decimal(0)),
             'source and combined ledgers do not tie')
    rollups=env.get('object_rollups'); pool_owners={p.owner_id for p in pools}
    config_sha=hashlib.sha256(json.dumps({k:v for k,v in schema.items() if k!='dataset_id'},
                        ensure_ascii=False,sort_keys=True,separators=(',',':')).encode()).hexdigest()
    result=replace(first,objects=tuple(objects.values()),entries=tuple(entries),pools=tuple(pools),
         rules=tuple(rules),drivers=tuple(drivers),
         object_rollup_plan=compile_rollups(rollups,owners,pool_owners,dataset,schema_uri) if rollups is not None else None,
         object_budget_plan=compile_budgets(env['object_budgets'],scope,rollups,dataset,schema_uri) if 'object_budgets' in env else None,
         category_checklist_plan=category_checklist.compile_plan(env['category_checklist'],scope,rollups,dataset,schema_uri) if 'category_checklist' in env else None,
         recognition={'operation':'raw_composite','composite_version':4,'context':dict(scope),'inputs':audits,
          'can_claim_complete':False,'ledger_recognized_total':str(total),
          'source_recognition_status':'partial' if any(b['source_recognition_status']=='partial' for b in audits.values()) else 'accepted_declared_source_set',
          'unrecognized_nonzero_sources':[{**x,'input_name':name} for name,b in audits.items() for x in b['unrecognized_nonzero_sources']],
          'memo_sources':[],'derived_views':{},'posted_pool_totals':{k:b['ledger_recognized_total'] for k,b in audits.items()},
          'source_bundle':{'version':1,'sources':manifest,'config_sha256':config_sha,
                           'plugin_version':schema.get('plugin_version','undeclared'),
                           'input_mappings':{k:dict(v.get('columns',{})) for k,v in sections.items()}}})
    if operational is not None:
        result = replace(result, recognition={**result.recognition, 'operational_inputs': operational['metadata']})
    if 'business_views' in schema:
        from .business_views import compile_business_views
        result=replace(result,business_plan=compile_business_views(files,schema,source_uris,recognized_entries=tuple(entries)))
    return result


def _validate_dag(objects,pools,rules,drivers):
    for label,values in [('object',list(objects)),('pool',[p.pool_id for p in pools]),
                         ('rule',[r.rule_id for r in rules]),('driver',[d.driver_id for d in drivers])]:
        aliases=[source_collision_key(v,4096,0) for v in values]
        need(len(set(aliases))==len(aliases),f'duplicate or aliased {label} ID')
    by_pool={p.pool_id:p for p in pools}; by_rule={r.pool_id:r for r in rules}
    need(len(by_pool)==len(pools),'duplicate pool ID')
    need(len(by_rule)==len(rules) and set(by_rule)==set(by_pool),'each pool requires exactly one rule; duplicate consumption forbidden')
    need(len({r.rule_id for r in rules})==len(rules),'duplicate rule ID')
    need(len({d.driver_id for d in drivers})==len(drivers),'duplicate driver ID')
    incoming={}; stage={}
    for p in pools:
        need(p.owner_id in objects,'unknown pool owner')
        if p.include_incoming:
            need(p.owner_id not in incoming,'duplicate incoming cost consumption for owner')
            incoming[p.owner_id]=p.pool_id
        stage[p.owner_id]=min(stage.get(p.owner_id,10**9),by_rule[p.pool_id].stage)
    for r in rules:
        need(r.stage>=1 and r.rounding==2,'v4 allocations require positive stage and two-decimal rounding')
        need(r.method in {'driver','equal','direct','quantity','labor_hours','machine_hours','area','energy','revenue','orders'},'unsupported allocation method')
        need(r.method!='direct' or len(r.target_ids)==1,'direct allocation requires one target')
        for target in r.target_ids:
            need(target in objects,f'unknown allocation target {target}')
            if target in stage:
                need(stage[target]>r.stage,f'cyclic or out of stage order: {r.rule_id}->{target}')
                need(target in incoming,f'intermediate owner {target} requires include_incoming pool')
