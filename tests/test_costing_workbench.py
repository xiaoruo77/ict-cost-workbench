"""Regression coverage for workbench example safety and private report ownership."""
import csv
import hashlib
import io
import json
import os
from pathlib import Path
import tempfile
import unittest

from costing_workbench import get_example, list_examples, personal_reports, document_target

ROOT = Path(__file__).resolve().parents[1] / "fixtures"
OWNER = "synthetic-owner-a"
OTHER = "synthetic-owner-b"


class WorkbenchExamplesTests(unittest.TestCase):
    def test_real_examples_preserve_independent_expected_totals_and_source_rows(self):
        examples = list_examples(ROOT)
        self.assertEqual(examples["diagnostics"], [])
        totals = {v["id"]: v["expected_total"] for v in examples["examples"]}
        self.assertEqual(totals, {"pharma": "2400.00", "restaurant": "760.50",
                                  "medical_devices": "2300.00", "logistics": "1762.00"})
        for entry in examples["examples"]:
            metadata, schema, files = get_example(entry["id"], ROOT)
            self.assertTrue(metadata["available"])
            self.assertEqual(metadata["data_kind"], "synthetic")
            self.assertEqual(sum(x["row_count"] for x in metadata["source_files"]),
                             {"pharma": 30, "restaurant": 132,
                              "medical_devices": 54, "logistics": 48}[entry["id"]])
            for item in metadata["source_files"]:
                self.assertEqual(hashlib.sha256(files[item["name"]]).hexdigest(), item["sha256"])
            self.assertEqual(schema["plugin_id"], "csv-generic")

    def tiny(self, directory):
        root = Path(directory)
        folder = root / "demo";folder.mkdir()
        raw = b"id,amount\nROW-1,100.00\n"
        (folder / "finance.csv").write_bytes(raw)
        schema = {"period": "2026-11", "currency": "CNY", "raw_composite": {
            "scope": {"book": "SYNTH", "period": "2026-11", "currency": "CNY"},
            "source_hashes": {"finance": hashlib.sha256(raw).hexdigest()}}}
        (folder / "rules.json").write_text(json.dumps(schema), encoding="utf-8")
        (folder / "expected.json").write_text(json.dumps({"total_cost": "100.00"}), encoding="utf-8")
        catalog = {"version": 1, "examples": [{
            "id": "demo", "name": "合成", "description": "测试来源", "scope": "测试",
            "folder": "demo"}]}
        (root / "workbench_examples.json").write_text(json.dumps(catalog), encoding="utf-8")
        return root, catalog

    def test_changed_original_source_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root, _ = self.tiny(directory)
            (root / "demo/finance.csv").write_bytes(b"id,amount\nROW-1,101.00\n")
            with self.assertRaisesRegex(ValueError, "hash mismatch"):
                get_example("demo", root)
            self.assertEqual(list_examples(root)["examples"], [])

    def test_catalog_paths_cannot_leave_fixture_root(self):
        with tempfile.TemporaryDirectory() as directory:
            root, catalog = self.tiny(directory)
            catalog["examples"][0]["folder"] = "../outside"
            (root / "workbench_examples.json").write_text(json.dumps(catalog), encoding="utf-8")
            with self.assertRaises(ValueError):
                get_example("demo", root)

    def test_unknown_and_duplicate_example_ids_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root, catalog = self.tiny(directory)
            with self.assertRaises(LookupError):
                get_example("missing", root)
            catalog["examples"].append(dict(catalog["examples"][0]))
            (root / "workbench_examples.json").write_text(json.dumps(catalog), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "identifier"):
                list_examples(root)

    def test_symlinked_example_source_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root, _ = self.tiny(directory)
            source = root / "demo/finance.csv"
            target = root / "original.csv";target.write_bytes(source.read_bytes())
            source.unlink()
            source.symlink_to(target)
            with self.assertRaisesRegex(ValueError, "source"):
                get_example("demo", root)


class WorkbenchPrivateReportsTests(unittest.TestCase):
    def record(self, root, ident, owner=OWNER, revision="a" * 64, modified=1000):
        reports = root / "reports";reports.mkdir(exist_ok=True)
        path = reports / ("costing-" + ident + ".md");path.write_text("合成报告", encoding="utf-8")
        metadata = {"analysis_id": ident, "revision": revision, "theme": "costing",
                    "owner_external_id": owner, "workspace_id": owner, "scope": "shared_sample"}
        path.with_name(path.name + ".meta.json").write_text(json.dumps(metadata), encoding="utf-8")
        os.utime(path, (modified, modified))
        return path

    @staticmethod
    def loader(path, scope):
        metadata = json.loads(path.with_name(path.name + ".meta.json").read_text(encoding="utf-8"))
        return {"name": path.name, "metadata": metadata, "analysis_id": metadata["analysis_id"],
                "scope": scope}

    def test_only_owned_cost_reports_are_listed_with_real_saved_time(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.record(root, "1" * 32, modified=1000)
            self.record(root, "2" * 32, modified=2000)
            self.record(root, "3" * 32, owner=OTHER, modified=3000)
            (root / "reports/shared-doc.md").write_text("其他文档", encoding="utf-8")
            rows, diagnostics = personal_reports(root, OWNER, self.loader)
            self.assertEqual([r["analysis_id"] for r in rows], ["2" * 32, "1" * 32])
            self.assertEqual(len(diagnostics), 1)
            self.assertTrue(all(r["scope"] == "personal" for r in rows))
            self.assertTrue(all(r["source_scope"] == "shared_sample" for r in rows))
            self.assertEqual(rows[0]["saved_at"], "1970-01-01T00:33:20+00:00")

    def test_wrong_revision_and_filename_identity_do_not_become_saved_reports(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory);self.record(root, "1" * 32, revision="bad")
            path = self.record(root, "2" * 32)
            meta = path.with_name(path.name + ".meta.json")
            value = json.loads(meta.read_text());value["analysis_id"] = "4" * 32
            meta.write_text(json.dumps(value), encoding="utf-8")
            rows, diagnostics = personal_reports(root, OWNER, self.loader)
            self.assertEqual(rows, []);self.assertEqual(len(diagnostics), 2)

    def test_report_directory_symlink_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            parent = Path(directory);outside = parent / "outside";outside.mkdir()
            owned = parent / "owned";owned.mkdir();(owned / "reports").symlink_to(outside, target_is_directory=True)
            with self.assertRaisesRegex(ValueError, "outside"):
                personal_reports(owned, OWNER, self.loader)



class WorkbenchDocumentBoundaryTests(unittest.TestCase):
    def test_costing_sources_snapshots_and_reports_are_protected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for relative in ("costing_datasets/csv-0123456789abcdef/sources/finance.csv",
                             "costing_snapshots/analysis.json", "reports/costing-report.md",
                             "auth.json"):
                with self.subTest(relative=relative):
                    with self.assertRaises(PermissionError):
                        document_target(root, relative)

    def test_supplementary_files_in_raw_wiki_and_inbox_remain_accessible(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for relative in ("raw/invoice.csv", "wiki/topic.md", "inbox/upload.pdf",
                             "raw/reports/supplement.md"):
                self.assertEqual(document_target(root, relative), (root / relative).resolve())

    def test_path_escape_and_costing_symlink_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with self.assertRaises(ValueError):
                document_target(root, "../foreign.md")
            (root / "raw").mkdir()
            target = root / "costing_datasets";target.mkdir()
            (root / "raw/shortcut").symlink_to(target, target_is_directory=True)
            with self.assertRaises(PermissionError):
                document_target(root, "raw/shortcut/source.csv")


if __name__ == "__main__":
    unittest.main()