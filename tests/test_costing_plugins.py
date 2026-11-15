"""Integration tests for the restaurant, manufacturing and mapped CSV adapters."""
import json
import hashlib
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from cost_core.costing import CostingEngine, CostingDatasetDefinition, CostingPluginRegistry, CostingService
from cost_core.csv_adapter import CsvCostingAdapter
from industry_plugins.costing_plugins import (RestaurantPlugin, ManufacturingPlugin, GenericCsvPlugin,
                                             read_imported_sources, configuration_sha256)


class IndustryPluginTests(unittest.TestCase):
    def test_restaurant_uses_common_multi_driver_engine(self):
        plugin = RestaurantPlugin()
        dataset = CostingDatasetDefinition(dataset_id="restaurant-sample-v1",
            plugin_id="restaurant", label="restaurant fixture")
        request = plugin.load(dataset, "2026-05", "actual")
        result = CostingEngine().analyze(request)
        self.assertEqual(result["method"], "restaurant_multi_driver_allocation")
        self.assertEqual(len([row for row in result["objects"] if row["kind"] == "menu_item"]), 3)
        self.assertEqual(len(result["allocations"]), 12)
        self.assertEqual(result["totals"]["unallocated_cost"], "0")
        self.assertEqual(sum(__import__("decimal").Decimal(x["amount"]) for x in result["allocations"]),
                         __import__("decimal").Decimal("36600.00"))
        self.assertTrue(all(row["source_refs"] for row in result["objects"] if row["kind"] == "menu_item"))

    def test_manufacturing_uses_two_stage_shared_allocation(self):
        plugin = ManufacturingPlugin()
        dataset = CostingDatasetDefinition(dataset_id="manufacturing-sample-v1",
            plugin_id="manufacturing", label="manufacturing fixture")
        request = plugin.load(dataset, "2026-05", "actual")
        result = CostingEngine().analyze(request)
        self.assertEqual(result["method"], "manufacturing_two_stage_machine_hour_allocation")
        self.assertEqual({row["stage"] for row in result["allocations"]}, {1, 2})
        self.assertEqual(result["totals"]["unallocated_cost"], "0")
        products = [row for row in result["objects"] if row["kind"] == "product"]
        self.assertEqual(len(products), 2)
        self.assertTrue(result["benchmarks"])
        self.assertTrue(result["standard_variances"])

    def test_csv_adapter_maps_direct_entries_and_indirect_pool(self):
        csv_text = (
            "object_id,object_name,object_kind,quantity,quantity_unit,currency,record_type,"
            "cost_element,amount,cost_type,behavior,pool_id,pool_name,pool_owner_id\n"
            "item-a,Item A,product,10,piece,CNY,entry,material,100,direct,variable,,,\n"
            "center,Shared Center,cost_center,,,CNY,pool,rent,50,indirect,fixed,rent-pool,Rent,center\n"
        )
        schema = {
            "dataset_id": "csv-fixture", "plugin_id": "csv-generic",
            "period": "2026-05", "scenario": "actual", "currency": "CNY",
            "columns": {
                "object_id": "object_id", "object_name": "object_name", "object_kind": "object_kind",
                "quantity": "quantity", "quantity_unit": "quantity_unit", "currency": "currency",
                "record_type": "record_type", "cost_element": "cost_element", "amount": "amount",
                "cost_type": "cost_type", "behavior": "behavior", "pool_id": "pool_id",
                "pool_name": "pool_name", "pool_owner_id": "pool_owner_id",
            },
            "allocation_rules": [{
                "rule_id": "rent-rule", "pool_id": "rent-pool", "method": "driver",
                "driver_id": "sales", "target_ids": ["item-a"], "weights": {"item-a": 1},
            }],
        }
        request = CsvCostingAdapter().from_text(csv_text, schema)
        result = CostingEngine().analyze(request)
        self.assertEqual(result["totals"]["total_cost"], "150")
        self.assertEqual(result["objects"][0]["total_cost"], "150")
        self.assertEqual(len(result["allocations"]), 1)

    def test_generic_csv_plugin_is_user_workspace_scoped(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            dataset_dir = root / "costing_datasets" / "csv-12345678"
            dataset_dir.mkdir(parents=True)
            schema = {
                "dataset_id": "csv-12345678", "plugin_id": "csv-generic",
                "period": "2026-05", "scenario": "actual", "currency": "CNY",
                "columns": {"object_id": "object_id", "amount": "amount",
                            "cost_element": "cost_element", "currency": "currency"},
            }
            (dataset_dir / "manifest.json").write_text(json.dumps({
                "dataset_id": "csv-12345678", "label": "Private test", "schema": schema
            }), encoding="utf-8")
            (dataset_dir / "data.csv").write_text(
                "object_id,amount,cost_element,currency\nitem,10,material,CNY\n", encoding="utf-8")
            registry = CostingPluginRegistry()
            registry.register(GenericCsvPlugin(root))
            service = CostingService(registry, (
                CostingDatasetDefinition(dataset_id="csv-12345678", plugin_id="csv-generic",
                                         label="Private test"),
            ))
            result = service.analyze("csv-12345678", "2026-05", "actual")
            self.assertEqual(result["result"]["totals"]["total_cost"], "10")
            self.assertEqual(service.datasets()[0]["dataset_id"], "csv-12345678")
            self.assertEqual(service.datasets()[0]["source_integrity_status"], "legacy_hash_assurance_unavailable")
            self.assertTrue(any(row["code"] == "legacy_source_hash_unavailable" for row in result["result"]["quality"]))
            self.assertNotIn("data_sha256", json.loads((dataset_dir / "manifest.json").read_text(encoding="utf-8")))

    def test_legacy_import_hash_if_present_is_still_enforced(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            dataset_dir = root / "costing_datasets/csv-legacy-hash"
            dataset_dir.mkdir(parents=True)
            raw = b"object_id,amount,cost_element,currency\nitem,10,material,CNY\n"
            (dataset_dir / "data.csv").write_bytes(raw)
            schema = {"period": "2026-05", "currency": "CNY"}
            manifest = {"schema": schema, "data_sha256": hashlib.sha256(raw).hexdigest()}
            manifest_path = dataset_dir / "manifest.json"
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            plugin = GenericCsvPlugin(root)
            dataset = CostingDatasetDefinition(dataset_id="csv-legacy-hash", plugin_id="csv-generic", label="Synthetic legacy")
            request = plugin.load(dataset, "2026-05", "actual")
            self.assertFalse(any(row["code"] == "legacy_source_hash_unavailable" for row in request.preflight_quality))
            self.assertEqual(plugin.describe(dataset)["source_integrity_status"], "matches_import_hashes")
            for bad in ("0" * 64, "", None):
                with self.subTest(hash=bad):
                    manifest["data_sha256"] = bad
                    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
                    with self.assertRaisesRegex(ValueError, "校验值不同"):
                        plugin.load(dataset, "2026-05", "actual")

    def test_bundle_missing_source_or_configuration_hash_is_refused(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "sources").mkdir()
            raw = b"object_id,amount,cost_element,currency\nitem,10,material,CNY\n"
            (root / "sources/finance.csv").write_bytes(raw)
            schema = {"period": "2026-05", "currency": "CNY"}
            manifest = {"storage_format": "costing-bundle/1", "schema": schema,
                        "configuration_sha256": configuration_sha256(schema),
                        "source_files": {"finance": {"path": "sources/finance.csv", "sha256": hashlib.sha256(raw).hexdigest()}}}
            self.assertEqual(read_imported_sources(root, manifest), {"finance": raw.decode("utf-8")})
            del manifest["source_files"]["finance"]["sha256"]
            with self.assertRaisesRegex(ValueError, "校验值不同"):
                read_imported_sources(root, manifest)
            manifest["source_files"]["finance"]["sha256"] = hashlib.sha256(raw).hexdigest()
            del manifest["configuration_sha256"]
            with self.assertRaisesRegex(ValueError, "规则配置"):
                read_imported_sources(root, manifest)


if __name__ == "__main__":
    unittest.main()
