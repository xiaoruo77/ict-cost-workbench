"""Failure paths, isolation and preservation tests using only temporary fixtures."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
import importlib.util
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import agent_compact as AC
import db_build as DB
import eval_report as ER
import finalize as F
import ingest_raw as IR
import make_sealed_context as PK
import overlay as OV
import state as S

HAS_TOOL_RUNTIME = importlib.util.find_spec('langchain_core') is not None
HAS_PDF_RUNTIME = importlib.util.find_spec('fitz') is not None


def metric_doc(value):
    return {'wiki_signature': 'fixture', 'metrics': [{'id': 'fixed-id', 'entity': 'widget',
            'metric': 'cost', 'period': '2026-05', 'value': value, 'article': 'product.md'}]}


class LegacyIngestRegressions(unittest.TestCase):
    @unittest.skipUnless(HAS_PDF_RUNTIME, 'PyMuPDF runtime is required for actual corrupt-PDF check')
    def test_corrupt_pdf_fails_without_success_hook_derived_text_or_hash(self):
        events = []
        class Hook(IR.IngestHook):
            def on_collected(self, result): events.append('collected')
            def on_error(self, result): events.append('error')
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            result = IR.ingest_file(b'%PDF-invalid-file', filename='broken.pdf', wiki_root=root, hooks=[Hook()])
            self.assertEqual(result.status, 'error')
            self.assertIn('invalid_document', result.error)
            self.assertIsNone(result.raw_path)
            self.assertEqual(events, ['error'])
            self.assertFalse((root / 'raw/.hashes.json').exists())
            self.assertFalse(list(root.rglob('*.txt')))
            self.assertFalse(list(root.rglob('broken.pdf')))
            again = IR.ingest_file(b'%PDF-invalid-file', filename='broken.pdf', wiki_root=root, hooks=[])
            self.assertEqual(again.status, 'error', 'failed input must remain retryable')

    @unittest.skipUnless(HAS_PDF_RUNTIME, 'PyMuPDF runtime is required')
    def test_valid_pdf_preserves_bytes_and_page_coordinates(self):
        import fitz
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            doc = fitz.open()
            page = doc.new_page()
            page.insert_text((72, 72), 'Fixture cost document 123.45')
            original = doc.tobytes()
            doc.close()
            result = IR.ingest_file(original, filename='valid.pdf', wiki_root=root, hooks=[])
            self.assertEqual(result.status, 'collected')
            self.assertEqual((root / result.raw_path).read_bytes(), original)
            derived = (root / result.derived_text).read_text(encoding='utf-8')
            self.assertIn('PAGE 1/1', derived)
            self.assertIn('123.45', derived)
            self.assertNotIn('解析失败', derived)
            self.assertEqual(IR.ingest_file(original, filename='same.pdf', wiki_root=root, hooks=[]).status, 'skipped')

    def test_filename_and_forced_topic_cannot_escape_raw(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder) / 'workspace'
            for filename, topic in [('../outside.txt', None), ('C:\\outside.txt', None),
                                    ('inside.txt', '../wiki'), ('inside.txt', '/tmp/escape')]:
                result = IR.ingest_file(b'safe fixture', filename=filename, topic=topic, wiki_root=root, hooks=[])
                self.assertEqual(result.status, 'error')
            self.assertFalse((Path(folder) / 'outside.txt').exists())


class LegacyBoundaryRegressions(unittest.TestCase):
    def test_overlay_rejects_traversal_absolute_non_markdown_and_symlink_escape(self):
        with tempfile.TemporaryDirectory() as folder:
            base = Path(folder)
            shared = base / 'shared'
            (shared / 'wiki/sub').mkdir(parents=True)
            (base / 'marker.md').write_text('private marker', encoding='utf-8')
            (shared / 'wiki/sub/valid.md').write_text('# Valid', encoding='utf-8')
            for bad in ('sub/../../../marker.md', str(base / 'marker.md'), 'C:\\marker.md',
                        'sub/valid.txt', 'sub/*.md', '..\\marker.md'):
                self.assertIsNone(OV.overlay_lookup(bad, shared))
            self.assertIsNotNone(OV.overlay_lookup('sub/valid.md', shared))
            try:
                (shared / 'wiki/leak.md').symlink_to(base / 'marker.md')
            except (NotImplementedError, OSError):
                return  # Windows may require a symlink privilege; other assertions still ran.
            self.assertIsNone(OV.overlay_lookup('leak.md', shared))
            self.assertEqual([p.name for p, _ in OV.overlay_articles(shared)], ['valid.md'])

    @unittest.skipUnless(HAS_TOOL_RUNTIME, 'LangChain tool runtime is checked in target Docker')
    def test_ingest_tools_cannot_read_other_resource_or_write_raw(self):
        import ingest_tools as IT
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / 'raw').mkdir()
            (root / 'wiki').mkdir()
            (root / 'raw/source.txt').write_text('immutable', encoding='utf-8')
            (root / 'wiki/secret.md').write_text('wiki only', encoding='utf-8')
            tools = {tool.name: tool for tool in IT.build_ingest_tools(root, shared_root=None)}
            for operation, args in [('read_raw', {'path': 'wiki/secret.md'}),
                                    ('grep_raw', {'path': '../wiki', 'pattern': '.'}),
                                    ('list_raw_files', {'topic': '../wiki'}),
                                    ('write_wiki_article', {'path': '../raw/source.txt', 'content': 'changed'}),
                                    ('write_wiki_article', {'path': 'raw/new.md', 'content': 'changed'}),
                                    ('patch_wiki_article', {'path': '../raw/source.txt', 'old_text': 'immutable', 'new_text': 'changed'})]:
                with self.assertRaises(ValueError, msg=operation):
                    tools[operation].invoke(args)
            self.assertEqual((root / 'raw/source.txt').read_text(encoding='utf-8'), 'immutable')
            self.assertIn('已新建', tools['write_wiki_article'].invoke({'path': 'products/new.md', 'content': '# New'}))

    @unittest.skipUnless(HAS_TOOL_RUNTIME, 'LangChain tool runtime is checked in target Docker')
    def test_bound_tools_preserve_two_workspace_contexts_concurrently(self):
        import ingest_tools as IT
        with tempfile.TemporaryDirectory() as folder:
            readers = []
            for name in ('alice', 'bob'):
                root = Path(folder) / name
                (root / 'raw').mkdir(parents=True)
                (root / 'raw/input.txt').write_text(name, encoding='utf-8')
                readers.append(next(tool for tool in IT.build_ingest_tools(root, None) if tool.name == 'read_raw'))
            with ThreadPoolExecutor(2) as pool:
                answers = list(pool.map(lambda tool: tool.invoke({'path': 'raw/input.txt'}), readers))
            self.assertIn('alice', answers[0])
            self.assertNotIn('bob', answers[0])
            self.assertIn('bob', answers[1])


class LegacyStateAndDatabaseRegressions(unittest.TestCase):
    def test_source_change_during_state_build_does_not_stamp_old_numbers_as_new(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / 'wiki').mkdir()
            article = root / 'wiki/product.md'
            initial = '# Product\n| 产品名称 | 成本(元) |\n|---|---|\n| Widget | 11 |\n'
            article.write_text(initial, encoding='utf-8')
            old = S.build(False, workspace_root=root)
            original = S.extract_from_article
            def changing(*args, **kwargs):
                article.write_text(initial.replace('11', '99'), encoding='utf-8')
                return original(*args, **kwargs)
            with patch.object(S, 'extract_from_article', side_effect=changing):
                with self.assertRaisesRegex(ValueError, '读取期间变化'):
                    S.build(False, workspace_root=root)
            self.assertEqual(S.load(root), old)
            self.assertFalse(S.verify(workspace_root=root, doc=old)['ok'])
            current = S.build(False, workspace_root=root)
            self.assertEqual(current['metrics'][0]['value'], 99)
            self.assertTrue(S.verify(workspace_root=root, doc=current)['ok'])
            self.assertFalse(S.verify(workspace_root=root, doc=old)['ok'])

    def test_database_signature_and_metrics_come_from_one_state_version(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            for sub in ('raw', 'wiki', 'state'):
                (root / sub).mkdir()
            source = root / 'state/metrics.json'
            source.write_text(json.dumps(dict(metric_doc(10), wiki_signature='old-version')), encoding='utf-8')
            original = DB.build_metrics
            def changing(*args, **kwargs):
                answer = original(*args, **kwargs)
                source.write_text(json.dumps(dict(metric_doc(99), wiki_signature='new-version')), encoding='utf-8')
                return answer
            with patch.object(DB, 'build_metrics', side_effect=changing):
                DB.build(root / 'private.db', False, root)
            with closing(sqlite3.connect(root / 'private.db')) as con, con:
                self.assertEqual(con.execute('SELECT value FROM mirror_metrics').fetchone()[0], 10)
                self.assertEqual(con.execute("SELECT value FROM meta WHERE key='wiki_signature'").fetchone()[0], 'old-version')

    def test_state_root_is_derived_from_wiki_and_concurrent_writes_are_isolated(self):
        with tempfile.TemporaryDirectory() as folder:
            roots = [Path(folder) / name for name in ('alice', 'bob')]
            for root, number in zip(roots, (11, 22)):
                (root / 'wiki').mkdir(parents=True)
                (root / 'wiki/product.md').write_text(
                    '# Product\n| 产品名称 | 成本(元) |\n|---|---|\n| Widget | ' + str(number) + ' |\n', encoding='utf-8')
            with ThreadPoolExecutor(2) as pool:
                docs = list(pool.map(lambda root: S.build(verbose=False, wiki_dir=root / 'wiki'), roots))
            self.assertEqual([doc['metrics'][0]['value'] for doc in docs], [11, 22])
            for root in roots:
                self.assertTrue(S.verify(wiki_dir=root / 'wiki')['ok'])
                self.assertTrue((root / 'state/metrics.json').exists())
            self.assertNotEqual(S.load(roots[0])['wiki_signature'], S.load(roots[1])['wiki_signature'])

    def test_database_rebuild_preserves_history_and_rolls_back_failure(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            for sub in ('raw', 'wiki', 'state'):
                (root / sub).mkdir()
            source = root / 'state/metrics.json'
            database = root / 'private.db'
            source.write_text(json.dumps(metric_doc(10)), encoding='utf-8')
            DB.build(database, False, root)
            with closing(sqlite3.connect(database)) as con, con:
                con.execute('INSERT INTO mirror_metric_changes(metric_key,old_value,new_value) VALUES(?,?,?)', ('fixed-id', 5, 10))
                con.execute('CREATE TABLE users(id INTEGER PRIMARY KEY, name TEXT)')
                con.execute('INSERT INTO users VALUES(1,?)', ('preserved',))
            source.write_text(json.dumps(metric_doc(12)), encoding='utf-8')
            self.assertEqual(DB.build(database, False, root)['metric_changes'], 1)
            self.assertEqual(DB.build(database, False, root)['metric_changes'], 0)
            with closing(sqlite3.connect(database)) as con, con:
                self.assertEqual(con.execute('SELECT old_value,new_value FROM mirror_metric_changes ORDER BY id').fetchall(), [(5, 10), (10, 12)])
                self.assertEqual(con.execute('SELECT name FROM users').fetchall(), [('preserved',)])
            broken = metric_doc(99)
            del broken['metrics'][0]['entity']
            source.write_text(json.dumps(broken), encoding='utf-8')
            with self.assertRaises(KeyError):
                DB.build(database, False, root)
            with closing(sqlite3.connect(database)) as con, con:
                self.assertEqual(con.execute('SELECT value FROM mirror_metrics').fetchall(), [(12,)])
                self.assertEqual(con.execute('SELECT count(*) FROM mirror_metric_changes').fetchone()[0], 2)


class LegacyFinalizeRegressions(unittest.TestCase):
    def test_missing_checker_is_not_clean_and_json_exit_status_is_nonzero(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(F, 'HERE', Path(folder)):
            result = F.finalize(only='check', quiet=True, workspace_root=Path(folder))
            self.assertFalse(result['check']['ok'])
            self.assertFalse(result['clean'])
            self.assertTrue(result['problems'])

    def test_nonzero_and_incomplete_subprocess_output_never_pass(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            for name in ('check_evidence.py', 'check_math.py'):
                (root / name).write_text('print("incomplete fixture")\n', encoding='utf-8')
            with patch.object(F, 'HERE', root):
                self.assertFalse(F.step_check(True, root)['ok'])
                self.assertFalse(F.step_math(True, root)['ok'])
                (root / 'check_math.py').write_text('import sys\nprint("通过 3 · 可疑 0")\nsys.exit(2)\n', encoding='utf-8')
                self.assertFalse(F.step_math(True, root)['ok'])
                self.assertFalse(F.finalize(quiet=True, skip=list(F.STEPS), workspace_root=root)['clean'])


class LegacyEvaluationRegressions(unittest.TestCase):
    def test_absent_element_rows_never_pass_structure(self):
        document = '\n'.join(ER.REQUIRED_CHAPTERS + ER.REQUIRED_HEADERS)
        self.assertFalse(ER.check_structure(document)['pass'])
        self.assertIs(ER.check_structure(document)['element_row_ok'], False)

    def test_no_comparisons_missing_rate_and_nonfinite_rate_never_pass(self):
        import benchmark_engine as BE
        dimensions = [label for label, _ in ER._UNIT_COLS]
        standard = {label: (0.0, 0.0) for label in dimensions}
        for rows in ([], [{'成本要素': label, '一厂': 10, '二厂': 10, '差异金额': 0,
                           '差异率': None, '方向': '持平'} for label in dimensions],
                     [{'成本要素': label, '一厂': 10, '二厂': 10, '差异金额': 0,
                       '差异率': float('nan'), '方向': '持平'} for label in dimensions]):
            with patch.object(BE, 'run', return_value={'steps': [{'rows': rows}, {}, {'text': '一厂单位成本'}]}), \
                 patch.object(ER, '_std_peer_diff', return_value=standard):
                self.assertFalse(ER.check_benchmark('fixture', '2026-05')['rate_err_ok'])
        self.assertEqual(ER._pct_err(0, 0), 0)
        self.assertEqual(ER._pct_err(1, 0), float('inf'))
        self.assertIsNone(ER._pct_err(float('nan'), 0))

    def test_all_failed_benchmarks_cannot_render_success_checkmark(self):
        result = {'scenarios': [], 'rpa': {'rate': None, 'error': 'not run'},
                  'benchmark': [{'product': product, 'month': month, 'ok': False, 'error': 'fixture'}
                                for product, month in ER.SCENARIOS],
                  'generated_at': '2026-11-12', 'use_llm': False}
        line = next(line for line in ER.render(result).splitlines() if line.startswith('| ④'))
        self.assertIn('❌', line)
        self.assertNotIn('✅', line)


class LegacyPackagingAndMemoryRegressions(unittest.TestCase):
    def package_roots(self, folder):
        root = Path(folder)
        code, data = root / 'code', root / 'external-data'
        (code / 'scripts').mkdir(parents=True)
        (code / 'server').mkdir()
        (code / 'scripts/app.py').write_text('print("fixture")\n', encoding='utf-8')
        (code / 'scripts/.env').write_text('secret fixture must not copy', encoding='utf-8')
        (data / 'raw').mkdir(parents=True)
        (data / 'wiki').mkdir()
        (data / 'raw/input.csv').write_text('value\n42\n', encoding='utf-8')
        (data / 'wiki/entry.md').write_text('# Source\n', encoding='utf-8')
        return root, code, data

    def test_packaging_external_data_manifest_and_preserves_existing_output(self):
        with tempfile.TemporaryDirectory() as folder:
            root, code, data = self.package_roots(folder)
            output = root / 'delivery'
            manifest = PK.assemble(code, data, output)
            self.assertFalse(manifest['encrypted'])
            self.assertTrue(manifest['complete'])
            self.assertEqual((output / 'raw/input.csv').read_bytes(), (data / 'raw/input.csv').read_bytes())
            self.assertFalse((output / 'scripts/.env').exists())
            self.assertEqual({item['path'] for item in manifest['files']}, {'scripts/app.py', 'raw/input.csv', 'wiki/entry.md'})
            with self.assertRaises(FileExistsError):
                PK.assemble(code, data, output)
            self.assertTrue((output / 'MANIFEST.json').exists())
            with self.assertRaises(ValueError):
                PK.assemble(code, data, code / 'context')

    def test_packaging_requires_actual_business_files(self):
        with tempfile.TemporaryDirectory() as folder:
            root, code, data = self.package_roots(folder)
            (data / 'raw/input.csv').unlink()
            with self.assertRaises(ValueError):
                PK.assemble(code, data, root / 'delivery')
            self.assertFalse((root / 'delivery').exists())

    def test_memory_cli_db_argument_is_a_path_and_does_not_create_user_name(self):
        with tempfile.TemporaryDirectory() as folder:
            database = Path(folder) / 'memory.db'
            process = subprocess.run([sys.executable, str(ROOT / 'scripts/agent_memory.py'),
                                      '--db', str(database), '--stats'], capture_output=True, text=True)
            self.assertEqual(process.returncode, 0, process.stderr)
            self.assertEqual(json.loads(process.stdout)['messages'], 0)
            self.assertEqual([path.name for path in Path(folder).iterdir()], ['memory.db'])

    def test_summary_failure_preserves_previous_constraints_and_does_not_promote_authority(self):
        class Failing:
            def invoke(self, *args): raise RuntimeError('fixture failure')
        previous = '必须只用数据集甲；保留旧版。 [wiki/entry.md:L12:42]'
        messages = [{'id': 1, 'role': 'user', 'content': '禁止覆盖原件；目录 D:\\project'},
                    {'id': 2, 'role': 'tool', 'content': 'fact [cost.csv:amount:row2] = 42.5'}]
        summary = AC.make_summary(Failing(), messages, previous)
        self.assertIn(previous, summary)
        self.assertIn('禁止覆盖原件', summary)
        self.assertIn('42.5', summary)
        self.assertIn('未经事实核验', summary)
        context = AC.build_context(messages, summary, 2)
        self.assertEqual(context[0]['role'], 'assistant')
        self.assertNotIn('可直接引用', context[0]['content'])


if __name__ == '__main__':
    unittest.main()
