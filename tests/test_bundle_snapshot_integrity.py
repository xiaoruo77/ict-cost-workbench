import copy
import json
from pathlib import Path
import tempfile
import unittest
from cost_core.csv_adapter import CsvCostingAdapter
from cost_core.costing import CostingEngine
from costing_outputs import create_snapshot
from test_source_bundle import fixture


class BundleSnapshotIntegrityTests(unittest.TestCase):
    def test_captured_sources_bind_calculated_bytes_and_configuration(self):
        for mutation in ('none','source','schema'):
            with self.subTest(mutation=mutation), tempfile.TemporaryDirectory() as temp:
                root=Path(temp);files,schema=fixture();schema['dataset_id']='csv-0123456789abcdef'
                result=CostingEngine().analyze(CsvCostingAdapter().from_files(files,schema))
                folder=root/'costing_datasets'/schema['dataset_id'];(folder/'sources').mkdir(parents=True)
                for name,text in files.items():
                    (folder/'sources'/(name+'.csv')).write_bytes(text.encode())
                manifest={'schema':copy.deepcopy(schema)}
                if mutation=='source':(folder/'sources/one.csv').write_text(files['one'].replace('10.00','11.00'),encoding='utf8')
                if mutation=='schema':manifest['schema']['raw_composite']['allocation_rules'][1]['weights']['A']=3
                (folder/'manifest.json').write_text(json.dumps(manifest),encoding='utf8')
                context=dict(dataset_id=schema['dataset_id'],period='2026-11',scenario='actual',owner_external_id='synthetic')
                if mutation=='none':
                    snapshot=create_snapshot(result,root,root,context)
                    self.assertTrue(all(e['status'] in {'captured','captured_mapping'} for e in snapshot['evidence']))
                else:
                    with self.assertRaisesRegex(ValueError,'计算后'):
                        create_snapshot(result,root,root,context)
                    self.assertFalse(list((root/'costing_snapshots').glob('*.json')))
