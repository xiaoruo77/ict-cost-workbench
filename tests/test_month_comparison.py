"""Monthly comparison using two immutable captures from the actual adapter."""
import calendar
import copy
from dataclasses import replace
from datetime import date, timedelta
from hashlib import sha256
import json
from pathlib import Path
import tempfile
import unittest

from costing_monthly import compare_canonical_months, MonthlyComparisonError, MonthlyRevision, amount_atoms, money, variance
from costing_outputs import create_snapshot, load_snapshot
from test_composite_adapter import evaluate, text
from test_object_rollups import example, member

_OWNER = "qa-authenticated"
_BOOK = "RETAIL_DEMO"
_POLICY = sha256(b"server-pinned accounting policy v1").hexdigest()


def receipt(s, policy=_POLICY):
    return MonthlyRevision(s["analysis_id"], s["revision"], policy)


def fixture(month, dataset_id, alternate=False, book=_BOOK):
    # Each period is independently seeded. These data are not a stock roll-forward.
    s, raw = example()
    y, m = map(int, month.split("-"))
    previous_close = (date(y,m,1)-timedelta(days=1)).isoformat() + "T23:59:59Z"
    month_end = f"{month}-{calendar.monthrange(y,m)[1]:02d}T23:59:59Z"

    def rewrite(v):
        if isinstance(v, list):
            return [rewrite(x) for x in v]
        if isinstance(v, dict):
            return {k:rewrite(x) for k,x in v.items()}
        if isinstance(v,str):
            if v == "2026-10-31T23:59:59Z": return previous_close
            if v == "2026-11-30T23:59:59Z": return month_end
            if v == _BOOK: return book
            return month+v[7:] if v.startswith("2026-11") else v
        return v

    s, raw = rewrite(s), rewrite(raw)
    s["dataset_id"] = dataset_id

    def g(ident, *members):
        return {"id":ident, "label":ident, "members":list(members)}

    stable = g("stable", member("period","A"), member("power","ENERGY_E"))
    if alternate:
        stable["members"].reverse()
        stable["label"] = "updated display label"
        next(r for r in raw if r["id"] == "rent")["amount"] = "160.00"
    others = (
        [g("shape", member("stock","SKU_1")), g("new", member("water","ENERGY_W"))]
        if alternate else
        [g("shape", member("water","ENERGY_W")), g("old", member("stock","SKU_1"))]
    )
    s["raw_composite"]["object_rollups"] = [stable,*others]
    return s,raw


def snapshot(root, month, id_char, alternate=False, owner=_OWNER, book=_BOOK, has_rollup=True, adjust=None):
    s,r = fixture(month, "csv-" + id_char*16, alternate, book)
    if not has_rollup:
        del s["raw_composite"]["object_rollups"]
    if adjust is not None:
        adjust(s,r)
    result = evaluate(s,r)[1]
    source = root/"costing_datasets"/s["dataset_id"]
    source.mkdir(parents=True)
    (source/"data.csv").write_text(text(r), encoding="utf-8")
    (source/"manifest.json").write_text(json.dumps({"schema":s}), encoding="utf-8")
    created = create_snapshot(result,root,root,dict(dataset_id=s["dataset_id"],period=month,
                scenario="actual",owner_external_id=owner))
    return load_snapshot(created["analysis_id"],root)


