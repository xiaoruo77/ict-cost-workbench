"""Owned multi-file imports exercise the actual ASGI routes and independent oracle."""
import copy
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "scripts"), str(ROOT / "server"), str(ROOT / "fixtures/industry_triad")]
import app as web
import agent_auth
import paths
from fastapi.testclient import TestClient
from generate import build
from profile_contract import make_schema
from industry_plugins.costing_plugins import configuration_sha256


class BundleHttpTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="ict_bundle_http_")
        self.env = patch.dict(os.environ, {"LLM_WIKI_DATA": self.temp.name})
        self.env.start()
        self.auth = agent_auth.Auth()
        self.aid = self.auth.ensure_user("bundle_alice", name="Synthetic Alice")
        self.bid = self.auth.ensure_user("bundle_bob", name="Synthetic Bob")
        self.a = {"Authorization": "Bearer " + self.auth.issue_token(self.aid)}
        self.b = {"Authorization": "Bearer " + self.auth.issue_token(self.bid)}
        self.client = TestClient(web.app, raise_server_exceptions=False)

    def tearDown(self):
        self.client.close()
        self.env.stop()
        self.temp.cleanup()

    def upload(self, bundle=None, schema=None, files=None, headers=None):
        bundle = bundle or build("pharma")
        schema = make_schema(bundle) if schema is None else schema
        files = [("files", (name + ".csv", text.encode("utf-8"), "text/csv"))
                 for name, text in bundle["files"].items()] if files is None else files
        return self.client.post("/api/platform/costing-bundle-import", headers=self.a if headers is None else headers,
            data={"schema_json": json.dumps(schema), "label": "Synthetic " + bundle["industry"]}, files=files)

    def test_three_industries_import_reload_same_revision_evidence_and_exports(self):
        retained = []
        for industry in ("pharma", "restaurant", "medical_devices"):
            with self.subTest(industry=industry):
                bundle = build(industry)
                response = self.upload(bundle)
                self.assertEqual(response.status_code, 200, response.text)
                body = response.json()
                snapshot, dataset = body["analysis"], body["dataset"]
                expected = bundle["expected"]
                self.assertEqual(snapshot["result"]["totals"]["total_cost"], expected["total_cost"])
                for field in ("asset_total", "pnl_cost", "period_expense", "service_cost"):
                    self.assertEqual(snapshot["result"]["business_views"][field], expected[field])
                self.assertEqual(snapshot["context"]["industry"], make_schema(bundle)["industry"])
                self.assertEqual(snapshot["context"]["configuration_sha256"], configuration_sha256(make_schema(bundle)))
                self.assertEqual(snapshot["context"]["owner_external_id"], "bundle_alice")
                root = paths.user_workspace("bundle_alice") / "costing_datasets" / dataset["dataset_id"]
                self.assertFalse((root / "data.csv").exists())
                for name, text in bundle["files"].items():
                    self.assertEqual((root / "sources" / (name + ".csv")).read_bytes(), text.encode("utf-8"))
                    self.assertEqual(body["source_files"][name]["sha256"], hashlib.sha256(text.encode()).hexdigest())
                endpoint = "/api/platform/costing-snapshots/" + snapshot["analysis_id"]
                pin = {"revision": snapshot["revision"]}
                self.assertEqual(self.client.get(endpoint, headers=self.a, params=pin).json()["revision"], snapshot["revision"])
                saved = self.client.post(endpoint + "/report", headers=self.a, params=pin)
                self.assertEqual(saved.status_code, 200, saved.text)
                for fmt in ("json", "md"):
                    exported = self.client.get(endpoint + "/export", headers=self.a, params={**pin, "format": fmt})
                    self.assertEqual(exported.status_code, 200, exported.text)
                    if fmt == "json": self.assertEqual(exported.json()["revision"], snapshot["revision"])
                    else: self.assertIn(snapshot["revision"], exported.text)
                captured = [e for e in snapshot["evidence"] if e["source_uri"].startswith("upload://sources/")]
                self.assertTrue(captured)
                self.assertFalse([e for e in snapshot['evidence'] if e['status']=='selector_not_found'])
                self.assertTrue(all(e["status"] == "captured" and e["rows"] for e in captured))
                for item in captured:
                    evidence = self.client.get(endpoint + "/evidence/" + item["evidence_id"], headers=self.a, params=pin)
                    self.assertEqual(evidence.status_code, 200, evidence.text)
                for suffix, method in [("", "get"), ("/report", "post"), ("/export", "get"),
                                       ("/evidence/" + captured[0]["evidence_id"], "get")]:
                    self.assertEqual(getattr(self.client, method)(endpoint + suffix, headers=self.b, params=pin).status_code, 404)
                    self.assertEqual(getattr(self.client, method)(endpoint + suffix, headers=self.a, params={"revision": "0" * 64}).status_code, 409)
                loaded = self.client.get("/api/platform/costing-analysis", headers=self.a,
                    params={"dataset_id": dataset["dataset_id"], "period": bundle["scope"]["period"]})
                self.assertEqual(loaded.status_code, 200, loaded.text)
                self.assertEqual(loaded.json()["result"], snapshot["result"])
                retained.append(snapshot)
        # Reopening the first industry's immutable revision follows later imports.
        first = retained[0]
        reopened = self.client.get("/api/platform/costing-snapshots/" + first["analysis_id"], headers=self.a,
                                   params={"revision": first["revision"]}).json()
        self.assertEqual(reopened["result"], first["result"])
        self.assertEqual(reopened["context"], first["context"])

    def test_source_tampering_blocks_reload_but_preserves_historical_snapshot(self):
        body = self.upload().json()
        dataset, snapshot = body["dataset"]["dataset_id"], body["analysis"]
        source = paths.user_workspace("bundle_alice") / "costing_datasets" / dataset / "sources/finance.csv"
        source.write_bytes(source.read_bytes() + b"tampered\n")
        listing = self.client.get("/api/platform/costing-industries", headers=self.a).json()
        self.assertNotIn(dataset, [item["dataset_id"] for item in listing["datasets"]])
        self.assertTrue(any(item["dataset_id"] == dataset for item in listing["diagnostics"]))
        self.assertEqual(self.client.get("/api/platform/costing-analysis", headers=self.a,
            params={"dataset_id": dataset, "period": "2026-11"}).status_code, 404)
        old = self.client.get("/api/platform/costing-snapshots/" + snapshot["analysis_id"], headers=self.a)
        self.assertEqual(old.status_code, 200, old.text)
        self.assertEqual(old.json()["revision"], snapshot["revision"])

    def test_bom_source_bytes_hash_and_reload_remain_identical(self):
        bundle = build("pharma")
        bundle["files"] = {name: "\ufeff" + text for name, text in bundle["files"].items()}
        response = self.upload(bundle)
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        dataset, snapshot = body["dataset"]["dataset_id"], body["analysis"]
        root = paths.user_workspace("bundle_alice") / "costing_datasets" / dataset
        manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
        source_hashes = {item["name"]: item["sha256"] for item in snapshot["result"]["recognition"]["source_bundle"]["sources"]}
        for name, text in bundle["files"].items():
            raw = (root / "sources" / (name + ".csv")).read_bytes()
            self.assertEqual(raw, text.encode("utf-8"))
            self.assertTrue(raw.startswith(b"\xef\xbb\xbf"))
            self.assertEqual(manifest["source_files"][name]["sha256"], source_hashes[name])
        loaded = self.client.get("/api/platform/costing-analysis", headers=self.a,
            params={"dataset_id": dataset, "period": bundle["scope"]["period"]})
        self.assertEqual(loaded.status_code, 200, loaded.text)
        self.assertEqual(loaded.json()["result"], snapshot["result"])

    def test_invalid_bundle_is_atomic_and_unauthorized_import_is_refused(self):
        bundle = build("pharma")
        valid = [("files", (name + ".csv", text.encode(), "text/csv")) for name, text in bundle["files"].items()]
        bad_sets = [valid[:-1], valid + [valid[0]],
                    [("files", ("../finance.csv", valid[0][1][1], "text/csv"))] + valid[1:],
                    valid + [("files", ("unknown.csv", b"id\nextra\n", "text/csv"))]]
        for files in bad_sets:
            with self.subTest(names=[item[1][0] for item in files]):
                response = self.upload(bundle, files=files)
                self.assertEqual(response.status_code, 422, response.text)
                self.assertEqual(list(Path(self.temp.name).rglob("manifest.json")), [])
        schema = make_schema(bundle)
        schema["raw_composite"]["source_hashes"] = {"finance": "0" * 64}
        response = self.upload(bundle, schema=schema)
        self.assertEqual(response.status_code, 422, response.text)
        self.assertEqual(list(Path(self.temp.name).rglob("manifest.json")), [])
        self.assertEqual(self.upload(headers={}).status_code, 401)
        with patch("costing_outputs.create_snapshot", side_effect=RuntimeError("synthetic storage failure")):
            failed = self.upload(bundle)
        self.assertEqual(failed.status_code, 500)
        self.assertEqual(list(Path(self.temp.name).rglob("manifest.json")), [])
        dataset_root = paths.user_workspace("bundle_alice") / "costing_datasets"
        self.assertEqual(list(dataset_root.iterdir()), [])


if __name__ == "__main__":
    unittest.main()
