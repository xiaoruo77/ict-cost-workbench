from copy import deepcopy
from hashlib import sha256
import json
import os
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest

from audit_isolated_cost_snapshots import audit
from test_legacy_snapshot_renderer import FIXTURE


def encode(v):
    return json.dumps(v,ensure_ascii=False,sort_keys=True,separators=(",",":")).encode("utf-8")


class OfflineGate(unittest.TestCase):
    def setUp(self):
        tmp=TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root=Path(tmp.name)
        self.clone=self.root/"clone"
        self.snapshots=self.clone/"costing_snapshots"
        self.snapshots.mkdir(parents=True)
        self.m=self.clone/"snapshot-audit-manifest.json"
        self.v=json.loads(FIXTURE.read_bytes())
        self.old_id=self.v["analysis_id"]
        self.f=self.snapshots/(self.old_id+".json")
        self.f.write_bytes(FIXTURE.read_bytes())

    def pin(self):
        records=[]
        for p in sorted(self.snapshots.glob("*.json")):
            raw=p.read_bytes()
            records.append(dict(id=p.stem,sha256=sha256(raw).hexdigest(),bytes=len(raw)))
        self.m.write_bytes(encode(dict(version=1,purpose="isolated_snapshot_clone",snapshots=records)))
        return sha256(self.m.read_bytes()).hexdigest()

    def test_audit_seven_distinct_historical_ids_exact_read_only(self):
        # The historical fixture was produced by an actual old writer. Give
        # it six distinct synthetic IDs to stress whole-population scanning.
        for index in range(6):
            val=deepcopy(self.v)
            replacement=f"{index:032x}"
            val["analysis_id"]=val["report"]["analysis_id"]=val["charts"]["analysis_id"]=replacement
            val["report"]["markdown"]=val["report"]["markdown"].replace(self.old_id,replacement)
            (self.snapshots/(replacement+".json")).write_bytes(encode(val))
        pin=self.pin()
        all_bytes={f:f.read_bytes() for f in self.clone.rglob("*") if f.is_file()}
        out=audit(str(self.clone),pin)
        self.assertEqual((out["ok"],out["expected"],out["validated"]),(True,7,7))
        self.assertEqual({f:f.read_bytes() for f in all_bytes},all_bytes)

    def test_digest_matches_do_not_hide_invalid_or_ambiguous_json(self):
        wrong=deepcopy(self.v)
        wrong["report"]["markdown"]=wrong["report"]["markdown"].replace("12.34","12.35")
        for raw in (encode(wrong),self.f.read_bytes()[:-1]+b',"schema_version":"duplicate"}',b'{}'):
            self.f.write_bytes(raw)
            out=audit(str(self.clone),self.pin())
            self.assertEqual((out["ok"],out["expected"],out["validated"],out["affected_count"]),
                             (False,1,0,1))
            self.assertNotIn(self.old_id,json.dumps(out))
            self.assertNotIn("synthetic",json.dumps(out))

    def test_unknown_symlinked_hardlinked_and_pending_files_are_refused(self):
        pin=self.pin()
        for name in ("future.pending","ignore.txt","f"*32+".json"):
            other=self.snapshots/name
            other.write_text("DO NOT DISCLOSE")
            out=audit(str(self.clone),pin)
            self.assertFalse(out["ok"])
            self.assertNotIn(name,json.dumps(out))
            other.unlink()
        stranger=self.root/"external"
        self.f.rename(stranger)
        self.f.symlink_to(stranger)
        self.assertFalse(audit(str(self.clone),pin)["ok"])
        self.f.unlink()
        os.link(stranger,self.f)
        self.assertFalse(audit(str(self.clone),pin)["ok"])

    def test_wrong_pin_duplicate_manifest_missing_oversized_and_parent_alias(self):
        p=self.pin()
        self.assertFalse(audit(str(self.clone),"a"*64)["ok"])
        alias=self.root/"alias";alias.symlink_to(self.clone,target_is_directory=True)
        self.assertFalse(audit(str(alias),p)["ok"])
        self.assertFalse(audit(str(self.clone)+"/../clone",p)["ok"])
        body=json.loads(self.m.read_bytes())
        body["snapshots"].append(body["snapshots"][0])
        self.m.write_bytes(encode(body))
        self.assertFalse(audit(str(self.clone),sha256(self.m.read_bytes()).hexdigest())["ok"])
        p=self.pin()
        with self.f.open("ab") as extra:
            extra.write(b"0")
        self.assertFalse(audit(str(self.clone),p)["ok"])
        self.f.unlink()
        self.assertEqual(audit(str(self.clone),p)["affected_count"],1)

    def test_bytes_above_16_mib_pass_and_above_64_mib_are_refused(self):
        # Read from a portable larger JSON wire file with unchanged sealed
        # result; the large museum workload is verified separately end-to-end.
        minimum=16*1024**2+1
        with self.f.open("ab") as stream:
            stream.write(b" "*(minimum-self.f.stat().st_size))
        self.assertEqual(self.f.stat().st_size,minimum)
        accepted=audit(str(self.clone),self.pin())
        self.assertEqual((accepted["ok"],accepted["validated"]),(True,1))
        body=json.loads(self.m.read_bytes())
        body["snapshots"][0]["bytes"]=64*1024**2+1
        self.m.write_bytes(encode(body))
        refused=audit(str(self.clone),sha256(self.m.read_bytes()).hexdigest())
        self.assertFalse(refused["ok"])
        self.assertEqual(refused["code"],"invalid_manifest_record")

    def test_cli_returns_nonzero_and_no_paths_or_report_content(self):
        p=self.pin()
        f=Path(__file__).resolve().parents[1]/"scripts"/"audit_isolated_cost_snapshots.py"
        cmd=[sys.executable,str(f),"--clone-root",str(self.clone),"--manifest-sha256",p]
        ok=subprocess.run(cmd,capture_output=True,text=True,check=False)
        self.assertEqual(ok.returncode,0)
        self.f.write_text("private customer query")
        bad=subprocess.run(cmd,capture_output=True,text=True,check=False)
        self.assertNotEqual(bad.returncode,0)
        self.assertEqual(bad.stderr,"")
        self.assertFalse(json.loads(bad.stdout)["ok"])
        self.assertNotIn(str(self.clone),bad.stdout)
        self.assertNotIn("private customer query",bad.stdout)

if __name__=="__main__":unittest.main()