class CanonicalMonthly(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.prior = snapshot(self.root,"2026-12","a")
        self.current = snapshot(self.root,"2027-01","b",True)

    def compare(self, p=None, c=None, **settings):
        p, c = p or self.prior, c or self.current
        args = dict(expected_workspace_owner=_OWNER, expected_declared_book=_BOOK,
                    prior_pin=receipt(p), current_pin=receipt(c))
        args.update(settings)
        return compare_canonical_months(p,c,**args)

    def test_actual_months_reconciled_unmatched_and_pinned_no_changes(self):
        old = copy.deepcopy((self.prior,self.current))
        files = {p:p.read_bytes() for p in self.root.rglob("*") if p.is_file()}
        out = self.compare()
        self.assertEqual(old,(self.prior,self.current))
        self.assertEqual(files,{p:p.read_bytes() for p in self.root.rglob("*") if p.is_file()})
        self.assertEqual((out["prior"]["comparable_actual"],out["current"]["comparable_actual"]),
                         ("1231.10","1261.10"))
        self.assertEqual((out["comparable_variance"],out["comparable_variance_pct"]),("30.00","2.44"))
        self.assertEqual((out["prior"]["unmatched_actual"],out["current"]["unmatched_actual"]),
                         ("1036.00","1036.00"))
        self.assertEqual((out["prior"]["ungrouped_actual"],out["current"]["ungrouped_actual"]),
                         ("30.00","40.00"))
        self.assertEqual((out["prior"]["ledger_actual"],out["current"]["ledger_actual"]),
                         ("2297.10","2337.10"))
        groups = {row["group_id"]:row for row in out["groups"]}
        self.assertEqual({k:v["status"] for k,v in groups.items()},
                         {"new":"added","old":"removed","shape":"membership_changed","stable":"comparable"})
        self.assertIsNone(groups["new"]["prior"])
        self.assertIsNone(groups["old"]["current"])
        self.assertIsNone(groups["shape"]["variance"])
        self.assertNotEqual(groups["stable"]["prior"]["membership_sha256"],groups["stable"]["current"]["membership_sha256"])
        self.assertFalse(out["can_claim_complete"])
        self.assertNotIn(_OWNER,json.dumps(out))
        for side, source in (("prior",self.prior),("current",self.current)):
            self.assertEqual(out[side]["revision"],source["revision"])
            self.assertEqual(out[side]["analysis_id"],source["analysis_id"])
            bound={i["evidence_id"]:i for i in source["evidence"]}
            stable_refs=groups["stable"][side]["evidence_ids"]
            self.assertTrue(any(bound[i]["status"]=="captured_mapping" for i in stable_refs))
            self.assertTrue(any(bound[i]["status"]=="captured" for i in stable_refs))

    def test_wrong_revision_tamper_cross_owner_indistinguishable(self):
        wrong=replace(receipt(self.current),revision="a"*64)
        foreign=snapshot(self.root,"2027-01","c",True,owner="different-workspace")
        tampered=copy.deepcopy(self.current)
        tampered["result"]["recognition"]["object_rollups"]["groups"][0]["total_cost"]="0.01"
        for kwargs in (dict(current_pin=wrong),dict(c=foreign),dict(c=tampered)):
            with self.subTest(kwargs=kwargs.keys()), self.assertRaisesRegex(MonthlyComparisonError,"^snapshot_unavailable$"):
                self.compare(**kwargs)
        with self.assertRaisesRegex(MonthlyComparisonError,"^snapshot_unavailable$"):
            self.compare(prior_pin=replace(receipt(self.prior),analysis_id="4"*32))

    def test_scope_policy_and_missing_opt_in_rejected(self):
        with self.assertRaisesRegex(MonthlyComparisonError,"^incomparable_accounting_policy$"):
            self.compare(current_pin=receipt(self.current,"3"*64))
        foreign_book=snapshot(self.root,"2027-01","d",True,book="OTHER_FINANCIAL_BOOK")
        prior_december=snapshot(self.root,"2026-11","e")
        missing=snapshot(self.root,"2027-01","f",True,has_rollup=False)
        for kwargs, err in ((dict(c=foreign_book),"incomparable_ledger_scope"),
                            (dict(p=prior_december),"months_not_adjacent"),
                            (dict(c=missing),"requires_full_canonical_actuals"),
                            (dict(p=self.current,c=self.prior),"months_not_adjacent")):
            with self.subTest(err=err), self.assertRaisesRegex(MonthlyComparisonError,f"^{err}$"):
                self.compare(**kwargs)

    def test_actual_zero_not_missing_and_no_comparable_has_no_variance(self):
        def restrict(s,r):
            stable = s["raw_composite"]["object_rollups"][0]
            stable["members"] = [member("period","A")]
        def actual_zero(s,r):
            restrict(s,r)
            next(row for row in r if row["id"] == "rent").update(amount="0.00",status="evidenced_zero")
        def no_matches(s,r):
            restrict(s,r)
            s["raw_composite"]["object_rollups"][0]["id"] = "replacement"
        p = snapshot(self.root,"2026-12","1",adjust=actual_zero)
        c = snapshot(self.root,"2027-01","2",True,adjust=restrict)
        o = self.compare(p,c)
        self.assertEqual((o["comparable_variance"],o["comparable_variance_pct"]),("120.00",None))
        row = next(x for x in o["groups"] if x["group_id"] == "stable")
        self.assertEqual((row["prior"]["amount"],row["current"]["amount"]),("0.00","120.00"))
        self.assertTrue(row["prior"]["evidence_ids"])
        c = snapshot(self.root,"2027-01","3",True,adjust=no_matches)
        o = self.compare(p,c)
        self.assertEqual(o["comparable_group_count"],0)
        self.assertIsNone(o["comparable_variance"])
        self.assertIsNone(o["comparable_variance_pct"])

    def test_real_eight_decimal_allocation_in_both_months(self):
        def adjust(s,r):
            root = s["raw_composite"]
            root["object_rollups"][0]["members"] = [member("period","A")]
            rule = root["inputs"]["period"]["allocation_rules"][0]
            rule.update(rounding=8, weights={"A":1,"B":99999999})
            row = next(r for r in r if r["id"] == "rent")
            row["amount"] = "1.00" if row["period"] == "2026-12" else "2.00"
        p = snapshot(self.root,"2026-12","1",adjust=adjust)
        c = snapshot(self.root,"2027-01","2",True,adjust=adjust)
        out = self.compare(p,c)
        row = next(row for row in out["groups"] if row["group_id"] == "stable")
        self.assertEqual((row["prior"]["amount"],row["current"]["amount"],
                          row["variance"],row["variance_pct"]),
                         ("0.00000001","0.00000002","0.00000001","100.00"))
        self.assertEqual((out["prior"]["ledger_actual"],out["current"]["ledger_actual"]),
                         ("2178.10","2179.10"))
        self.assertEqual((out["prior"]["ungrouped_actual"],out["current"]["ungrouped_actual"]),
                         ("1142.09999999","1143.09999998"))

    def test_exact_money_limits_and_ordinary_display(self):
        for v, canonical in (("0.00000000","0.00"), ("7.12000000","7.12"),
                             ("-0.00000001","-0.00000001"), ("0.00120000","0.0012"),
                             ("123","123.00")):
            self.assertEqual(money(amount_atoms(v)),canonical)
        for v in ("0.000000001","1000000000000000000000000000000","NaN","1e-8","- 1","0."):
            with self.assertRaisesRegex(MonthlyComparisonError,"^invalid_ledger_money$"):
                amount_atoms(v)

    def test_percent_has_no_zero_or_credit_denominator(self):
        self.assertEqual(variance(amount_atoms("0"),amount_atoms("5")),("5.00",None))
        self.assertEqual(variance(amount_atoms("-1"),amount_atoms("2.5")),("3.50",None))
        self.assertEqual(variance(amount_atoms("0.8"),amount_atoms("1")),("0.20","25.00"))
        self.assertEqual(variance(amount_atoms("1000"),amount_atoms("999.99")),("-0.01","0.00"))


if __name__ == "__main__":
    unittest.main()
