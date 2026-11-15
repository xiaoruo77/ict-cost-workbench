import copy
import csv
import io
from urllib.parse import parse_qsl
import unittest

from cost_core.disposition_controls import compile_dispositions

SCOPE={'book':'SYNTH','period':'2026-11','currency':'CNY'}


def csvtext(rows):
    out=io.StringIO();w=csv.DictWriter(out,fieldnames=list(rows[0]));w.writeheader();w.writerows(rows)
    return out.getvalue()


def fixture():
    financial=[dict(**SCOPE,id='f-rework',tag='approved_rework',target_object_id='batch',amount='60.00',kind='expense',status='data'),
               dict(**SCOPE,id='f-materials',tag='materials',target_object_id='batch',amount='100.00',kind='expense',status='data')]
    events=[dict(**SCOPE,id='e-start',object_id='batch',op='start',quantity='10',unit='unit',ref=''),
            dict(**SCOPE,id='e-scrap',object_id='batch',op='scrap',quantity='2',unit='unit',ref=''),
            dict(**SCOPE,id='e-return',object_id='batch',op='return',quantity='1',unit='unit',ref='original-sale')]
    controls=[dict(**SCOPE,id='d1',object_id='batch',item_id='item-reworked',action='rework',ref='f-rework'),
              dict(**SCOPE,id='d2',object_id='batch',item_id='item-scrap-1',action='scrap',ref='e-scrap'),
              dict(**SCOPE,id='d3',object_id='batch',item_id='item-scrap-2',action='scrap',ref='e-scrap'),
              dict(**SCOPE,id='d4',object_id='batch',item_id='item-return',action='return',ref='e-return')]
    recognition={'version':2,'rules':[{'id':'rw','op':'recognize','source_tag':'approved_rework','source_kind':'expense'}],
                 'posting_map':{'rw':{'object_id_from':'target_object_id','record_type':'entry'}},'excluded_tags':{}}
    schema={'dataset_id':'csv-1111111111111111','raw_composite':{'scope':SCOPE,'inputs':{
               'finance':{'file':'finance','raw_recognition':recognition}}},
            'business_views':{'classifications':{'batch':'production','other':'production'},
               'lifecycle':{'file':'events','columns':{k:k for k in ('id','object_id','op','quantity','unit','ref')}},
               'dispositions':{'file':'dispositions','columns':{k:k for k in ('id','object_id','item_id','action','ref')},
                    'identity_basis':'synthetic approved unit identity v1','action_tags':{'rework':'approved_rework'}}}}
    return {'finance':csvtext(financial),'events':csvtext(events),'dispositions':csvtext(controls)},schema,controls


