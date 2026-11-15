"""HTTP regression gates; all identities/data/network responses are isolated fixtures."""
from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "server"))
import app as web
import agent_auth
import paths
import costing_outputs
import rpa_client
from fastapi.testclient import TestClient


CSV = "object_id,object_name,amount,cost_element,currency,quantity,quantity_unit,behavior\nP1,Fixture product,100,materials,CNY,10,piece,variable\n"
SCHEMA = {"period": "2026-05", "currency": "CNY", "allocation_rules": []}


class WebContextTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="ict_web_regression_")
        self.env = patch.dict(os.environ, {"LLM_WIKI_DATA": self.tmp.name})
        self.env.start()
        auth = agent_auth.Auth()
        self.aid = auth.ensure_user("fixture_alice", name="Alice")
        self.bid = auth.ensure_user("fixture_bob", name="Bob")
        self.a = {"Authorization": "Bearer " + auth.issue_token(self.aid)}
        self.b = {"Authorization": "Bearer " + auth.issue_token(self.bid)}
        self.auth = auth
        self.client = TestClient(web.app, raise_server_exceptions=False)
        web._MODEL_ACTIVE.clear()
        web._MODEL_REQUESTS.clear()

    def tearDown(self):
        self.client.close()
        self.env.stop()
        self.tmp.cleanup()

    def upload(self, schema=None, headers=None, csv=CSV):
        return self.client.post("/api/platform/costing-import", headers=self.a if headers is None else headers,
                                files={"file": ("fixture.csv", csv.encode(), "text/csv")},
                                data={"schema_json": json.dumps(SCHEMA if schema is None else schema), "label": "Fixture dataset"})

    def test_import_stable_identity_and_same_snapshot_flow(self):
        response = self.upload()
        self.assertEqual(response.status_code, 200, response.text)
        data = response.json()
        dataset, snapshot = data["dataset"], data["analysis"]
        ident = snapshot["analysis_id"]
        self.assertTrue((paths.user_workspace("fixture_alice") / "costing_datasets" / dataset["dataset_id"] / "data.csv").is_file())
        self.assertEqual(snapshot["context"]["owner_external_id"], "fixture_alice")
        self.assertEqual(snapshot["result"]["objects"][0]["unit_cost"], "10")
        self.assertIsNone(snapshot["result"]["totals"]["total_revenue"])
        self.assertIn("未知", snapshot["report"]["markdown"])
        endpoint = "/api/platform/costing-snapshots/" + ident
        saved = self.client.post(endpoint + "/report", headers=self.a)
        self.assertEqual(saved.status_code, 200, saved.text)
        reopened = self.client.get(endpoint, headers=self.a)
        self.assertEqual(reopened.json()["revision"], snapshot["revision"])
        asked = self.client.post(endpoint + "/question", headers=self.a, json={"question": "Fixture product利润是多少？"})
        self.assertEqual(asked.status_code, 200, asked.text)
        self.assertEqual(asked.json()["revision"], snapshot["revision"])
        self.assertIn("未知", asked.json()["answer"])
        chart = self.client.get(endpoint + "/charts", headers=self.a).json()
        self.assertEqual(chart["revision"], snapshot["revision"])
        self.assertEqual(chart["charts"]["objects"][0]["total_cost"], "100")
        evidence_id = next(e["evidence_id"] for e in snapshot["evidence"] if e["source_uri"] == "upload://data.csv")
        evidence = self.client.get(endpoint + "/evidence/" + evidence_id, headers=self.a)
        self.assertEqual(evidence.status_code, 200, evidence.text)
        self.assertEqual(evidence.json()["rows"][0]["record"], 2)
        self.assertEqual(evidence.json()["rows"][0]["values"]["amount"], "100")
        exported = self.client.get(endpoint + "/export", params={"format": "json"}, headers=self.a)
        self.assertEqual(exported.status_code, 200, exported.text)
        self.assertEqual(exported.json()["revision"], snapshot["revision"])
        workspace = self.client.get("/api/control/workspace", headers=self.a).json()
        self.assertIn("costing_datasets/" + dataset["dataset_id"] + "/data.csv", [f["path"] for f in workspace["raw"]])
        for suffix, method in [("", "get"), ("/report", "post"), ("/charts", "get"), ("/export", "get"), ("/evidence/" + evidence_id, "get")]:
            denied = getattr(self.client, method)(endpoint + suffix, headers=self.b)
            self.assertEqual(denied.status_code, 404, denied.text)
        self.assertEqual(self.client.get(endpoint).status_code, 401)
        private = self.client.get("/api/platform/costing-analysis", headers=self.b, params={"dataset_id": dataset["dataset_id"], "period": "2026-05"})
        self.assertEqual(private.status_code, 404)

    def test_import_rejects_bad_shapes_and_never_commits(self):
        bad = [None, [], {**SCHEMA, "columns": 123}, {**SCHEMA, "columns": {"amount": []}},
               {**SCHEMA, "currency_rates": [{}]}, {**SCHEMA, "currency_rates": 123},
               {**SCHEMA, "allocation_rules": 123}, {**SCHEMA, "allocation_rules": [{"target_ids": "P1"}]},
               {**SCHEMA, "unit_conversions": [{}]}, {**SCHEMA, "plugin_id": "unexpected-plugin"}]
        for schema in bad:
            with self.subTest(schema=schema):
                response = self.client.post("/api/platform/costing-import", headers=self.a,
                    files={"file": ("fixture.csv", CSV.encode(), "text/csv")}, data={"schema_json": json.dumps(schema)})
                self.assertEqual(response.status_code, 422, response.text)
                self.assertEqual(list(Path(self.tmp.name).rglob("manifest.json")), [])
        with patch.object(costing_outputs, "create_snapshot", side_effect=RuntimeError("fixture storage failure")):
            response = self.upload()
            self.assertEqual(response.status_code, 500)
            self.assertEqual(list(Path(self.tmp.name).rglob("manifest.json")), [])
            self.assertEqual(list(paths.user_workspace("fixture_alice").glob("costing_datasets/*")), [])

    def test_dataset_list_excludes_staging_and_reports_corruption(self):
        dataset = self.upload().json()["dataset"]["dataset_id"]
        root = paths.user_workspace("fixture_alice") / "costing_datasets"
        temp = root / (dataset + ".tmp-fixture")
        temp.mkdir()
        (temp / "manifest.json").write_text("{}")
        (root / dataset / "data.csv").write_text("changed", encoding="utf-8")
        response = self.client.get("/api/platform/costing-industries", headers=self.a)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertNotIn(dataset, [x["dataset_id"] for x in response.json()["datasets"]])
        self.assertEqual(response.json()["diagnostics"][0]["dataset_id"], dataset)
        self.assertIn("校验", response.json()["diagnostics"][0]["message"])

    def test_legacy_workspace_recovery_preserves_original_and_identity(self):
        response = self.upload().json()
        dataset = response["dataset"]["dataset_id"]
        current = paths.user_workspace("fixture_alice") / "costing_datasets" / dataset
        old = paths.data_root() / ("users/user_id___" + str(self.aid) + "___external_id____fixture_alice") / "ws/costing_datasets" / dataset
        old.parent.mkdir(parents=True)
        current.rename(old)
        record = json.loads((old / "manifest.json").read_text())
        record["uploaded_by"] = str(self.aid)
        (old / "manifest.json").write_text(json.dumps(record), encoding="utf-8")
        recovered = paths.recover_legacy_costing_datasets("fixture_alice", self.aid)
        self.assertEqual(recovered, [dataset])
        self.assertTrue((old / "data.csv").exists())
        self.assertTrue((current / "data.csv").exists())
        self.assertEqual(paths.recover_legacy_costing_datasets("fixture_bob", self.bid), [])
        with self.assertRaises(ValueError):
            paths.user_workspace({"external_id": "fixture_alice"})

    def test_cookie_delete_requires_csrf_and_positive_route_works(self):
        self.client.cookies.set("lw_sid", self.auth.create_session(self.aid))
        self.client.cookies.set("lw_csrf", "fixture-csrf")
        self.assertEqual(self.client.delete("/api/conversations/7").status_code, 403)
        fake = types.SimpleNamespace(user_id=self.aid, mem=types.SimpleNamespace(delete_conversation=lambda *a: True))
        with patch.object(web, "_conv_guard", return_value=fake):
            deleted = self.client.delete("/api/conversations/7", headers={"X-CSRF-Token": "fixture-csrf"})
            self.assertEqual(deleted.status_code, 200, deleted.text)
        self.client.cookies.delete("lw_csrf")
        self.assertEqual(self.client.delete("/api/conversations/7").status_code, 403)

    def test_paid_routes_authentication_concurrency_and_context(self):
        called = []
        router = types.ModuleType("router")
        router.answer = lambda *a, **kw: called.append(kw) or {"answer": "fixture"}
        benchmark = types.ModuleType("benchmark_engine")
        benchmark.run = lambda *a, **kw: called.append(kw) or {"steps": [{}, {}, {}], "steps_meta": []}
        with patch.dict(sys.modules, {"router": router, "benchmark_engine": benchmark}), patch.object(web.RF, "list_products", return_value=["fixture_product"]):
            self.assertEqual(self.client.post("/api/route", json={"question": "test"}).status_code, 401)
            self.assertEqual(self.client.get("/api/benchmark", params={"product": "fixture_product", "llm": 1}).status_code, 401)
            self.assertEqual(called, [])
            release = web._acquire_model_slot({"external_id": "fixture_alice"})
            try:
                self.assertEqual(self.client.post("/api/route", headers=self.a, json={"question": "test"}).status_code, 429)
                self.assertEqual(called, [])
            finally:
                release()
            response = self.client.post("/api/route", headers=self.a, json={"question": "test"})
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(called[-1]["workspace_root"], paths.user_workspace("fixture_alice"))
            self.assertEqual(web._MODEL_ACTIVE["fixture_alice"], 0)
            public = self.client.get("/api/benchmark", params={"product": "fixture_product", "llm": 0})
            self.assertEqual(public.status_code, 200, public.text)

    def test_public_samples_readonly_and_unknown_report_product_refused(self):
        result = self.client.get("/api/platform/costing-analysis", params={"dataset_id": "restaurant-sample-v1", "period": "2026-05"})
        self.assertEqual(result.status_code, 200, result.text)
        self.assertTrue(result.json()["requires_login_to_save"])
        self.assertNotIn("analysis_id", result.json())
        self.assertEqual(self.upload(headers={}).status_code, 401)
        for theme in ("quarterly", "topic"):
            with patch.object(web.RF, "list_products", return_value=["fixture_supported"]):
                response = self.client.post("/api/control/generate", headers=self.a, json={"product": "fixture_product", "theme": theme})
                self.assertEqual(response.status_code, 422, response.text)

    def test_rpa_report_metadata_and_cross_user_task_guard(self):
        reports = paths.ensure_workspace("fixture_alice") / "reports"
        source = reports / "arbitrary_name_with_no_date.md"
        source.write_text("### 6.4 整改任务清单\n| TASK-202605-001 | Fixture task | 财务部 | 高 | 月度报告 | 2026-06-30 |\n", encoding="utf-8")
        metadata = {"report_id": "fixture-report", "product": "fixture_product", "period": "2026-05", "theme": "monthly", "dataset_id": "fixture", "rpa_supported": True}
        source.with_name(source.name + ".meta.json").write_text(json.dumps(metadata), encoding="utf-8")
        sent = []
        fake = types.SimpleNamespace(dispatch=lambda values: sent.extend(values) or {"ok": True},
                                     get_task=lambda tid: {"ok": True, "task": {"source": {"owner_external_id": "fixture_alice"}}})
        with patch.object(web, "_rpa_client", return_value=fake):
            rejected = self.client.post("/api/rpa/dispatch", headers=self.a, json={"report": source.name, "month": "2099-01"})
            self.assertEqual(rejected.status_code, 422)
            accepted = self.client.post("/api/rpa/dispatch", headers=self.a, json={"report": source.name})
            self.assertEqual(accepted.status_code, 200, accepted.text)
            self.assertEqual(sent[0]["source"]["analysis_month"], "2026-05")
            self.assertEqual(sent[0]["source"]["product"], "fixture_product")
            self.assertEqual(sent[0]["source"]["owner_external_id"], "fixture_alice")
            self.assertEqual(self.client.get("/api/rpa/tasks/fixture-task", headers=self.a).status_code, 200)
            self.assertEqual(self.client.get("/api/rpa/tasks/fixture-task", headers=self.b).status_code, 404)

    def test_legacy_three_themes_freeze_metadata_sources_and_export_context(self):
        shared = Path(self.tmp.name) / "shared_fixture"
        (shared / "raw/csv").mkdir(parents=True)
        (shared / "reports").mkdir()
        (shared / "raw/csv/fixture.csv").write_text("product,period,unit_cost\nfixture_product,2026-05,10\n", encoding="utf-8")
        source_ref = {"dataset_id": "pharma-sample-v1", "source_uri": "raw/csv/fixture.csv", "field": "unit_cost", "selector": "row=2", "kind": "raw", "formula": ""}
        frozen = {"product": "fixture_product", "month": "2026-05", "facts": {"本月单位成本": {"value": 10, "source_refs": [source_ref]}}, "monthly": [], "peer": []}
        called, exported_contexts = [], []
        builder = types.ModuleType("report_build")
        builder.TOPIC_ELEMENTS = ["直接材料"]
        builder.build = lambda *args, **kwargs: called.append(kwargs) or {"report": "# Fixture report\n\n成本10\n", "stats": {"deterministic_fields": 1, "export_facts": frozen}}
        periods = types.ModuleType("period_agg")
        periods.AVAILABLE_QUARTERS = ["2026-Q2"]
        exporter = types.ModuleType("export_doc")
        def export(src, fmt, out_dir, **kwargs):
            exported_contexts.append(kwargs["context"])
            out_dir.mkdir(parents=True, exist_ok=True)
            output = out_dir / "fixture.docx"
            output.write_bytes(b"fixture isolated export")
            return [output]
        exporter.export = export
        with patch.dict(sys.modules, {"report_build": builder, "period_agg": periods, "export_doc": exporter}), patch.object(web.RF, "list_products", return_value=["fixture_product"]), patch.object(web, "WIKI_ROOT", shared), patch.object(web, "REPORTS", shared / "reports"):
            for theme, period, expected_period in [("monthly", "", "2026-06"), ("quarterly", "2026-Q2", "2026-Q2"), ("topic", "直接材料", "2026-06")]:
                response = self.client.post("/api/control/generate", headers=self.a, json={"product": "fixture_product", "month": "2026-06", "theme": theme, "period": period, "use_llm": False})
                self.assertEqual(response.status_code, 200, response.text)
                name = response.json()["file"]
                self.assertFalse(called[-1]["use_llm"])
                self.assertEqual(called[-1]["workspace_root"], shared)
                record = json.loads((paths.user_workspace("fixture_alice") / "reports" / (name + ".meta.json")).read_text())
                self.assertEqual(record["period"], expected_period)
                self.assertEqual(record["export_facts"], frozen)
                self.assertTrue(record["source_revision"])
                evidence = self.client.get("/api/control/report-evidence", headers=self.a, params={"report": name}).json()["evidence"]
                self.assertEqual(len(evidence), 1)
                detail = self.client.get("/api/control/report-evidence", headers=self.a, params={"report": name, "evidence_id": evidence[0]["evidence_id"]})
                self.assertEqual(detail.json()["evidence"]["rows"][0]["values"]["unit_cost"], "10")
                exported = self.client.post("/api/control/export", headers=self.a, json={"md": name, "fmt": "docx", "charts": True})
                self.assertEqual(exported.status_code, 200, exported.text)
                self.assertEqual(exported_contexts[-1]["export_facts"], frozen)
                self.assertEqual(self.client.post("/api/control/export", headers=self.b, json={"md": name, "fmt": "docx"}).status_code, 404)
                self.assertEqual(self.client.get("/api/control/report-evidence", headers=self.b, params={"report": name}).status_code, 404)
            before = len(called)
            bad_cutoff = self.client.post("/api/control/generate", headers=self.a, json={"product": "fixture_product", "month": "2026-05", "theme": "quarterly", "period": "2026-Q2", "use_llm": False})
            self.assertEqual(bad_cutoff.status_code, 422)
            self.assertEqual(len(called), before)
            listed = self.client.get("/api/control/exports", headers=self.a).json()["reports"]
            self.assertEqual(len(listed), 3)
            self.assertNotIn("export_facts", listed[0]["metadata"])

    def test_cookie_benchmark_cannot_charge_without_csrf(self):
        self.client.cookies.set("lw_sid", self.auth.create_session(self.aid))
        self.client.cookies.set("lw_csrf", "fixture-csrf")
        called = []
        benchmark = types.ModuleType("benchmark_engine")
        benchmark.run = lambda *a, **kw: called.append(kw) or {"steps": [{}, {}, {}], "steps_meta": []}
        with patch.dict(sys.modules, {"benchmark_engine": benchmark}), patch.object(web.RF, "list_products", return_value=["fixture_product"]):
            self.assertEqual(self.client.get("/api/benchmark", params={"product": "fixture_product", "llm": 1}).status_code, 403)
            self.assertEqual(called, [])
            accepted = self.client.get("/api/benchmark", params={"product": "fixture_product", "llm": 1}, headers={"X-CSRF-Token": "fixture-csrf"})
            self.assertEqual(accepted.status_code, 200, accepted.text)
            self.assertEqual(len(called), 1)

    def test_concurrent_legacy_exports_use_distinct_directories_and_zip_files(self):
        from concurrent.futures import ThreadPoolExecutor
        import io
        import threading
        import zipfile
        report_dir = paths.ensure_workspace("fixture_alice") / "reports"
        source = report_dir / "fixture_monthly.md"
        source.write_text("# Fixture monthly report", encoding="utf-8")
        source.with_name(source.name + ".meta.json").write_text(json.dumps({"report_id": "fixture-export", "theme": "monthly", "product": "fixture", "period": "2026-05"}), encoding="utf-8")
        destinations = []
        barrier = threading.Barrier(2)
        exporter = types.ModuleType("export_doc")
        def export(src, fmt, out_dir, **kwargs):
            destinations.append(out_dir)
            barrier.wait(timeout=5)
            outputs = [out_dir / "fixture.docx", out_dir / "fixture.pdf"]
            for output in outputs:
                output.write_bytes((out_dir.name + output.suffix).encode())
            return outputs
        exporter.export = export
        def request_export():
            with TestClient(web.app) as client:
                return client.post("/api/control/export", headers=self.a, json={"md": source.name, "fmt": "both", "charts": True})
        with patch.dict(sys.modules, {"export_doc": exporter}), ThreadPoolExecutor(max_workers=2) as executor:
            futures = [executor.submit(request_export) for _ in range(2)]
            responses = [future.result(timeout=10) for future in futures]
        self.assertEqual(len(set(destinations)), 2)
        for response in responses:
            self.assertEqual(response.status_code, 200)
            with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
                self.assertEqual(set(archive.namelist()), {"fixture.docx", "fixture.pdf"})
        for directory in destinations:
            self.assertTrue((directory / "fixture.docx").is_file())
            self.assertTrue((directory / "fixture.pdf").is_file())
            self.assertTrue((directory / "fixture_monthly.zip").is_file())


