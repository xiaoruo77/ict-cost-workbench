"""Independent 10 + 20 -> (1:2) = 10,20 oracle; no engine-derived expectations."""
import copy
import hashlib
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from decimal import Decimal as D
from urllib.parse import parse_qsl
import unittest

from cost_core.csv_adapter import CsvCostingAdapter
from cost_core.costing import CostingEngine
from test_adapter_recognition import convert


def fixture():
    scope={'book':'synthetic','period':'2026-11','currency':'CNY'}
    files={}; inputs={}
    for name,amount in [('one','10.00'),('two','20.00')]:
        doc={'name':name,'context':scope,'excluded_sources':{},
             'sources':[{**scope,'id':f'raw-{name}','kind':'expense','status':'data','amount':amount,
                         'economic_key':f'economic-{name}','evidence':f'synthetic-{name}'}],
             'rules':[{'id':f'{name}_rule','op':'recognize','source':f'raw-{name}','posting_pool':name}],
             'outputs':[f'{name}_rule']}
        text,schema=convert(doc,pool=True)
        files[name]=text
        inputs[name]={'file':name,'raw_recognition':schema['raw_recognition']}
    schema={'dataset_id':'synthetic-bundle','raw_composite':{'version':4,'scope':scope,'inputs':inputs,
         'pool_options':{'two:two_rule':{'include_incoming':True}},
         'allocation_rules':[
             {'rule_id':'transfer','pool_id':'one:one_rule','target_ids':['site_two'],'method':'direct','stage':1},
             {'rule_id':'distribute','pool_id':'two:two_rule','target_ids':['A','B'],'weights':{'A':1,'B':2},'stage':2}]}}
    return files,schema


