import json,tempfile,unittest
from pathlib import Path
from costing_outputs import _capture_ref

class RequestCache(unittest.TestCase):
    def test_frozen_bytes_but_fresh_requests_and_workspace(self):
        with tempfile.TemporaryDirectory() as t:
            root=Path(t); dataset="csv-123abcd123abcd12"
            def prepare(ws,amount,stage):
                data=ws/"costing_datasets"/dataset;data.mkdir(parents=True,exist_ok=True)
                (data/"data.csv").write_text(f"amount\n{amount}\n",encoding="utf8")
                (data/"manifest.json").write_text(json.dumps({"schema":{"allocation_rules":[{"stage":stage}]}}),encoding="utf8")
            w1,w2=root/"owner1",root/"owner2"
            prepare(w1,11,1);prepare(w2,33,3)
            raw=dict(dataset_id=dataset,source_uri="upload://data.csv",field="amount",selector="row=2",kind="raw")
            rule=dict(dataset_id=dataset,source_uri="upload://schema",field="rule",selector="allocation_rules[0]",kind="configuration")
            c={}
            before=_capture_ref(raw,w1,root,dataset,c);br=_capture_ref(rule,w1,root,dataset,c)
            prepare(w1,22,2)
            self.assertEqual(_capture_ref(raw,w1,root,dataset,c),before)
            self.assertEqual(_capture_ref(rule,w1,root,dataset,c),br)
            self.assertNotEqual(_capture_ref(raw,w1,root,dataset)["sha256"],before["sha256"])
            self.assertNotEqual(_capture_ref(rule,w1,root,dataset)["sha256"],br["sha256"])
            self.assertEqual(_capture_ref(raw,w2,root,dataset,c)["rows"][0]["value"],"33")
            with self.assertRaises(ValueError):_capture_ref(raw,w2,root,"csv-6555555555555555",c)

    def test_symlink_escape_still_rejected_before_capture(self):
        with tempfile.TemporaryDirectory() as t:
            root=Path(t);w=root/"owner"; d="csv-123abcd123abcd12"
            place=w/"costing_datasets"/d;place.mkdir(parents=True)
            f=root/"outside.csv";f.write_text("amount\n123\n")
            try:(place/"data.csv").symlink_to(f)
            except OSError:self.skipTest("symlink unavailable")
            raw=dict(dataset_id=d,source_uri="upload://data.csv",field="amount",selector="row=2",kind="raw")
            with self.assertRaises(ValueError):_capture_ref(raw,w,root,d,{})