class RpaRegressionTests(unittest.TestCase):
    def test_pagination_25_and_encoded_filters(self):
        client = rpa_client.RPAClient()
        rows = [{"task_id": f"t-{i}", "task_title": str(i), "status": "confirmed"} for i in range(25)]
        calls = []
        def req(method, path, body=None):
            from urllib.parse import parse_qs, urlsplit
            query = parse_qs(urlsplit(path).query)
            calls.append(query)
            page = int(query["page"][0])
            # A real server may impose a page size smaller than requested.
            return {"data": {"total": 25, "page": page, "page_size": 20, "tasks": rows[(page - 1) * 20:page * 20]}}
        with patch.object(client, "_req", side_effect=req):
            result = client.list_all_tasks(owner_external_id="测试 & owner")
        tasks = client.extract_tasks(result)
        self.assertEqual(len(tasks), 25)
        self.assertEqual(client.summarize_status(tasks)["confirmed"], 25)
        self.assertEqual(calls[0]["owner_external_id"], ["测试 & owner"])

    def test_outbox_failure_survives_new_client_and_retry(self):
        payload = {"task_id": "fixture-task", "task_title": "Fixture task"}
        with tempfile.TemporaryDirectory() as directory:
            client = rpa_client.RPAClient(outbox_dir=Path(directory))
            with patch.object(client, "_req", side_effect=OSError("fixture offline")):
                failed = client.dispatch([payload])
            self.assertEqual(failed["pending_saved"], 1)
            self.assertEqual(failed["sent"], 0)
            reopened = rpa_client.RPAClient(outbox_dir=Path(directory))
            self.assertEqual(len(reopened.pending_tasks()), 1)
            with patch.object(reopened, "_req", return_value={"data": {"total": 0, "tasks": [], "status": "sent"}}):
                result = reopened.retry_pending()
            self.assertEqual(result["sent"], 1)
            self.assertEqual(reopened.pending_tasks(), [])

    @unittest.skipUnless((ROOT / "server/mock_rpa.py").is_file(), "Legacy RPA mock service is not distributed")
    def test_mock_assignee_schema_persistence_and_page_validation(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {"RPA_STATE_FILE": str(Path(directory) / "state.json")}):
            spec = importlib.util.spec_from_file_location("fixture_rpa_mock", ROOT / "server/mock_rpa.py")
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            fixture = {"task_id": "t-1", "task_title": "fixture", "priority": "high", "status": "confirmed", "deadline": "2026-06-30", "created_at": "2026-05-01", "assignee": {"name": "Fixture owner", "department": "Finance"}, "source": {"owner_external_id": "fixture_alice"}}
            module.tasks_db["t-1"] = fixture
            module._persist_state()
            client = TestClient(module.app)
            response = client.get("/api/rpa/tasks", params={"owner_external_id": "fixture_alice"})
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(response.json()["data"]["tasks"][0]["assignee"]["name"], "Fixture owner")
            self.assertEqual(client.get("/api/rpa/tasks", params={"page": 0}).status_code, 422)
            self.assertEqual(client.get("/api/rpa/tasks", params={"page_size": -1}).status_code, 422)
            module.tasks_db.clear()
            module._load_state()
            self.assertIn("t-1", module.tasks_db)
            client.close()


if __name__ == "__main__":
    unittest.main()
