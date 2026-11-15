"""Raw v3 preserves evidence and rejects ambiguous aliases before calculating."""
from copy import deepcopy
from decimal import Decimal as D
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from urllib.parse import parse_qs
import unittest

from costing_outputs import create_snapshot, load_snapshot, resolve_evidence
from test_composite_adapter import evaluate, setup, text
from test_composite_meter_v3 import two_accounts


class CompositeIdentityAliases(unittest.TestCase):
    def test_aliases_rejected_for_both_ids_in_same_bill_and_across_branches(self):
        for alias in ("POWER-A", "ＰＯＷＥＲ－Ａ"):
            for field in ("id","posting_identity","both"):
                s, r=two_accounts()
                row=deepcopy(next(x for x in r if x["id"]=="power-a"))
                row["id"]=alias if field in ("id","both") else "power-distinct"
                row["posting_identity"]=alias if field in ("posting_identity","both") else "posting-distinct"
                failure="original source ID" if field=="id" else "posting identity"
                before=evaluate(s,r)[1]
                with self.subTest(alias=alias,field=field), self.assertRaisesRegex(ValueError,"ambiguous aliased "+failure) as got:
                    evaluate(s,r+[row])
                self.assertNotIn(alias,str(got.exception))
                self.assertEqual(before,evaluate(s,r)[1])
            for field in ("id","posting_identity"):
                s, r=two_accounts()
                water=next(x for x in r if x["id"]=="water-a")
                water[field]=alias
                with self.subTest(alias=alias,cross_branch_field=field), self.assertRaisesRegex(ValueError,"ambiguous aliased"):
                    evaluate(s,r)

    def test_currency_and_role_separation_allow_genuinely_distinct_ids_and_saved_refs(self):
        s,rows=two_accounts()
        p=next(r for r in rows if r["id"]=="power-a")
        w=next(r for r in rows if r["id"]=="water-a")
        # A posting identity may equal a native source ID, even from elsewhere;
        # each column denotes an independent identity role.
        p["id"]="售电-Pöwer-A"
        w["posting_identity"]="售电-PÖWER-A"
        req,result=evaluate(s,rows)
        self.assertEqual(D(result["totals"]["total_cost"]),D("2297.10"))
        other=deepcopy(s);other["dataset_id"]="csv-3333333333333333"
        req2,other_result=evaluate(other,rows)
        self.assertEqual(result["totals"],other_result["totals"])
        self.assertNotEqual(req.entries[0].source_refs[0].dataset_id,req2.entries[0].source_refs[0].dataset_id)
        with TemporaryDirectory() as t:
            root=Path(t);ds=root/"costing_datasets"/s["dataset_id"];ds.mkdir(parents=True)
            (ds/"data.csv").write_text(text(rows),encoding="utf-8")
            (ds/"manifest.json").write_text(json.dumps({"schema":s}),encoding="utf-8")
            x=create_snapshot(result,root,root,dict(dataset_id=s["dataset_id"],period="2026-11",
                 scenario="actual",dataset_label="synthetic identity",owner_external_id="identity-gate"))
            y=load_snapshot(x["analysis_id"],root)
            self.assertEqual(x["revision"],y["revision"])
            e=next(r for r in y["evidence"] if parse_qs(r["selector"]).get("id")==["售电-Pöwer-A"])
            self.assertEqual(e["status"],"captured")
            self.assertEqual(e["rows"][0]["values"]["id"],"售电-Pöwer-A")
            self.assertTrue(resolve_evidence(y,e["evidence_id"],root,root)["current_source_matches"])

    def test_version_compatibility_and_v3_unicode_boundary(self):
        for old in (1,2):
            s,rows=setup()
            s["raw_composite"]["version"]=old
            bill=next(row for row in rows if row["input_name"]=="tariff")
            ob={**bill,"id":"UTILITY-1","posting_identity":"UTILITY-1"}
            with self.subTest(old=old):
                self.assertGreater(D(evaluate(s,rows+[ob])[1]["totals"]["total_cost"]),
                                   D(evaluate(s,rows)[1]["totals"]["total_cost"]))
        for hidden in ("pow\u200ber-a","pow\0er-a"):
            for field in ("id","posting_identity"):
                s,r=two_accounts()
                next(x for x in r if x["id"]=="power-a")[field]=hidden
                with self.subTest(field=field),self.assertRaisesRegex(ValueError,"invalid v3 source identity"):
                    evaluate(s,r)
        s,r=two_accounts()
        next(x for x in r if x["id"]=="power-a")["posting_identity"]="ﷺ"*127
        with self.assertRaisesRegex(ValueError,"invalid canonical v3 source identity"):
            evaluate(s,r)
