"""Budget compares only exact, same-scope opted-in post-allocation objects."""
from copy import deepcopy
from decimal import Decimal as D
from pathlib import Path
from tempfile import TemporaryDirectory
import json
import unittest

from cost_core.costing import CostingEngine
from cost_core.object_budgets import membership_sha256
from costing_outputs import create_snapshot, export_snapshot, load_snapshot
from test_composite_adapter import evaluate, text
from test_object_rollups import example, member


def plan_row(scope, group, amount, status="budgeted", source="FY26-11 budget row 17"):
    return dict(group_id=group["id"],amount=amount,status=status,source_id=source,
                membership_sha256=membership_sha256(scope,group["id"],group["members"]))


def budget_example(include_zero=False):
    s, r = example()
    opt=s["raw_composite"]
    scope=opt["scope"]
    a=opt["object_rollups"][0]
    b=dict(id="secondary",label="备用对象",members=[member("period","B")])
    opt["object_rollups"].append(b)
    bud=[plan_row(scope,a,"2250.00")]
    if include_zero:
        bud.append(plan_row(scope,b,"0.00","evidenced_zero",source="FY26-11 budget row 18"))
    opt["object_budgets"]=dict(version=1,**scope,rows=bud)
    return s,r


class ObjectBudgets(unittest.TestCase):
    def test_exact_amounts_zero_and_missing_do_not_change_ledger(self):
        s,r=budget_example()
        req,o=evaluate(s,r)
        view=o["recognition"]["object_budgets"]
        self.assertFalse(view["can_claim_complete"])
        self.assertEqual(view["plan_basis"],"declared_only_not_independently_verified")
        self.assertEqual(view["comparison_status"],"partial_comparable_group_set")
        a,b=view["rows"]
        self.assertEqual((a["actual_cost"],a["budget_cost"],a["variance"],a["variance_pct"]),
                         ("2267.10","2250.00","17.10","0.76"))
        self.assertTrue(all(b[k] is None for k in ("budget_cost","variance","variance_pct","budget_source_id")))
        self.assertEqual(b["budget_status"],"missing")
        self.assertEqual((view["summary"]["comparable_actual"],view["summary"]["unmatched_group_actual"],
                          view["summary"]["ledger_actual"]),("2267.10","30.00","2297.10"))
        self.assertEqual(b["budget_status"],"missing")
        filtered=CostingEngine().analyze(req,cost_object="A")["recognition"]
        self.assertNotIn("object_budgets",filtered)
        self.assertNotIn("object_rollups",filtered)
        s["raw_composite"].pop("object_budgets")
        old=evaluate(s,r)[1]
        o["recognition"].pop("object_budgets")
        self.assertEqual(o,old)
        zero=evaluate(*budget_example(True))[1]["recognition"]["object_budgets"]
        self.assertEqual(zero["comparison_status"],"all_rollup_groups_budgeted")
        self.assertEqual((zero["rows"][1]["actual_cost"],zero["rows"][1]["budget_cost"],
                          zero["rows"][1]["variance"],zero["rows"][1]["variance_pct"]),
                         ("30.00","0.00","30.00",None))
        self.assertEqual(zero["summary"]["comparable_variance"],"47.10")

    def test_rollup_receipt_bootstraps_budget_without_reimplementing_digest(self):
        s,r=example()
        opt=s["raw_composite"]
        req,out=evaluate(s,r)
        audit=out["recognition"]
        self.assertNotIn("object_budgets",audit)
        group=audit["object_rollups"]["groups"][0]
        pin=group["membership_sha256"]
        self.assertRegex(pin,r"^[0-9a-f]{64}$")
        filtered=CostingEngine().analyze(req,cost_object="A")["recognition"]
        self.assertNotIn("object_rollups",filtered)
        no_groups=deepcopy(s)
        no_groups["raw_composite"].pop("object_rollups")
        self.assertNotIn("object_rollups",evaluate(no_groups,r)[1]["recognition"])

        opt["object_rollups"][0]["label"]="改名不改口径"
        opt["object_rollups"][0]["members"].reverse()
        scope=audit["context"]
        opt["object_budgets"]=dict(version=1,**scope,rows=[dict(
            group_id=group["id"],membership_sha256=pin,amount="2250.00",
            status="budgeted",source_id="FY26-11 budget row 17")])
        budgeted=evaluate(s,r)[1]["recognition"]
        self.assertEqual(budgeted["object_rollups"]["groups"][0]["label"],"改名不改口径")
        self.assertEqual(budgeted["object_rollups"]["groups"][0]["membership_sha256"],pin)
        self.assertEqual(budgeted["object_budgets"]["rows"][0]["variance"],"17.10")
        opt["object_rollups"][0]["members"].pop()
        with self.assertRaisesRegex(ValueError,"membership differs"):
            evaluate(s,r)

    def test_compiled_context_is_detached_from_caller_scope(self):
        s,r=budget_example()
        req,_=evaluate(s,r)
        scope=s["raw_composite"]["scope"]
        expected=dict(scope)
        scope.update(book="tampered-book",period="2026-12",currency="USD")
        audit=CostingEngine().analyze(req)["recognition"]
        self.assertEqual(audit["context"],expected)
        self.assertEqual(audit["object_budgets"]["scope"],expected)
        self.assertEqual(audit["object_budgets"]["rows"][0]["variance"],"17.10")

    def test_credit_actual_and_uncomparable_ungrouped_actual(self):
        s,r=example()
        opt=s["raw_composite"]
        a=opt["object_rollups"][0]
        a["members"]=[member("power","ENERGY_E")]
        src=next(x for x in r if x["input_name"]=="power")
        debit={k:src[k] for k in ("book","period","currency","billing_account","input_name")}
        r.append(dict(debit,id="late-credit",kind="vendor_credit",amount="-1200.00",
                      evidence="credit credit-memo 081",posting_identity="credit-rebate"))
        opt["object_budgets"]=dict(version=1,**opt["scope"],rows=[plan_row(opt["scope"],a,"5.00")])
        _,o=evaluate(s,r)
        v=o["recognition"]["object_budgets"]
        self.assertEqual(tuple(v["rows"][0][f] for f in ("actual_cost","budget_cost","variance","variance_pct")),
                         ("-58.90","5.00","-63.90","-1278.00"))
        self.assertEqual(v["summary"]["ungrouped_ledger_actual"],"1156.00")
        self.assertEqual(v["summary"]["ledger_actual"],"1097.10")
        self.assertEqual(o["totals"]["total_cost"],"1097.10")

    def test_atomic_strict_validation(self):
        cases={"no rollups":"rollups required","other book":"different book",
               "other period":"different book","other currency":"different book","mutated members":"membership differs",
               "bad pin":"membership differs","noncanonical group":"unknown or duplicate","duplicate budget":"unknown or duplicate",
               "bad id":"invalid budget source","wrong version":"invalid budget declaration","mixed versions":"invalid envelope",
               "bad cents":"exact CNY cents","negative budget":"exact CNY cents","float budget":"exact CNY cents",
               "oversized budget":"exact CNY cents","zero implicit":"zero status mismatch","false zero":"zero status mismatch",
               "spoof":"invalid budget row fields","budget too long":"invalid budget row count"}
        for case, exp in cases.items():
            s,r=budget_example()
            opt=s["raw_composite"]; plan=opt["object_budgets"]; row=plan["rows"][0]
            if case=="no rollups": opt.pop("object_rollups")
            if case=="other book": plan["book"]="wrong"
            if case=="other period": plan["period"]="2026-12"
            if case=="other currency": plan["currency"]="USD"
            if case=="mutated members": opt["object_rollups"][0]["members"].pop()
            if case=="bad pin": row["membership_sha256"]="0"*64
            if case=="noncanonical group": row["group_id"]="Product-Worth"
            if case=="duplicate budget": plan["rows"].append(dict(row))
            if case=="bad id": row["source_id"]="A\u200b"
            if case=="wrong version": plan["version"]=True
            if case=="mixed versions": opt["version"]=2
            if case=="bad cents": row["amount"]="2250.011"
            if case=="negative budget": row["amount"]="-1.00"
            if case=="float budget": row["amount"]=2250.00
            if case=="oversized budget": row["amount"]="1000000000000.00"
            if case=="zero implicit": row["amount"]="0.00"
            if case=="false zero": row["status"]="evidenced_zero"
            if case=="spoof": row["trusted_budget"]=True
            if case=="budget too long": plan["rows"]=[row]*97
            with self.subTest(case=case),self.assertRaisesRegex(ValueError,exp):
                evaluate(s,r)

    def test_manifestated_same_revision_exact_lineage_and_signed_md_below_one(self):
        s,r=budget_example()
        opt=s["raw_composite"]; opt["object_budgets"]["rows"][0]["amount"]="2267.55"
        # Reordering members does not change that explicitly pinned economic membership.
        opt["object_rollups"][0]["members"].reverse()
        _,out=evaluate(s,r)
        with TemporaryDirectory() as t:
            root=Path(t)
            source=root/"costing_datasets"/s["dataset_id"]
            source.mkdir(parents=True)
            (source/"data.csv").write_text(text(r),"utf8")
            (source/"manifest.json").write_text(json.dumps({"schema":s},ensure_ascii=False),"utf8")
            snap=create_snapshot(out,root,root,dict(dataset_id=s["dataset_id"],dataset_label="预算桥接",
                                 period="2026-11",scenario="actual",owner_external_id="qa-only"))
            same=load_snapshot(snap["analysis_id"],root)
            j=json.loads(export_snapshot(same,"json",root/"out").read_text())
            md=export_snapshot(same,"md",root/"out").read_text()
            self.assertEqual(j["revision"],snap["revision"])
            self.assertTrue(all(v in md for v in ("-0.45","2,267.55","申报预算与可比实际",
                                                   "未提供预算不等于零","未配预算的归组实际")))
            self.assertEqual(D(same["result"]["totals"]["total_cost"]),D("2297.10"))
            ev={x["selector"]:x for x in same["evidence"]}
            for selector in ("raw_composite.object_budgets.rows[0]","raw_composite.object_rollups[0]"):
                self.assertEqual(ev[selector]["status"],"captured_mapping")
            self.assertEqual(ev["raw_composite.object_budgets.rows[0]"]["mapping"]["configuration"],
                             opt["object_budgets"]["rows"][0])
            refs=same["result"]["recognition"]["object_budgets"]["rows"][0]["source_refs"]
            self.assertTrue({q["selector"].rsplit("input_name=",1)[1] for q in refs if q["source_uri"]=="upload://data.csv"}==
                            {"period","power","water","stock"})
            self.assertTrue(all(ev[q["selector"]]["status"]=="captured" for q in refs if q["source_uri"]=="upload://data.csv"))


if __name__=="__main__":
    unittest.main()
