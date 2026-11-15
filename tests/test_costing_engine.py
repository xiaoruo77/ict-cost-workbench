"""Regression tests for the shared cost ledger and staged allocation engine."""
from decimal import Decimal as D
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from cost_core.costing import (
    AllocationRule, CostBenchmark, CostDriver, CostEntry, CostObject, CostPool, CostingEngine,
    CostingInput, CurrencyRate, StandardCost, UnitConversion,
)
from cost_core.models import SourceRef


def ref(dataset="fixture"):
    return SourceRef(dataset_id=dataset, source_uri="fixture://ledger",
                     field="amount", selector="row=1", kind="raw")


class CostingEngineTests(unittest.TestCase):
    def test_rounding_residual_preserves_pool_total(self):
        objects = tuple(CostObject(object_id=f"p{i}", name=f"Product {i}",
                                   quantity=D("1"), quantity_unit="piece")
                        for i in range(3)) + (
            CostObject(object_id="cc", name="Shared center", kind="cost_center"),)
        pool = CostPool(pool_id="pool", name="shared", owner_id="cc",
                        amount=D("100.01"), currency="CNY", source_refs=(ref(),))
        driver = CostDriver(driver_id="units", name="units", unit="count",
                            values={"p0": D("1"), "p1": D("1"), "p2": D("1")})
        rule = AllocationRule(rule_id="r", pool_id="pool",
                              target_ids=("p0", "p1", "p2"), driver_id="units",
                              method="driver", rounding=2)
        result = CostingEngine().analyze(CostingInput(
            dataset_id="fixture", plugin_id="test", period="2026-05",
            scenario="actual", currency="CNY", objects=objects,
            pools=(pool,), drivers=(driver,), rules=(rule,)))
        amounts = [D(row["amount"]) for row in result["allocations"]]
        self.assertEqual(sum(amounts), D("100.01"))
        self.assertEqual(D(result["totals"]["total_cost"]), D("100.01"))
        self.assertEqual(D(result["totals"]["unallocated_cost"]), D("0"))
        self.assertEqual([row["total_cost"] for row in result["objects"] if row["kind"] == "product"],
                         ["33.34", "33.34", "33.33"])

    def test_two_stage_cost_center_allocation_reconciles(self):
        objects = (
            CostObject(object_id="maintenance", name="Maintenance", kind="cost_center"),
            CostObject(object_id="line-a", name="Line A", kind="cost_center"),
            CostObject(object_id="line-b", name="Line B", kind="cost_center"),
            CostObject(object_id="pump", name="Pump", quantity=D("10"), quantity_unit="piece"),
            CostObject(object_id="valve", name="Valve", quantity=D("20"), quantity_unit="piece"),
        )
        pools = (
            CostPool(pool_id="maintenance-pool", name="Maintenance", owner_id="maintenance",
                     amount=D("100"), currency="CNY"),
            CostPool(pool_id="line-a-pool", name="Line A overhead", owner_id="line-a",
                     amount=D("50"), currency="CNY", include_incoming=True),
            CostPool(pool_id="line-b-pool", name="Line B overhead", owner_id="line-b",
                     amount=D("40"), currency="CNY", include_incoming=True),
        )
        drivers = (
            CostDriver(driver_id="maintenance-hours", name="Machine hours", unit="h",
                       values={"line-a": D("60"), "line-b": D("40")}),
            CostDriver(driver_id="line-a-hours", name="Line A hours", unit="h",
                       values={"pump": D("1"), "valve": D("1")}),
            CostDriver(driver_id="line-b-hours", name="Line B hours", unit="h",
                       values={"pump": D("1"), "valve": D("3")}),
        )
        rules = (
            AllocationRule(rule_id="stage-1", pool_id="maintenance-pool",
                           target_ids=("line-a", "line-b"), driver_id="maintenance-hours", stage=1),
            AllocationRule(rule_id="stage-2a", pool_id="line-a-pool",
                           target_ids=("pump", "valve"), driver_id="line-a-hours", stage=2),
            AllocationRule(rule_id="stage-2b", pool_id="line-b-pool",
                           target_ids=("pump", "valve"), driver_id="line-b-hours", stage=2),
        )
        result = CostingEngine().analyze(CostingInput(
            dataset_id="factory", plugin_id="manufacturing", period="2026-05",
            scenario="actual", currency="CNY", objects=objects, pools=pools,
            drivers=drivers, rules=rules))
        product_total = sum(D(row["total_cost"]) for row in result["objects"] if row["kind"] == "product")
        self.assertEqual(product_total, D("190"))
        self.assertEqual(D(result["totals"]["total_cost"]), D("190"))
        self.assertEqual(D(result["totals"]["unallocated_cost"]), D("0"))
        self.assertEqual(len(result["allocations"]), 6)
        self.assertEqual({row["stage"] for row in result["allocations"]}, {1, 2})

    def test_cycles_and_out_of_order_cost_centers_are_rejected(self):
        objects = (CostObject(object_id="a", name="A", kind="cost_center"),
                   CostObject(object_id="b", name="B", kind="cost_center"))
        pools = (CostPool(pool_id="pa", name="A", owner_id="a", amount=D("10"), currency="CNY"),
                 CostPool(pool_id="pb", name="B", owner_id="b", amount=D("10"), currency="CNY"))
        drivers = (CostDriver(driver_id="ab", name="ab", unit="x", values={"b": D("1")}),
                   CostDriver(driver_id="ba", name="ba", unit="x", values={"a": D("1")}))
        rules = (AllocationRule(rule_id="a-to-b", pool_id="pa", target_ids=("b",),
                                driver_id="ab", stage=1),
                 AllocationRule(rule_id="b-to-a", pool_id="pb", target_ids=("a",),
                                driver_id="ba", stage=2))
        with self.assertRaisesRegex(ValueError, "cyclic|stage"):
            CostingEngine().analyze(CostingInput(
                dataset_id="x", plugin_id="x", period="2026-05", scenario="actual",
                currency="CNY", objects=objects, pools=pools, drivers=drivers, rules=rules))

    def test_zero_driver_is_not_silently_allocated(self):
        objects = (CostObject(object_id="p", name="Product"),
                   CostObject(object_id="cc", name="Center", kind="cost_center"))
        pool = CostPool(pool_id="pool", name="Pool", owner_id="cc", amount=D("20"), currency="CNY")
        driver = CostDriver(driver_id="zero", name="Zero", unit="h", values={"p": D("0")})
        rule = AllocationRule(rule_id="rule", pool_id="pool", target_ids=("p",),
                              method="driver", driver_id="zero")
        with self.assertRaisesRegex(ValueError, "zero"):
            CostingEngine().analyze(CostingInput(
                dataset_id="x", plugin_id="x", period="2026-05", scenario="actual",
                currency="CNY", objects=objects, pools=(pool,), drivers=(driver,), rules=(rule,)))

    def test_unallocated_pool_is_visible_and_reconciled(self):
        objects = (CostObject(object_id="p", name="Product"),
                   CostObject(object_id="cc", name="Center", kind="cost_center"))
        pool = CostPool(pool_id="pool", name="Pool", owner_id="cc", amount=D("20"), currency="CNY")
        result = CostingEngine().analyze(CostingInput(
            dataset_id="x", plugin_id="x", period="2026-05", scenario="actual",
            currency="CNY", objects=objects, pools=(pool,)))
        self.assertEqual(D(result["totals"]["total_cost"]), D("20"))
        self.assertEqual(D(result["totals"]["unallocated_cost"]), D("20"))
        self.assertEqual(result["quality"][0]["code"], "unallocated_cost_pool")

    def test_currency_conversion_requires_effective_dated_source(self):
        objects = (CostObject(object_id="p", name="Product", currency="CNY"),)
        entry = CostEntry(entry_id="usd", object_id="p", element_id="material",
                          amount=D("10"), currency="USD", source_refs=(ref(),))
        with self.assertRaisesRegex(ValueError, "Missing traceable FX rate"):
            CostingEngine().analyze(CostingInput(
                dataset_id="x", plugin_id="x", period="2026-05", scenario="actual",
                currency="CNY", objects=objects, entries=(entry,)))
        fx = CurrencyRate(from_currency="USD", to_currency="CNY", rate=D("7.2"),
                          effective_date="2026-05-01", source_ref=ref())
        result = CostingEngine().analyze(CostingInput(
            dataset_id="x", plugin_id="x", period="2026-05", scenario="actual",
            currency="CNY", objects=objects, entries=(entry,), currency_rates=(fx,)))
        self.assertEqual(result["objects"][0]["total_cost"], "72.0")

    def test_benchmark_and_standard_variances_are_exposed(self):
        objects = (CostObject(object_id="p", name="Product", quantity=D("10"),
                              quantity_unit="piece", selling_price=D("20")),)
        entry = CostEntry(entry_id="material", object_id="p", element_id="steel",
                          amount=D("100"), currency="CNY", quantity=D("12"),
                          unit_rate=D("8.333333333333"), unit="kg",
                          source_refs=(ref(),))
        benchmark = CostBenchmark(object_id="p", scenario="budget", total_cost=D("90"),
                                  currency="CNY", source_refs=(ref(),))
        standard = StandardCost(object_id="p", element_id="steel",
                                quantity_per_output=D("1"), quantity_unit="kg",
                                standard_rate=D("8"), currency="CNY", source_refs=(ref(),))
        result = CostingEngine().analyze(CostingInput(
            dataset_id="x", plugin_id="x", period="2026-05", scenario="actual",
            currency="CNY", objects=objects, entries=(entry,),
            benchmarks=(benchmark,), standard_costs=(standard,)))
        self.assertEqual(result["benchmarks"][0]["variance"], "10")
        self.assertTrue(result["standard_variances"])
        self.assertEqual(result["objects"][0]["revenue"], "200")

    def test_missing_fx_and_units_are_never_silently_combined(self):
        from cost_core.costing import _decimal
        self.assertEqual(_decimal("0", "zero"), D("0"))
        self.assertIsNone(_decimal(None, "missing", optional=True))


if __name__ == "__main__":
    unittest.main()
