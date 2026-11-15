import unittest
from decimal import Decimal as D
from dataclasses import replace
from copy import deepcopy
from test_adapter_recognition import fixture,convert
from cost_core.csv_adapter import CsvCostingAdapter
from cost_core.costing import CostingEngine
from cost_core.recognition_authority import VerifiedSourceBookComparison,Scope
from cost_core.source_book_manifest import build_manifest

def setup():
    value=fixture();ctx=value["context"]
    scope=Scope(workspace="authenticated-unit-test-A",book=ctx["book"],currency=ctx["currency"],period=ctx["period"],
                scope="recognized synthetic input financial records",version="verified-reconcile-v1",scale=2)
    rows=[{**{k:x[k] for k in ("id","book","period","currency","kind","amount")},
           "tags":{k:x[k] for k in ("status","economic_key","origin_period","bridge_group") if k in x}}
          for x in value["sources"] if x["amount"] is not None]
    return value,rows,scope

class AuthorityIntegration(unittest.TestCase):
    def compile(self,doc,proof=None):
        t,s=convert(doc)
        return CostingEngine().analyze(CsvCostingAdapter().from_text(t,s,trusted_source_book=proof))

    def test_upload_only_never_promoted(self):
        v,_,_=setup();r=self.compile(v)
        self.assertFalse(r["recognition"]["can_claim_complete"])
        self.assertEqual(r["recognition"]["source_book_check"]["status"],"not_provided")
        for key in ("trusted_authority_id","source_book_manifest","trusted_source_book"):
            t,s=convert(v);s[key]="spoofed"
            with self.assertRaises(ValueError):CsvCostingAdapter().from_text(t,s)
        with self.assertRaises(ValueError):self.compile(v,{"trusted":"spoofed"})

    def test_authenticated_context_matches_exactly_without_completeness_claim(self):
        v,rows,scope=setup(); manifest=build_manifest(rows=rows,scope=scope,authority_id="out_of_band_trusted_A")
        proof=VerifiedSourceBookComparison(manifest,scope,"out_of_band_trusted_A")
        r=self.compile(v,proof)
        self.assertEqual(r["recognition"]["source_book_check"]["status"],"matched")
        self.assertEqual(r["recognition"]["source_book_check"]["upstream_version"],"verified-reconcile-v1")
        self.assertTrue(r["ledger_reconciled"])
        self.assertFalse(r["recognition"]["can_claim_complete"])
        stale=replace(proof,expected=replace(scope,workspace="unrelated-owner"))
        self.assertEqual(self.compile(v,stale)["recognition"]["source_book_check"]["status"],"mismatch")
        self.assertEqual(self.compile(v,replace(proof,independently_verified_authority="untrusted"))["recognition"]["source_book_check"]["status"],"unverified")

    def test_amount_mismatch_survives_despite_balanced_ledger(self):
        v,rows,scope=setup();v2=deepcopy(v)
        for row in v2["sources"]:
            if row["id"]=="materials_used":row["amount"]="1099"
        proof=VerifiedSourceBookComparison(build_manifest(rows=rows,scope=scope,authority_id="verified"),scope,"verified")
        r=self.compile(v2,proof)
        self.assertTrue(r["ledger_reconciled"])
        self.assertEqual(D(r["recognition"]["outputs"]["production_cost"]),1999)
        self.assertEqual(r["recognition"]["source_book_check"]["status"],"mismatch")
        self.assertEqual(r["recognition"]["source_book_check"]["changed_ids"],["materials_used"])
        self.assertFalse(r["recognition"]["can_claim_complete"])