class SourceBundleTests(unittest.TestCase):
    def test_two_files_cross_source_two_stages_exact_conservation_and_original_refs(self):
        files,schema=fixture()
        req=CsvCostingAdapter().from_files(files,schema)
        out=CostingEngine().analyze(req)
        totals={r['object_id']:D(r['total_cost']) for r in out['objects']}
        self.assertEqual(totals,{'site_one':D(0),'site_two':D(0),'A':D(10),'B':D(20)})
        self.assertEqual(D(out['totals']['total_cost']),D(30))
        self.assertEqual(D(out['totals']['unallocated_cost']),D(0))
        self.assertTrue(out['ledger_reconciled'])
        self.assertEqual([D(a['amount']) for a in out['allocations']],[D(10),D(10),D(20)])
        second=out['allocations'][1]
        self.assertEqual({r['source_uri'] for r in second['source_refs'] if r['kind']=='raw'},
                         {'upload://sources/one.csv','upload://sources/two.csv'})
        for pool in req.pools:
            ref=pool.source_refs[0]; selector=dict(parse_qsl(ref.selector))
            self.assertEqual(selector['row'],'2')
            self.assertEqual(selector['id'],'raw-'+ref.source_uri.rsplit('/',1)[-1].removesuffix('.csv'))
            self.assertNotIn('input_name',selector)
        manifest=req.recognition['source_bundle']
        self.assertEqual({x['name']:x['sha256'] for x in manifest['sources']},
                         {k:hashlib.sha256(v.encode()).hexdigest() for k,v in files.items()})
        self.assertEqual(len(manifest['config_sha256']),64)

    def test_explicit_zero_incoming_pool_preserves_upstream_and_local_sources(self):
        files,schema=fixture(); env=schema['raw_composite']
        env.pop('pool_options')
        env['objects']=[{'object_id':'bridge','kind':'cost_center'}]
        env['incoming_pools']=[{'pool_id':'global:bridge','owner_id':'bridge','element_id':'shared'}]
        env['allocation_rules']=[
            {'pool_id':f'{name}:{name}_rule','target_ids':['bridge'],'method':'direct','stage':1}
            for name in ('one','two')]+[
            {'pool_id':'global:bridge','target_ids':['A','B'],'weights':{'A':1,'B':2},'stage':2}]
        result=CostingEngine().analyze(CsvCostingAdapter().from_files(files,schema))
        totals={r['object_id']:D(r['total_cost']) for r in result['objects']}
        self.assertEqual((totals['A'],totals['B'],totals['bridge']),(D(10),D(20),D(0)))

    def test_mapping_keeps_real_header_and_row(self):
        files,schema=fixture()
        files['one']=files['one'].replace('amount,','actual_amount,').replace(',id,',',voucher_id,')
        schema['raw_composite']['inputs']['one']['columns']={'amount':'actual_amount','id':'voucher_id'}
        req=CsvCostingAdapter().from_files(files,schema)
        self.assertEqual(req.pools[0].source_refs[0].field,'actual_amount')
        self.assertEqual(dict(parse_qsl(req.pools[0].source_refs[0].selector))['row'],'2')
        self.assertEqual(dict(parse_qsl(req.pools[0].source_refs[0].selector))['voucher_id'],'raw-one')
        self.assertEqual(req.recognition['source_bundle']['input_mappings']['one'],{'amount':'actual_amount','id':'voucher_id'})

    def test_snapshot_captures_independent_source_headers_and_global_rule(self):
        from costing_outputs import create_snapshot
        files,schema=fixture();schema['dataset_id']='csv-1111111111111111'
        req=CsvCostingAdapter().from_files(files,schema)
        result=CostingEngine().analyze(req)
        with TemporaryDirectory() as tmp:
            root=Path(tmp);ds=root/'costing_datasets'/schema['dataset_id'];(ds/'sources').mkdir(parents=True)
            for name,text in files.items():(ds/'sources'/(name+'.csv')).write_bytes(text.encode())
            (ds/'manifest.json').write_text(json.dumps({'schema':schema}),encoding='utf-8')
            snap=create_snapshot(result,root,root,{'dataset_id':schema['dataset_id'],'dataset_label':'independent bundle',
                       'period':'2026-11','scenario':'actual','owner_external_id':'synthetic'})
            raw=[e for e in snap['evidence'] if e['source_uri'].startswith('upload://sources/')]
            self.assertTrue(raw)
            self.assertTrue(all(e['status']=='captured' for e in raw),raw)
            self.assertTrue(all(e['rows'][0]['record']==2 for e in raw))
            config={e['selector']:e for e in snap['evidence'] if e['kind']=='configuration'}
            self.assertEqual(config['raw_composite.allocation_rules[1]']['mapping']['configuration']['weights'],{'A':1,'B':2})

    def test_duplicate_and_unicode_alias_originals_rejected_across_files(self):
        for new in ('raw-one','ＲＡＷ-ＯＮＥ'):
            files,schema=fixture();files['two']=files['two'].replace('raw-two',new)
            schema['raw_composite']['inputs']['two']['raw_recognition']['rules'][0]['source']=new
            with self.subTest(new=new),self.assertRaisesRegex(ValueError,'duplicate or aliased'):
                CsvCostingAdapter().from_files(files,schema)

    def test_distinct_raw_ids_cannot_hide_duplicate_economic_posting(self):
        files,schema=fixture()
        files['two']=files['two'].replace('economic-two','ECONOMIC-ONE')
        with self.assertRaisesRegex(ValueError,'recognized economic component'):
            CsvCostingAdapter().from_files(files,schema)

    def test_aliased_cost_object_configuration_is_rejected(self):
        files,schema=fixture()
        schema['raw_composite']['objects']=[{'object_id':'Ａ','kind':'service'}]
        with self.assertRaisesRegex(ValueError,'aliased object'):
            CsvCostingAdapter().from_files(files,schema)

    def test_invalid_dag_duplicate_consumption_missing_and_zero_driver_fail_at_compile(self):
        for change in ('cycle','order','duplicate','missing','zero','negative','incomplete','unabsorbed'):
            files,schema=fixture();env=schema['raw_composite'];rules=env['allocation_rules']
            if change=='cycle':rules[1]['target_ids']=['site_one'];rules[1]['weights']={'site_one':1}
            if change=='order':rules[1]['stage']=1
            if change=='duplicate':rules.append(copy.deepcopy(rules[1]))
            if change=='missing':rules.pop()
            if change=='zero':rules[1]['weights']={'A':0,'B':0}
            if change=='negative':rules[1]['weights']={'A':-1,'B':2}
            if change=='incomplete':rules[1]['weights']={'A':1}
            if change=='unabsorbed':env.pop('pool_options')
            with self.subTest(change=change),self.assertRaises(ValueError):
                CsvCostingAdapter().from_files(files,schema)

    def test_file_population_scope_and_source_hash_fail_closed(self):
        for change in ('missing','extra','hash','scope','same_file','uri_alias'):
            files,schema=fixture();opts={}
            if change=='missing':files.pop('two')
            if change=='extra':files['ignored']=files['one']
            if change=='hash':schema['raw_composite']['source_hashes']={'one':'0'*64}
            if change=='scope':files['two']=files['two'].replace('synthetic','other-book')
            if change=='same_file':schema['raw_composite']['inputs']['two']['file']='one'
            if change=='uri_alias':opts={'source_uris':{'one':'upload://same.csv','two':'upload://same.csv'}}
            with self.subTest(change=change),self.assertRaises(ValueError):
                CsvCostingAdapter().from_files(files,schema,**opts)

    def test_configuration_hash_excludes_dataset_identity_and_binds_plugin_version(self):
        files,schema=fixture();schema['plugin_version']='synthetic-v1'
        first=CsvCostingAdapter().from_files(files,schema).recognition['source_bundle']['config_sha256']
        schema['dataset_id']='another-private-import'
        same=CsvCostingAdapter().from_files(files,schema).recognition['source_bundle']['config_sha256']
        self.assertEqual(first,same)
        schema['plugin_version']='synthetic-v2'
        changed=CsvCostingAdapter().from_files(files,schema).recognition['source_bundle']['config_sha256']
        self.assertNotEqual(first,changed)

    def test_v4_is_explicit_named_file_operation(self):
        files,schema=fixture()
        with self.assertRaisesRegex(ValueError,'named files'):
            CsvCostingAdapter().from_text(files['one'],schema)


if __name__=='__main__':unittest.main()
