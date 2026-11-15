"""Graph freshness/cutoff and arithmetic-validator failure-path regressions."""
from pathlib import Path
from unittest.mock import patch
import csv
import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import graph_build as GB
import finalize as F
HAS_TOOL_RUNTIME = importlib.util.find_spec('langchain_core') is not None


class LegacyGraphRegressions(unittest.TestCase):
    def fixture(self, root):
        for folder in ('raw/csv/cost_data', 'wiki/equipment', 'wiki/products', 'wiki/costs'):
            (root / folder).mkdir(parents=True)
        equipment = ['# Equipment', '| 设备编号 | 设备名称 | 型号 | 数量 | 厂商 | 年份 | 原值 | 月折旧 |',
                     '|---|---|---|---|---|---|---|---|']
        equipment += [f'| EQ-AA-{i:03} | Device{i} | fixture | 1 | fixture | 2020 | 100 | 1 |'
                      for i in range(1, 30)]
        equipment += ['| 日期 | 设备 | 故障描述 | 维修费(元) | 停工(h) | 影响 |',
                      '|---|---|---|---|---|---|']
        equipment += [f'| {period} | Device{i} EQ-AA-{i:03} | Repair | 1 | 1 | pending |'
                      for i, period in enumerate(('2026-01', '2026-03', '2026-05', '2026-07'), 1)]
        (root / 'wiki/equipment/设备台账与维修历史.md').write_text('\n'.join(equipment), encoding='utf-8')
        for product, count in enumerate((7, 7, 6)):
            rows = [f'# P{product}', '| 工序 | 参数 | 收率 | 关键设备 | 影响 |', '|---|---|---|---|---|']
            rows += [f'| Step{i} | fixture | unknown | EQ-AA-{i + 1:03} | pending |' for i in range(count)]
            (root / f'wiki/products/P{product}.md').write_text('\n'.join(rows), encoding='utf-8')
        (root / 'wiki/costs/成本异动登记册.md').write_text(
            '# Incidents\n| # | 波动事件 | 涉及产品 | 发生月份 | 官方根因 |\n|---|---|---|---|---|\n'
            '| 1 | Older observed change | P0 | 2026-04 | unverified explanation |\n'
            '| 2 | Future event | P0 | 2026-07 | must not leak |\n', encoding='utf-8')
        with (root / GB._MATERIAL_FILE).open('w', encoding='utf-8', newline='') as stream:
            writer = csv.writer(stream)
            writer.writerow(['产品名称', '月份', '原材料名称', '占总材料成本比例', '单位消耗成本(元/盒)'])
            for period, cost in (('2026-05', '1'), ('2026-06', '9')):
                for i in range(15):
                    writer.writerow([f'P{i // 5}', period, f'M{i}', '20%', cost])
        (root / GB._PEER_FILE).write_text('产品名称\nP0\nP1\nP2\n', encoding='utf-8')

    def test_latest_material_period_and_historical_cutoff_are_explicit(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            self.fixture(root)
            latest = GB.build(False, root)
            self.assertEqual(latest['as_of'], '2026-06')
            self.assertEqual(latest['schema_version'], 'pharma-graph/2')
            self.assertEqual(latest['source_scope'], 'authorized_workspace')
            self.assertTrue(latest['saved'])
            self.assertEqual(latest['errors'], [])
            material_edges = [edge for edge in latest['edges'] if edge['relation'] == '处方含']
            self.assertEqual(len(material_edges), 15)
            self.assertTrue(all(edge['attrs']['单位消耗成本(元/盒)'] == '9' for edge in material_edges))
            self.assertTrue(all('2026-06' in edge['coord'] for edge in material_edges))
            self.assertNotIn('Future event', json.dumps(latest))
            self.assertFalse(any(node['type'] == 'incident' and node['attrs']['日期'] > '2026-06'
                                 for node in latest['nodes']))
            historical = GB.build(False, root, as_of='2026-05')
            self.assertTrue(historical['saved'])
            self.assertTrue(all(edge['attrs']['单位消耗成本(元/盒)'] == '1'
                                for edge in historical['edges'] if edge['relation'] == '处方含'))

    def test_source_signature_changes_for_content_or_new_product_and_rejects_escape(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder) / 'workspace'
            self.fixture(root)
            before = GB.input_signature(root)
            path = root / 'wiki/products/P0.md'
            path.write_text(path.read_text(encoding='utf-8') + '\nNew source content', encoding='utf-8')
            changed = GB.input_signature(root)
            self.assertNotEqual(before, changed)
            extra = root / 'wiki/products/Extra.md'
            extra.write_text('# New product', encoding='utf-8')
            self.assertNotEqual(changed, GB.input_signature(root))
            outside = Path(folder) / 'outside.md'
            outside.write_text('outside', encoding='utf-8')
            try:
                (root / 'wiki/products/Escape.md').symlink_to(outside)
            except (OSError, NotImplementedError):
                return
            with self.assertRaises(ValueError):
                GB.input_signature(root)

    def test_source_change_while_building_preserves_prior_derived_file(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            self.fixture(root)
            GB.build(False, root)
            target = root / 'graph/graph.json'
            original_bytes = target.read_bytes()
            original = GB.extract_materials
            def changing(*args, **kwargs):
                original(*args, **kwargs)
                source = root / GB._MATERIAL_FILE
                source.write_text(source.read_text(encoding='utf-8').replace(',9', ',999'), encoding='utf-8')
            with patch.object(GB, 'extract_materials', side_effect=changing):
                with self.assertRaisesRegex(ValueError, '构建期间变化'):
                    GB.build(False, root)
            self.assertEqual(target.read_bytes(), original_bytes)

    @unittest.skipUnless(HAS_TOOL_RUNTIME, 'LangChain report-tools consumer is checked in target Docker')
    def test_graph_consumer_rejects_old_or_stale_graph_and_accepts_current_input(self):
        import report_tools as RT
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            self.fixture(root)
            current = GB.build(False, root)
            target = root / 'graph/graph.json'
            self.assertIsNotNone(RT._load_graph(root))
            old = dict(current)
            old.pop('source_signature')
            old.pop('schema_version')
            target.write_text(json.dumps(old), encoding='utf-8')
            self.assertIsNone(RT._load_graph(root))
            target.write_text(json.dumps(current), encoding='utf-8')
            article = root / 'wiki/products/P0.md'
            article.write_text(article.read_text(encoding='utf-8') + '\nChanged input', encoding='utf-8')
            self.assertIsNone(RT._load_graph(root))

    def test_invalid_graph_never_overwrites_valid_graph_or_prints_success(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            self.fixture(root)
            GB.build(False, root)
            target = root / 'graph/graph.json'
            original_bytes = target.read_bytes()
            article = root / 'wiki/products/P0.md'
            article.write_text(article.read_text(encoding='utf-8').replace('EQ-AA-001', 'TQ-01'), encoding='utf-8')
            invalid = GB.build(False, root)
            self.assertTrue(invalid['errors'])
            self.assertFalse(invalid['saved'])
            self.assertFalse(any('✅' in warning for warning in invalid['warnings']))
            self.assertEqual(target.read_bytes(), original_bytes)

    def test_arithmetic_missing_or_empty_workspace_is_unverified(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder) / 'workspace'
            for mode in ('missing', 'empty', 'index-only'):
                if mode != 'missing':
                    (root / 'wiki').mkdir(parents=True, exist_ok=True)
                if mode == 'index-only':
                    (root / 'wiki/index.md').write_text('# Index', encoding='utf-8')
                result = subprocess.run([sys.executable, str(ROOT / 'scripts/check_math.py'), '--wiki-root', str(root)],
                                        capture_output=True, text=True)
                self.assertNotEqual(result.returncode, 0, mode)
                self.assertIn('未验证', result.stdout)
                self.assertFalse(F.finalize(only='math', quiet=True, workspace_root=root)['clean'])


if __name__ == '__main__':
    unittest.main()
