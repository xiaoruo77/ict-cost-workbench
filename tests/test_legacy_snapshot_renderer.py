"""Keep sealed legacy text exact; new reports only permit the safer renderer."""
import copy
import hashlib
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from costing_outputs import (create_snapshot, export_snapshot, load_snapshot,
                             validate_snapshot_integrity)

FIXTURE=Path(__file__).resolve().parents[1]/"fixtures"/"recognition"/"legacy_snapshot_v1.json"
OLD="以下桥接属于完整的声明账簿及当前期间，不因本页筛选某一成本对象而改变。"
NEW="以下桥接仅对应本次申报的账簿及期间范围，不因本页筛选某一成本对象而改变。"

class OldReports(unittest.TestCase):
    def setUp(self):
        self.raw=FIXTURE.read_bytes()
        self.v=json.loads(self.raw)
        self.assertEqual(self.v["revision"],"97c4d4067f4449057c550ad0e3445fdb7cca3b29cfc0f8652b8ea3b3fb400666")
        self.assertIn(OLD,self.v["report"]["markdown"])
        self.assertEqual(self.v["context"]["recognition_render_version"],1)
        folder=TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root=Path(folder.name)
        d=self.root/"costing_snapshots"
        d.mkdir()
        self.path=d/(self.v["analysis_id"]+".json")
        self.path.write_bytes(self.raw)

    def test_real_legacy_writer_load_exact_and_unchanged_exports(self):
        loaded=load_snapshot(self.v["analysis_id"],self.root)
        self.assertEqual(loaded,self.v)
        self.assertEqual(self.path.read_bytes(),self.raw)
        markdown=export_snapshot(loaded,"md",self.root/"exports").read_text()
        original=export_snapshot(loaded,"json",self.root/"exports").read_bytes()
        self.assertEqual(markdown,self.v["report"]["markdown"])
        self.assertEqual(hashlib.sha256(original).digest(),hashlib.sha256(self.raw).digest())

    def test_one_character_tamper_and_wrong_chart_or_revision_rejected(self):
        bads=[]
        for old,new in (("属于完整","属于全部"),("12.34","12.35")):
            v=copy.deepcopy(self.v)
            v["report"]["markdown"]=v["report"]["markdown"].replace(old,new,1)
            self.assertNotEqual(v["report"]["markdown"],self.v["report"]["markdown"])
            bads.append(v)
        c=copy.deepcopy(self.v); c["charts"]["currency"]="USD"; bads.append(c)
        e=copy.deepcopy(self.v); e["evidence"][0]["field"]="bogus"; bads.append(e)
        for v in bads:
            with self.assertRaisesRegex(ValueError,"不一致|校验失败"):
                validate_snapshot_integrity(v,self.v["analysis_id"])

    def test_new_writer_uses_new_version_and_forbids_old_sentence(self):
        c={k:self.v["context"][k] for k in ("dataset_id","period","scenario","owner_external_id","dataset_label")}
        current=create_snapshot(self.v["result"],self.root,self.root,c)
        result=load_snapshot(current["analysis_id"],self.root)
        self.assertEqual(result["context"]["recognition_render_version"],2)
        self.assertIn(NEW,result["report"]["markdown"])
        self.assertNotIn(OLD,result["report"]["markdown"])
        bad=copy.deepcopy(result); bad["report"]["markdown"]=bad["report"]["markdown"].replace(NEW,OLD)
        with self.assertRaisesRegex(ValueError,"不一致"):
            validate_snapshot_integrity(bad,result["analysis_id"])
        self.assertEqual(self.path.read_bytes(),self.raw)

if __name__=="__main__": unittest.main()