class DispositionTests(unittest.TestCase):
    def test_declared_identity_mutual_exclusion_and_real_refs_without_serial_claim(self):
        files,schema,_=fixture();plan=compile_dispositions(files,schema,{k:f'upload://sources/{k}.csv' for k in files})
        self.assertEqual(plan['counts'],{'rework':1,'return':1,'scrap':2})
        self.assertEqual(plan['evidence_scope'],'source_declared_disposition_identity;not_serial_trace')
        self.assertEqual(plan['coverage']['rework'],'complete_declared_positive_sources')
        for n,row in enumerate(plan['records'],2):
            self.assertEqual(dict(parse_qsl(row['source_refs'][0]['selector']))['row'],str(n))
            self.assertEqual(row['source_refs'][0]['source_uri'],'upload://sources/dispositions.csv')
            self.assertEqual(len(row['source_refs']),2)
        self.assertEqual(plan['records'][0]['source_refs'][1]['source_uri'],'upload://sources/finance.csv')

    def test_absent_configuration_returns_none_without_inventing_trace(self):
        self.assertIsNone(compile_dispositions({},{}))
        files,schema,_=fixture();schema['business_views'].pop('dispositions')
        self.assertIsNone(compile_dispositions(files,schema))

    def test_same_item_across_any_action_or_unicode_alias_rejected(self):
        for value in ('item-reworked','ＩＴＥＭ-ＲＥＷＯＲＫＥＤ'):
            files,schema,rows=fixture();rows[1]['item_id']=value;files['dispositions']=csvtext(rows)
            with self.subTest(value=value),self.assertRaisesRegex(ValueError,'already disposed'):
                compile_dispositions(files,schema)

    def test_source_type_action_object_and_unapproved_rework_mapping_fail(self):
        for change in ('wrong-financial-tag','missing-ref','wrong-object','event-as-financial','financial-as-event',
                       'wrong-event-action','unknown-action','no-approved-tag'):
            files,schema,rows=fixture()
            if change=='wrong-financial-tag':rows[0]['ref']='f-materials'
            if change=='missing-ref':rows[0]['ref']='absent'
            if change=='wrong-object':rows[0]['object_id']='other'
            if change=='event-as-financial':rows[0]['ref']='e-scrap'
            if change=='financial-as-event':rows[1]['ref']='f-rework'
            if change=='wrong-event-action':rows[1]['ref']='e-return'
            if change=='unknown-action':rows[1]['action']='dispose'
            if change=='no-approved-tag':schema['business_views']['dispositions'].pop('action_tags')
            files['dispositions']=csvtext(rows)
            with self.subTest(change=change),self.assertRaises(ValueError):compile_dispositions(files,schema)

    def test_deleted_or_extra_controls_cannot_bypass_scope_coverage(self):
        for change in ('missing-rework','missing-scrap','extra-scrap','missing-return','fractional-quantity'):
            files,schema,rows=fixture()
            if change=='missing-rework':rows.pop(0)
            if change=='missing-scrap':rows.pop(1)
            if change=='missing-return':rows.pop()
            if change=='extra-scrap':rows.append(dict(rows[1],id='extra',item_id='extra-item'))
            if change=='fractional-quantity':files['events']=files['events'].replace('scrap,2,','scrap,1.5,')
            files['dispositions']=csvtext(rows)
            with self.subTest(change=change),self.assertRaises(ValueError):compile_dispositions(files,schema)

    def test_financial_and_disposition_mapping_preserve_actual_headers(self):
        files,schema,rows=fixture()
        files['finance']=files['finance'].replace(',id,',',voucher,').replace('target_object_id','target').replace(',tag,',',category,')
        schema['raw_composite']['inputs']['finance']['columns']={'id':'voucher','target_object_id':'target','tag':'category'}
        for row in rows:row['record_id']=row.pop('id');row['declared_unit']=row.pop('item_id')
        schema['business_views']['dispositions']['columns'].update(id='record_id',item_id='declared_unit')
        files['dispositions']=csvtext(rows)
        plan=compile_dispositions(files,schema)
        ref=plan['records'][0]['source_refs'][1]
        self.assertEqual(dict(parse_qsl(ref['selector']))['voucher'],'f-rework')
        self.assertEqual(plan['records'][0]['source_refs'][0]['field'],'declared_unit')

    def test_scope_ragged_columns_and_duplicate_record_ids_fail(self):
        for change in ('book','period','currency','extra-column','duplicate-id','missing-file'):
            files,schema,rows=fixture()
            if change in SCOPE:rows[0][change]='wrong'
            if change=='extra-column':
                for row in rows:row['unmapped']='bad'
            if change=='duplicate-id':rows[1]['id']=rows[0]['id']
            files['dispositions']=csvtext(rows)
            if change=='missing-file':files.pop('dispositions')
            with self.subTest(change=change),self.assertRaises(ValueError):compile_dispositions(files,schema)

    def test_positive_rework_cannot_disappear_by_excluding_or_breaking_its_mapping(self):
        for change in ('excluded','unmapped','status-whitespace'):
            files,schema,rows=fixture();rows.pop(0);files['dispositions']=csvtext(rows)
            rec=schema['raw_composite']['inputs']['finance']['raw_recognition']
            if change=='excluded':rec['excluded_tags']['approved_rework']='pretend out of scope'
            if change=='unmapped':rec['posting_map']={}
            if change=='status-whitespace':files['finance']=files['finance'].replace(',data',', data ')
            with self.subTest(change=change),self.assertRaises(ValueError):compile_dispositions(files,schema)


if __name__=='__main__':unittest.main()
