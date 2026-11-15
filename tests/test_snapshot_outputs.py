"""User-visible invariants: same result, evidence, private scope and exports."""
import copy
import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from cost_core.csv_adapter import CsvCostingAdapter
from cost_core.costing import CostingEngine
from costing_outputs import create_snapshot, load_snapshot, resolve_evidence, answer_snapshot, export_snapshot


class SnapshotAcceptance(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self.ws, self.other = self.base / "user-a", self.base / "user-b"
        self.root = self.base / "shared"
        self.root.mkdir()
        self.dataset = "csv-0123456789abcdef"
        self.folder = self.ws / "costing_datasets" / self.dataset
        self.folder.mkdir(parents=True)
        self.csv = "object_id,object_name,quantity,quantity_unit,cost_element,amount,currency,behavior\np1,我的测试菜品,10,份,材料,120,CNY,variable\n"
        (self.folder / "data.csv").write_text(self.csv, encoding="utf-8")
        self.schema = {"dataset_id": self.dataset, "plugin_id": "csv-generic", "period": "2026-11", "currency": "CNY"}
        ledger = CsvCostingAdapter().from_text(self.csv, self.schema)
        self.result = CostingEngine().analyze(ledger)
        self.context = {"dataset_id": self.dataset, "period": "2026-11", "scenario": "actual", "cost_object": "",
                        "dataset_label": "个人测试CSV", "owner_external_id": "a", "workspace_id": "a"}

    def tearDown(self):
        self.tmp.cleanup()

    def snapshot(self):
        return create_snapshot(self.result, self.ws, self.root, self.context)

    def test_unknown_revenue_never_becomes_loss_in_report_or_answer(self):
        snap = self.snapshot()
        text = snap["report"]["markdown"]
        self.assertIn("无法判断整体盈利或亏损", text)
        self.assertIn("总收入 | 未知", text)
        self.assertIn("经营毛差 | 未知", text)
        answer = answer_snapshot(snap, "我的测试菜品是否亏损？")
        self.assertIn("收入 未知", answer["answer"])
        self.assertNotIn("-120.00", answer["answer"])
        self.assertEqual(answer["model_calls"], 0)

    def test_captured_csv_row_column_value_and_hash(self):
        snap = self.snapshot()
        relevant = [x for x in snap["evidence"] if x.get("field") == "amount"]
        self.assertEqual(len(relevant), 1)
        source = resolve_evidence(snap, relevant[0]["evidence_id"], self.root, self.ws)
        self.assertEqual(source["rows"][0]["record"], 2)
        self.assertEqual(source["rows"][0]["value"], "120")
        self.assertEqual(len(source["sha256"]), 64)
        self.assertTrue(source["current_source_matches"])

    def test_immutable_snapshot_survives_input_change_and_reports_change(self):
        snap = self.snapshot()
        evidence = next(x for x in snap["evidence"] if x.get("field") == "amount")
        (self.folder / "data.csv").write_text(self.csv.replace(",120,", ",999,"), encoding="utf-8")
        loaded = load_snapshot(snap["analysis_id"], self.ws)
        self.assertEqual(loaded["result"]["totals"]["total_cost"], "120")
        source = resolve_evidence(loaded, evidence["evidence_id"], self.root, self.ws)
        self.assertFalse(source["current_source_matches"])
        self.assertEqual(source["rows"][0]["value"], "120")
        self.assertEqual(loaded["charts"]["revision"], loaded["revision"])

    def test_other_workspace_cannot_load_snapshot(self):
        snap = self.snapshot()
        with self.assertRaises(FileNotFoundError):
            load_snapshot(snap["analysis_id"], self.other)
        with self.assertRaises(ValueError):
            load_snapshot("../user-a/" + snap["analysis_id"], self.other)

    def test_result_report_or_chart_tampering_is_rejected(self):
        for part in ("result", "report", "charts"):
            snap = self.snapshot()
            path = self.ws / "costing_snapshots" / (snap["analysis_id"] + ".json")
            bad = copy.deepcopy(snap)
            if part == "result":
                bad[part]["totals"]["total_cost"] = "999"
            elif part == "report":
                bad[part]["markdown"] += "\n销售收入999。"
            else:
                bad[part]["objects"][0]["total_cost"] = "999"
            path.write_text(json.dumps(bad), encoding="utf-8")
            with self.assertRaises(ValueError):
                load_snapshot(snap["analysis_id"], self.ws)

    def test_context_mismatch_is_rejected_before_any_write(self):
        for key, value in (("period", "2026-10"), ("dataset_id", "other"), ("scenario", "budget"), ("cost_object", "other-object")):
            context = dict(self.context, **{key: value})
            with self.assertRaises(ValueError):
                create_snapshot(self.result, self.ws, self.root, context)
        self.assertFalse((self.ws / "costing_snapshots").exists())

    def test_scope_path_cannot_resolve_sibling_source(self):
        result = copy.deepcopy(self.result)
        result["objects"][0]["source_refs"][0]["source_uri"] = "raw/../../secret.csv"
        snap = create_snapshot(result, self.ws, self.root, self.context)
        item = next(x for x in snap["evidence"] if ".." in x["source_uri"])
        self.assertEqual(item["status"], "reference_only")
        self.assertNotIn("rows", item)

    def test_json_and_markdown_exports_are_identical_to_snapshot(self):
        snap = self.snapshot()
        one = export_snapshot(snap, "json", self.ws / "exports")
        two = export_snapshot(snap, "md", self.ws / "exports")
        self.assertEqual(json.loads(one.read_text()), snap)
        self.assertEqual(two.read_text(), snap["report"]["markdown"])
        self.assertNotEqual(one.parent, two.parent)

    def test_no_causal_claim_is_fabricated(self):
        reply = answer_snapshot(self.snapshot(), "是不是设备故障造成亏损？")
        self.assertIn("不能作确定归因", reply["answer"])
        self.assertIn("经营毛差 未知", reply["answer"])

    def test_question_cannot_silently_substitute_another_period(self):
        for question in ("2026-10的收入多少？", "2026年10月的成本是多少？"):
            reply = answer_snapshot(self.snapshot(), question)
            self.assertIn("本次快照不含所需期间", reply["answer"])
            self.assertEqual(reply["evidence_ids"], [])
            self.assertNotIn("120.00", reply["answer"])

    def test_total_question_uses_frozen_scope_total(self):
        reply = answer_snapshot(self.snapshot(), "整体总成本和总收入是多少？")
        self.assertIn("当前范围合计：总成本 120.00，总收入 未知", reply["answer"])

    def test_bad_column_or_selector_cannot_claim_source_is_verified(self):
        for key, value, expected in (("field", "nonexistent", "field_not_found"),
                                    ("selector", "row=2&nonexistent=yes", "selector_field_not_found")):
            result = copy.deepcopy(self.result)
            result["objects"][0]["source_refs"][0][key] = value
            snap = create_snapshot(result, self.ws, self.root, self.context)
            self.assertTrue(any(e["status"] == expected for e in snap["evidence"]))

    def test_markdown_image_payload_stays_plain_text(self):
        self.result["objects"][0]["name"] = "![read-local](/secret/image.png)"
        snap = self.snapshot()
        self.assertNotIn("![read-local]", snap["report"]["markdown"])
        self.assertIn("!［read-local］", snap["report"]["markdown"])

    def test_non_file_source_does_not_claim_hash_match(self):
        result = copy.deepcopy(self.result)
        result["objects"][0]["source_refs"][0]["source_uri"] = "sample://fixture"
        snap = create_snapshot(result, self.ws, self.root, self.context)
        item = next(x for x in snap["evidence"] if x["source_uri"] == "sample://fixture")
        resolved = resolve_evidence(snap, item["evidence_id"], self.root, self.ws)
        self.assertIsNone(resolved["current_source_matches"])


if __name__ == "__main__":
    unittest.main()
