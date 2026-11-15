import sys,unittest
from pathlib import Path
from decimal import Decimal as D
from dataclasses import replace
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
from cost_core.models import DatasetContext,CostObservation,AnalysisInput
from cost_core.registry import (PluginMetadata,PluginRegistry,DatasetDefinition,AnalysisRequest,
                                CostService,UnknownDatasetError,UnknownPluginError,to_primitive)

class FakePlugin:
    metadata=PluginMetadata(plugin_id='test',name='Test adapter',version='1',capabilities=('monthly_cost',))
    def describe(self,d):return {'dataset_id':d.dataset_id,'plugin_id':d.plugin_id}
    def load(self,d,r):
        ctx=DatasetContext(dataset_id=d.dataset_id,plugin_id='test',currency='TST',quantity_unit='item',
                           self_org='A',peer_org='',category_labels={'x':'X','y':'Y'})
        value=D('2') if d.dataset_id=='alpha' else D('7')
        row=CostObservation(dataset_id=d.dataset_id,organization='A',cost_object=r.cost_object,
              period=r.period,quantity=D('10'),unit_costs={'x':value,'y':D('1')},
              reported_unit_cost=value+D('1'),reported_total=(value+D('1'))*10,currency='TST',quantity_unit='item')
        return AnalysisInput(context=ctx,cost_object=r.cost_object,period=r.period,current=row,history=(row,))
    def preview_report(self,d,r):return {'report':r.cost_object+' draft','model_calls':0,
        'dataset_id':d.dataset_id,'plugin_id':'test','cost_object':r.cost_object,'period':r.period,'organization':'A'}

class RegistryTests(unittest.TestCase):
    def setUp(self):
        self.registry=PluginRegistry();self.registry.register(FakePlugin())
        self.service=CostService(self.registry,tuple(DatasetDefinition(dataset_id=i,plugin_id='test',
              workspace_root=Path('/not-read'),label=i) for i in ('alpha','beta')))
    def test_same_object_and_period_in_two_datasets_are_isolated(self):
        a=self.service.analyze_payload(AnalysisRequest(dataset_id='alpha',cost_object='same',period='2030-02'))
        b=self.service.analyze_payload(AnalysisRequest(dataset_id='beta',cost_object='same',period='2030-02'))
        self.assertEqual(a['metrics']['unit_cost']['value'],'3')
        self.assertEqual(b['metrics']['unit_cost']['value'],'8')
        self.assertNotEqual(a['metrics']['unit_cost']['fact_id'],b['metrics']['unit_cost']['fact_id'])
        self.assertEqual(a['metrics']['unit_cost']['display'],'3.00')
    def test_unknown_ids_never_fall_back(self):
        with self.assertRaises(UnknownDatasetError):self.service.analyze(AnalysisRequest(dataset_id='../../.env',cost_object='same',period='2030-02'))
        with self.assertRaises(UnknownPluginError):self.service.analyze(AnalysisRequest(dataset_id='alpha',plugin_id='other',cost_object='same',period='2030-02'))
    def test_plugin_cannot_return_another_dataset(self):
        plugin=self.registry.get('test');original=plugin.load
        def wrong(d,r):return original(replace(d,dataset_id='beta'),r)
        plugin.load=wrong
        with self.assertRaises(ValueError):self.service.analyze(AnalysisRequest(dataset_id='alpha',cost_object='same',period='2030-02'))
    def test_preview_is_explicitly_offline(self):
        value=self.service.preview_report(AnalysisRequest(dataset_id='alpha',cost_object='same',period='2030-02'))
        self.assertTrue(value['dry_run']);self.assertEqual(value['model_calls'],0)
        original=self.registry.get('test').preview_report
        self.registry.get('test').preview_report=lambda d,r:{**original(d,r),'model_calls':1}
        with self.assertRaises(ValueError):self.service.preview_report(AnalysisRequest(dataset_id='alpha',cost_object='same',period='2030-02'))
    def test_report_preview_cannot_hide_a_wrong_scope(self):
        original=self.registry.get('test').preview_report
        self.registry.get('test').preview_report=lambda d,r:{**original(d,r),'dataset_id':'beta'}
        with self.assertRaises(ValueError):self.service.preview_report(AnalysisRequest(dataset_id='alpha',cost_object='same',period='2030-02'))
    def test_frozen_mapping_serialization_is_supported(self):
        result=self.service.analyze(AnalysisRequest(dataset_id='alpha',cost_object='same',period='2030-02'))
        self.assertEqual(to_primitive(result)['context']['quantity_unit'],'item')

if __name__=='__main__':unittest.main()
