"""Read-only rollups on final branch objects, with explicit scope and proof."""
from copy import deepcopy
from decimal import Decimal as D
from pathlib import Path
import json
from tempfile import TemporaryDirectory
import unittest

from cost_core.costing import CostingEngine
from costing_outputs import create_snapshot, load_snapshot, export_snapshot
from test_composite_adapter import evaluate, text
from test_composite_meter_v3 import two_accounts


def member(branch, obj):
    return {"input_name": branch, "object_id": obj}


def example():
    s, r = two_accounts()
    s["raw_composite"]["object_rollups"] = [
        {"id": "product-worth", "label": "冷链产线",
         "members": [member("period", "A"), member("stock", "SKU_1"),
                     member("power", "ENERGY_E"), member("water", "ENERGY_W")]}
    ]
    return s, r


class ObjectRollups(unittest.TestCase):
    def test_additive_view_and_original_evidence(self):
        s, rows = example()
        _, out = evaluate(s, rows)
        view = out["recognition"]["object_rollups"]
        self.assertEqual(view["status"], "partial")
        self.assertFalse(out["recognition"]["can_claim_complete"])
        self.assertEqual((view["grouped_total"], view["ungrouped_total"], view["ledger_total"]),
                         ("2267.10", "30.00", "2297.10"))
        self.assertEqual(D(out["totals"]["total_cost"]), D("2297.10"))
        group = view["groups"][0]
        self.assertEqual(group["total_cost"], "2267.10")
        self.assertEqual([m["total_cost"] for m in group["members"]], ["90.00", "382.80", "1141.10", "653.20"])
        self.assertFalse(any("quantity" in item or "unit_cost" in item for item in [group, *group["members"]]))
        self.assertEqual({x["object_id"]: x["reason"] for x in view["ungrouped"]},
                         {"B": "no_group_declared", "site_period": "pool_owner"})
        baseline = deepcopy(s)
        baseline["raw_composite"].pop("object_rollups")
        old = evaluate(baseline, rows)[1]
        out["recognition"].pop("object_rollups")
        # Grouping adds no ledger amounts, drivers or quantity estimates.
        self.assertEqual(out, old)
        _, persisted = evaluate(s, rows)
        with TemporaryDirectory() as folder:
            root = Path(folder)
            source = root/"costing_datasets"/s["dataset_id"]
            source.mkdir(parents=True)
            (source/"data.csv").write_text(text(rows), "utf-8")
            (source/"manifest.json").write_text(json.dumps({"schema":s}, ensure_ascii=False), "utf-8")
            a = create_snapshot(persisted, root, root, dict(dataset_id=s["dataset_id"], dataset_label="分支归组",
                                period="2026-11", scenario="actual", owner_external_id="qa-only"))
            after = load_snapshot(a["analysis_id"], root)
            j = json.loads(export_snapshot(after,"json",root/"exp").read_text())
            md = export_snapshot(after,"md",root/"exp").read_text()
            self.assertEqual(j["revision"], a["revision"])
            self.assertTrue(all(term in md for term in ("冷链产线", "2,267.10", "未归组小计", "不合并用量")))
            evidence = {e["selector"]:e for e in after["evidence"]}
            cfg = evidence["raw_composite.object_rollups[0]"]
            self.assertEqual(cfg["status"], "captured_mapping")
            self.assertEqual(cfg["mapping"]["configuration"], s["raw_composite"]["object_rollups"][0])
            raw = {ref["selector"] for ref in group["source_refs"] if ref["source_uri"]=="upload://data.csv"}
            self.assertTrue(raw)
            self.assertTrue(all(evidence[key]["status"]=="captured" for key in raw))
            self.assertEqual({"power","water","stock","period"},
                             {key.rsplit("input_name=",1)[1] for key in raw})

    def test_no_partial_or_cross_scope_rollup_in_object_report(self):
        s, rows = example()
        req, out = evaluate(s, rows)
        obj = CostingEngine().analyze(req, cost_object="A")
        self.assertEqual(obj["scope"]["object_ids"], ["A"])
        self.assertNotIn("object_rollups", obj["recognition"])
        self.assertNotIn("_object_rollup_plan", obj["recognition"])
        self.assertEqual(D(obj["totals"]["total_cost"]), D("90"))
        s["raw_composite"]["object_rollups"][0]["members"].append(member("period", "B"))
        complete = evaluate(s, rows)[1]["recognition"]
        self.assertEqual(complete["object_rollups"]["status"], "all_final_objects_grouped")
        self.assertEqual(complete["object_rollups"]["ungrouped_total"], "0")
        self.assertFalse(complete["can_claim_complete"])

    def test_ambiguous_invalid_and_oversized_rejected(self):
        cases = {"version": "invalid envelope", "empty": "invalid group count",
                 "branch": "unknown member branch", "wrong_branch": "incorrect branch",
                 "missing": "unknown member object", "pool_owner": "pool owners",
                 "repeat": "member overlaps", "aliased_group": "duplicate group ID",
                 "denominator": "invalid group fields", "modification": "invalid member fields",
                 "hidden_label": "invalid group label", "too_many_members": "invalid group member count",
                 "too_many_groups": "invalid group count", "total_overflow":"membership exceeds limit"}
        for case, expected in cases.items():
            s, rows = example()
            opt = s["raw_composite"]
            g = opt["object_rollups"][0]
            if case == "version": opt["version"]=2
            if case == "empty": opt["object_rollups"]=[]
            if case == "branch": g["members"]=[member("imaginary","X")]
            if case == "wrong_branch": g["members"]=[member("power","ENERGY_W")]
            if case == "missing": g["members"]=[member("power","不存在")]
            if case == "pool_owner": g["members"]=[member("period","site_period")]
            if case == "repeat": opt["object_rollups"].append(dict(id="other",label="other",members=g["members"][:1]))
            if case == "aliased_group": opt["object_rollups"].append(dict(id="Product-Worth",label="other",members=[member("period","B")]))
            if case == "denominator": g["quantity"]="1"
            if case == "modification": g["members"][0]["amount"]="1"
            if case == "hidden_label": g["label"]="a\u200bb"
            if case == "too_many_members": g["members"]=[member("stock",str(i)) for i in range(65)]
            if case == "too_many_groups": opt["object_rollups"]=[g]*97
            if case == "total_overflow": opt["object_rollups"]=[
                dict(id=f"group-{i}",label="Group",members=[member("stock",str(64*i+j)) for j in range(64)])
                for i in range(9)]
            with self.subTest(case=case), self.assertRaisesRegex(ValueError, expected):
                evaluate(s, rows)


if __name__ == "__main__":
    unittest.main()
