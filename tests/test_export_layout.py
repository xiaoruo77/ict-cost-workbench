import sys,unittest
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
import export_doc as E

class ExportLayoutTests(unittest.TestCase):
    def test_long_advice_table_fits_text_width(self):
        rows=[['序号','建议事项','责任部门','优先级','预期效果','建议完成时间'],
              ['1','核对采购合同与用量差异'*20,'采购部','高','形成可复算核对结论'*14,'2026-11-22']]
        widths=E._col_widths(rows,6)
        self.assertAlmostEqual(sum(widths),14.6)
        self.assertGreaterEqual(widths[-1],1.95)
        self.assertTrue(all(w>0 for w in widths))

    def test_percentage_column_has_room(self):
        rows=[['原材料','年初价','本月价','涨幅','市场趋势','对材料成本影响'],
              ['空心胶囊（元/万粒）','85.0','87.0','+14.29%','总体趋势不可套本月'*5,'传导需核查'*7]]
        widths=E._col_widths(rows,6)
        self.assertAlmostEqual(sum(widths),14.6)
        self.assertGreaterEqual(widths[3],1.65)

class ExportLongEvidenceTests(unittest.TestCase):
    def test_word_export_keeps_identifiers_exact_and_chart_caption_link_after_generic_formatting(self):
        from copy import deepcopy
        import tempfile
        from unittest.mock import patch
        from docx import Document
        from docx.oxml.ns import qn
        from PIL import Image
        import costing_outputs as C
        digest = '08b6be09c9297d8df82b346cfa83507f34a278d7a000187960768d9bb4931c26'
        encoded = 'fact%3A' + '%E9%93%B6%E9%BB%84%E5%8F%A3%E6%9C%8D%E6%B6%B2' * 8
        markdown = '# 报告\n\n## 三、成本对象与成本结构\n\n正文\n\n## 附录 核算事实与来源复核\n\n### E0001\n来源：'+encoded+'\nSHA-256：'+digest+'\n\n|列|原始值|\n|---|---|\n|id|'+encoded+'|\n'
        snapshot = {'analysis_id':'f'*32,'context':{'period':'2026-05'},'report':{'markdown':markdown}}
        original = deepcopy(snapshot)
        def charts(_snapshot, directory):
            image = directory/'generated.png';Image.new('RGB',(400,160),'#0f766e').save(image)
            return [{'path':image,'image':image,'after':'## 三、成本对象与成本结构','caption':'完整图注不可拆离图片'}]
        with tempfile.TemporaryDirectory() as directory, patch.object(C,'_charts',side_effect=charts):
            target = C.export_snapshot(snapshot,'docx',directory)
            document = Document(target)
            picture = next(par for par in document.paragraphs if par._p.xpath('.//w:drawing'))
            caption = next(par for par in document.paragraphs if par.text=='完整图注不可拆离图片')
            self.assertTrue(picture.paragraph_format.keep_with_next)
            self.assertTrue(caption.paragraph_format.keep_together)
            self.assertEqual(picture._p.getnext(),caption._p)
            self.assertEqual(picture.paragraph_format.first_line_indent,0)
            self.assertEqual(caption.paragraph_format.first_line_indent,0)
            hash_par = next(par for par in document.paragraphs if digest in par.text)
            self.assertEqual(hash_par.text,'SHA-256：'+digest)
            self.assertEqual(hash_par._p.pPr.find(qn('w:wordWrap')).get(qn('w:val')),'off')
            self.assertEqual(hash_par.style.name,'Evidence Text')
            self.assertEqual(document.tables[0].rows[1].cells[1].text,encoded)
            self.assertNotIn('\u200b',hash_par.text)
            self.assertEqual(snapshot,original)

    def test_pdf_long_text_transform_preserves_full_values_and_only_inserts_constant_breaks(self):
        import shutil,json,subprocess
        if not shutil.which('pandoc'):
            self.skipTest('Pandoc system dependency unavailable')
        digest = '08b6be09c9297d8df82b346cfa83507f34a278d7a000187960768d9bb4931c26'
        encoded = 'fact%3A' + '%E9%93%B6%E9%BB%84%E5%8F%A3%E6%9C%8D%E6%B6%B2' * 8
        ids = ', '.join('e-'+str(i).zfill(20) for i in range(12))
        dangerous = r'\input{/private/very_long_secret_path_that_must_never_be_read}'
        markdown = '# Evidence\n\nSHA-256：'+digest+'\n\n'+ids+'\n\n|fact_id|literal|\n|---|---|\n|`'+encoded+'`|`'+dangerous+'`|\n'
        clean = E._sanitize_pandoc_document(E._pandoc_document('pandoc',markdown,E.SAFE_MARKDOWN_READER))
        before = E._plain_inlines(clean['blocks'])
        transformed = E._break_long_ascii(clean)
        self.assertEqual(E._plain_inlines(transformed['blocks']),before)
        self.assertIn(digest,before)
        self.assertIn(encoded,before)
        def nodes(obj):
            if isinstance(obj,dict):
                if obj.get('t'):
                    yield obj
                for value in obj.values():yield from nodes(value)
            elif isinstance(obj,list):
                for value in obj:yield from nodes(value)
        breaks = [n for n in nodes(transformed) if n['t']=='RawInline']
        self.assertGreater(len(breaks),50)
        self.assertTrue(all(n['c']==['latex',r'\allowbreak{}'] for n in breaks))
        latex=subprocess.run(['pandoc','--from=json','--to=latex'],input=json.dumps(transformed),text=True,capture_output=True,check=True).stdout
        self.assertIn(r'\allowbreak{}',latex)
        self.assertNotIn(dangerous,latex)
        self.assertNotIn(r'\input{',latex)
        self.assertNotIn(encoded,latex)


if __name__=='__main__':unittest.main()
