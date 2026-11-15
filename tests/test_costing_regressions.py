"""Independent counterexamples from the ICT C01-C11/A12 defect audit."""
from dataclasses import replace
from decimal import Decimal as D
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
from urllib.parse import parse_qsl

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from cost_core.costing import (
    AllocationRule, CostBenchmark, CostDriver, CostEntry, CostObject, CostPool,
    CostingDatasetDefinition, CostingEngine, CostingInput, CostingPluginRegistry,
    CostingService, CurrencyRate, ForecastAssumption, StandardCost, UnitConversion,
)
from cost_core.csv_adapter import CsvCostingAdapter
from cost_core.engine import analyze
from cost_core.models import AnalysisInput, CostObservation, DatasetContext, SourceRef
from industry_plugins.costing_plugins import RestaurantPlugin, ManufacturingPlugin, PharmaCostingPlugin


def ref(label="source"):
    return SourceRef(dataset_id="audit", source_uri="fixture://" + label, field="amount", selector="row=2")


def request(objects, **kw):
    return CostingInput(dataset_id="audit", plugin_id="generic", period="2026-05",
                        scenario="actual", currency="CNY", objects=tuple(objects), **kw)


def product(name="p", **kw):
    return CostObject(object_id=name, name=name, quantity=D(10), **kw)


def entry(name="e", obj="p", amount="100", **kw):
    return CostEntry(entry_id=name, object_id=obj, element_id=kw.pop("element_id", "material"),
                     amount=D(amount), currency="CNY", source_refs=(ref(name),), **kw)


class AuditCounterexampleTests(unittest.TestCase):
    def setUp(self):
        self.engine = CostingEngine()

    def test_c01_unknown_revenue_never_becomes_zero_or_a_loss(self):
        result = self.engine.analyze(request((product(),), entries=(entry(),), forecast=ForecastAssumption()))
        self.assertIsNone(result["totals"]["total_revenue"])
        self.assertIsNone(result["totals"]["total_operating_margin"])
        self.assertIsNone(result["totals"]["total_contribution"])
        self.assertIsNone(result["forecast"]["forecast_revenue"])
        self.assertIsNone(result["forecast"]["forecast_margin"])
        self.assertEqual(result["totals"]["revenue_status"], "missing")
        self.assertIn("missing_revenue", {row["code"] for row in result["quality"]})
        self.assertNotIn("经营毛差：-100", result["report"]["markdown"])
        self.assertIn("无法据此给出盈利或亏损结论", result["report"]["markdown"])

    def test_c01_partial_and_true_zero_revenue_remain_distinguishable(self):
        objects = (product("known", selling_price=D(2)), product("missing"),
                   CostObject(object_id="center", name="Center", kind="cost_center"))
        result = self.engine.analyze(request(objects))
        totals = result["totals"]
        self.assertEqual(totals["revenue_status"], "partial")
        self.assertEqual(D(totals["known_revenue"]), D(20))
        self.assertEqual(totals["revenue_coverage"]["total_objects"], 2)
        self.assertIsNone(totals["total_operating_margin"])
        zero = self.engine.analyze(request((replace(product(selling_price=D(3)), quantity=D(0)),)))
        self.assertEqual(zero["totals"]["revenue_status"], "complete")
        self.assertEqual(D(zero["totals"]["total_revenue"]), D(0))

    def test_c02_break_even_uses_converted_selling_price(self):
        fx = CurrencyRate(from_currency="USD", to_currency="CNY", rate=D("7.2"),
                          effective_date="2026-05-01", source_ref=ref("fx"))
        result = self.engine.analyze(request((product(currency="USD", selling_price=D(10)),),
            entries=(entry("fixed", behavior="fixed"), entry("variable", amount="20")), currency_rates=(fx,)))
        row = result["objects"][0]
        self.assertEqual(D(row["revenue"]), D(720))
        self.assertEqual(D(row["break_even_quantity"]), D(100) / D(70))
        self.assertIn("fixture://fx", {source["source_uri"] for source in row["source_refs"]})

    def test_c03_quantity_and_unit_price_are_converted_together(self):
        conversion = UnitConversion(from_unit="tonne", to_unit="kg", factor=D(1000),
                                    effective_date="2026-05-01", source_ref=ref("unit"))
        std = StandardCost(object_id="p", element_id="material", quantity_per_output=D(100),
                           quantity_unit="kg", standard_rate=D(8), currency="CNY")
        result = self.engine.analyze(request((product(),), entries=(entry(amount="8000", quantity=D(1),
            unit_rate=D(8000), unit="tonne"),), standard_costs=(std,), unit_conversions=(conversion,)))
        variance = result["standard_variances"][0]
        self.assertEqual(D(variance["actual_quantity"]), D(1000))
        self.assertEqual(D(variance["actual_rows"][0]["unit_rate"]), D(8))
        self.assertEqual(D(variance["price_variance"]), D(0))
        self.assertEqual(D(variance["usage_variance"]), D(0))
        reverse = replace(std, quantity_per_output=D("0.1"), quantity_unit="tonne", standard_rate=D(8000))
        result = self.engine.analyze(request((product(),), entries=(entry(amount="8000", quantity=D(1000),
            unit_rate=D(8), unit="kg"),), standard_costs=(reverse,), unit_conversions=(conversion,)))
        self.assertEqual(D(result["standard_variances"][0]["total_variance"]), D(0))

    def test_c04_splitting_or_merging_actual_rows_cannot_duplicate_standard_usage(self):
        standard = StandardCost(object_id="p", element_id="material", quantity_per_output=D(2),
                                quantity_unit="kg", standard_rate=D(5), currency="CNY")
        whole = entry(quantity=D(20), unit_rate=D(5), unit="kg")
        splits = (replace(whole, entry_id="a", amount=D(50), quantity=D(10)),
                  replace(whole, entry_id="b", amount=D(50), quantity=D(10)))
        first = self.engine.analyze(request((product(),), entries=(whole,), standard_costs=(standard,)))
        second = self.engine.analyze(request((product(),), entries=splits, standard_costs=(standard,)))
        self.assertEqual(len(second["standard_variances"]), 1)
        self.assertEqual(first["standard_variances"][0]["total_variance"], second["standard_variances"][0]["total_variance"])
        self.assertEqual(D(second["standard_variances"][0]["total_variance"]), D(0))
        different_prices = (entry("a", amount="30", quantity=D(5), unit_rate=D(6), unit="kg"),
                            entry("b", amount="60", quantity=D(15), unit_rate=D(4), unit="kg"))
        result = self.engine.analyze(request((product(),), entries=different_prices, standard_costs=(standard,)))
        self.assertEqual(D(result["standard_variances"][0]["price_variance"]), D(-10))
        self.assertEqual(D(result["standard_variances"][0]["usage_variance"]), D(0))

    def test_incomplete_actual_usage_is_not_reported_as_complete_variance(self):
        std = StandardCost(object_id="p", element_id="material", quantity_per_output=D(2),
                           quantity_unit="kg", standard_rate=D(5), currency="CNY")
        result = self.engine.analyze(request((product(),), entries=(entry(),), standard_costs=(std,)))
        self.assertIsNone(result["standard_variances"][0]["total_variance"])
        self.assertEqual(result["standard_variances"][0]["status"], "unavailable")
        with self.assertRaisesRegex(ValueError, "Duplicate standard"):
            self.engine.analyze(request((product(),), standard_costs=(std, std)))
        with self.assertRaisesRegex(ValueError, "amount conflicts"):
            self.engine.analyze(request((product(),), entries=(entry(quantity=D(2), unit_rate=D(2)),)))

    def test_c05_settled_centers_clear_elements_and_cost_behavior_and_keep_upstream_sources(self):
        plugin = ManufacturingPlugin()
        dataset = CostingDatasetDefinition(dataset_id="factory", plugin_id="manufacturing", label="Factory")
        result = self.engine.analyze(plugin.load(dataset, "2026-05", "actual"))
        self.assertEqual(D(result["totals"]["total_cost"]), D(147600))
        for row in result["objects"]:
            if row["kind"] == "cost_center":
                self.assertEqual(D(row["total_cost"]), D(0))
                self.assertEqual(D(row["variable_cost"]), D(0))
                self.assertEqual(D(row["fixed_cost"]), D(0))
                self.assertEqual(row["cost_structure"], {})
            else:
                self.assertGreater(D(row["cost_structure"]["maintenance"]), D(0))
                self.assertTrue(any("maintenance" in ref["source_uri"] for ref in row["source_refs"]))
                self.assertTrue(any(len(comp["allocation_path"]) >= 2 for comp in row["cost_components"]))
                self.assertEqual(sum(D(amount) for amount in row["cost_structure"].values()), D(row["total_cost"]))
        self.assertEqual(len(result["allocations"]), 6)
        for allocation in result["allocations"]:
            self.assertEqual(sum((D(comp["amount"]) for comp in allocation["components"]), D(0)), D(allocation["amount"]))

    def test_c06_scoped_service_recomputes_every_summary_and_report(self):
        plugin = RestaurantPlugin()
        registry = CostingPluginRegistry()
        registry.register(plugin)
        service = CostingService(registry, (CostingDatasetDefinition(dataset_id="restaurant", plugin_id="restaurant", label="R"),))
        result = service.analyze("restaurant", "2026-05", cost_object="dish-rice")["result"]
        self.assertEqual([row["object_id"] for row in result["objects"]], ["dish-rice"])
        obj, totals = result["objects"][0], result["totals"]
        for total_name, object_name in (("total_cost", "total_cost"), ("total_revenue", "revenue"),
                                        ("total_operating_margin", "operating_margin"), ("total_contribution", "contribution")):
            self.assertEqual(D(totals[total_name]), D(obj[object_name]))
        self.assertEqual(totals["quantity_by_unit"], {"份": "1200"})
        self.assertEqual({line["to_object"] for line in result["allocations"]}, {"dish-rice"})
        self.assertEqual({line["object_id"] for line in result["benchmarks"]}, {"dish-rice"})
        self.assertNotIn("番茄牛肉面", result["report"]["markdown"])
        self.assertNotIn("菌菇汤", result["report"]["markdown"])
        self.assertIn(obj["total_cost"], result["report"]["markdown"])
        with self.assertRaises(LookupError):
            service.analyze("restaurant", "2026-05", cost_object="not-there")

    def test_scoped_forecast_standards_quality_and_lineage_are_consistent(self):
        objects = (product("a", selling_price=D(20)), product("b"))
        data = request(objects, entries=(entry("a", "a", "50"), entry("b", "b", "100")),
                       forecast=ForecastAssumption(quantity_growth_pct=D(10)))
        result = self.engine.analyze(data, cost_object="a")
        self.assertEqual(D(result["forecast"]["forecast_cost"]), D(55))
        self.assertEqual(D(result["forecast"]["forecast_revenue"]), D(220))
        self.assertEqual([row["object_id"] for row in result["forecast"]["objects"]], ["a"])
        self.assertNotIn("missing_revenue", {row["code"] for row in result["quality"]})
        plugin = ManufacturingPlugin()
        result = self.engine.analyze(plugin.load(CostingDatasetDefinition(dataset_id="f", plugin_id="manufacturing", label="F"), "2026-05", "actual"), cost_object="product-pump")
        self.assertTrue(result["lineage_allocations"])
        self.assertEqual({row["object_id"] for row in result["standard_variances"]}, {"product-pump"})
        self.assertEqual({line["to_object"] for line in result["allocations"]}, {"product-pump"})

    def test_c07_csv_incoming_pool_absorbs_upstream_cost(self):
        text = ("object_id,object_kind,quantity,quantity_unit,currency,record_type,cost_element,amount,pool_id,include_incoming\n"
                "maint,cost_center,,,CNY,pool,maint,100,pm,false\n"
                "line,cost_center,,,CNY,pool,line,50,pl,true\n"
                "product,product,10,piece,CNY,entry,material,20,,false\n")
        schema = {"period": "2026-05", "allocation_rules": [
            {"pool_id": "pm", "target_ids": ["line"], "method": "direct", "stage": 1},
            {"pool_id": "pl", "target_ids": ["product"], "method": "direct", "stage": 2}]}
        parsed = CsvCostingAdapter().from_text(text, schema)
        self.assertEqual([pool.include_incoming for pool in parsed.pools], [False, True])
        result = self.engine.analyze(parsed)
        rows = {row["object_id"]: row for row in result["objects"]}
        self.assertEqual(D(rows["product"]["total_cost"]), D(170))
        self.assertEqual(D(rows["line"]["total_cost"]), D(0))
        self.assertEqual(D(result["totals"]["unallocated_cost"]), D(0))
        without_incoming = replace(parsed, pools=(parsed.pools[0], replace(parsed.pools[1], include_incoming=False)))
        remainder = self.engine.analyze(without_incoming)
        self.assertEqual(D(remainder["totals"]["unallocated_cost"]), D(100))
        self.assertIn("unabsorbed_incoming_cost", {item["code"] for item in remainder["quality"]})

    def test_c08_invalid_grouped_numbers_and_nonfinite_values_are_rejected(self):
        for value in ("1,2,3", "1,23", "1_000", "NaN", "Infinity", "12%"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                CsvCostingAdapter().from_text(f'object_id,amount,cost_element,currency\np,"{value}",m,CNY\n', {"period": "2026-05"})
        parsed = CsvCostingAdapter().from_text('object_id,amount,cost_element,currency\np,"1,234.50",m,CNY\n', {"period": "2026-05"})
        self.assertEqual(parsed.entries[0].amount, D("1234.50"))

    def test_c09_conflicting_selling_price_and_reported_cost_are_rejected(self):
        text = "object_id,amount,cost_element,currency,quantity,selling_price\np,10,m,CNY,10,20\np,10,m,CNY,10,99\n"
        with self.assertRaisesRegex(ValueError, "conflicts"):
            CsvCostingAdapter().from_text(text, {"period": "2026-05"})
        text = "object_id,amount,cost_element,currency,reported_total\np,10,m,CNY,20\np,10,m,CNY,99\n"
        with self.assertRaisesRegex(ValueError, "conflicts"):
            CsvCostingAdapter().from_text(text, {"period": "2026-05"})

    def test_csv_source_selector_cannot_be_overridden_by_object_name(self):
        text = "object_id,amount,cost_element,currency\np&row=3,10,m,CNY\nother,20,m,CNY\n"
        parsed = CsvCostingAdapter().from_text(text, {"period": "2026-05"})
        selector = dict(parse_qsl(parsed.entries[0].source_refs[0].selector))
        self.assertEqual(selector["row"], "2")
        self.assertEqual(selector["object"], "p&row=3")

    def test_c10_positive_tiny_pool_never_allocates_a_negative_tail(self):
        targets = tuple(product(f"p{i}") for i in range(4))
        center = CostObject(object_id="center", name="Center", kind="cost_center")
        pool = CostPool(pool_id="pool", name="Pool", owner_id="center", amount=D("0.02"), currency="CNY")
        rule = AllocationRule(rule_id="r", pool_id="pool", target_ids=tuple(row.object_id for row in targets), method="equal")
        result = self.engine.analyze(request((*targets, center), pools=(pool,), rules=(rule,)))
        amounts = [D(row["amount"]) for row in result["allocations"]]
        self.assertEqual(amounts, [D(".01"), D(".01"), D(0), D(0)])
        self.assertEqual(sum(amounts), pool.amount)
        remainder = self.engine.analyze(request((*targets, center), pools=(replace(pool, amount=D("0.025")),), rules=(rule,)))
        self.assertEqual(D(remainder["totals"]["unallocated_cost"]), D("0.005"))
        self.assertEqual(sum(D(row["amount"]) for row in remainder["allocations"]), D("0.02"))
        self.assertEqual(D(remainder["totals"]["total_cost"]), D("0.025"))

    def test_allocation_bounds_and_conservation_for_positive_and_negative_pools(self):
        for count in range(2, 8):
            targets = tuple(product(f"p{i}") for i in range(count))
            center = CostObject(object_id="center", name="Center", kind="cost_center")
            driver = CostDriver(driver_id="weights", name="Weights", unit="x", values={row.object_id: D(i + 1) for i, row in enumerate(targets)})
            rule = AllocationRule(rule_id="r", pool_id="pool", target_ids=tuple(row.object_id for row in targets), driver_id="weights")
            for pennies in range(-5, 6):
                amount = D(pennies) / 100
                pool = CostPool(pool_id="pool", name="Pool", owner_id="center", amount=amount, currency="CNY")
                result = self.engine.analyze(request((*targets, center), pools=(pool,), rules=(rule,), drivers=(driver,)))
                actual = [D(row["amount"]) for row in result["allocations"]]
                with self.subTest(count=count, amount=amount):
                    self.assertEqual(sum(actual), amount)
                    self.assertTrue(all(value >= 0 for value in actual) if amount >= 0 else all(value <= 0 for value in actual))
                    self.assertEqual(D(result["totals"]["total_cost"]), amount)

    def test_c11_mixed_cost_requires_an_explicit_split(self):
        data = request((product(selling_price=D(20)),), entries=(entry(behavior="semi_variable"),), forecast=ForecastAssumption())
        result = self.engine.analyze(data)
        row = result["objects"][0]
        self.assertIsNone(row["variable_cost"])
        self.assertIsNone(row["fixed_cost"])
        self.assertEqual(D(row["mixed_cost"]), D(100))
        self.assertIsNone(row["contribution"])
        self.assertIsNone(row["break_even_quantity"])
        self.assertIsNone(result["forecast"]["forecast_cost"])
        fixed = replace(data, entries=(replace(data.entries[0], variable_fraction=D(".3")),))
        result = self.engine.analyze(fixed)
        self.assertEqual(D(result["objects"][0]["variable_cost"]), D(30))
        self.assertEqual(D(result["objects"][0]["fixed_cost"]), D(70))
        with self.assertRaisesRegex(ValueError, "between 0 and 1"):
            self.engine.analyze(replace(data, entries=(replace(data.entries[0], variable_fraction=D("1.1")),)))

    def test_csv_schema_failures_are_uniform_value_errors(self):
        base = {"period": "2026-05"}
        malformed = [None, [], {"columns": 123}, {"columns": {"amount": []}},
            {"allocation_rules": "bad"}, {"allocation_rules": [None]},
            {"allocation_rules": [{"pool_id": "p", "target_ids": "item"}]},
            {"allocation_rules": [{"pool_id": "p", "target_ids": ["item"], "method": []}]},
            {"allocation_rules": [{"pool_id": "p", "target_ids": ["item"], "weights": []}]},
            {"allocation_rules": [{"pool_id": "p", "target_ids": ["item"], "allow_partial": "perhaps"}]},
            {"currency_rates": [{}]}, {"currency_rates": 2}, {"unit_conversions": [3]},
            {"standard_costs": [{"object_id": "p"}]}, {"benchmarks": [False]},
            {"forecast": []}, {"pool_options": {"p": []}}]
        text = "object_id,amount,cost_element,currency\np,10,m,CNY\n"
        for invalid in malformed:
            schema = {**base, **invalid} if isinstance(invalid, dict) else invalid
            with self.subTest(schema=schema), self.assertRaises(ValueError):
                CsvCostingAdapter().from_text(text, schema)

    def test_csv_standard_variance_round_trip_uses_consumption_fields(self):
        text = "object_id,amount,cost_element,currency,quantity,entry_quantity,entry_unit,unit_rate\np,8000,m,CNY,10,1,tonne,8000\n"
        schema = {"period": "2026-05", "standard_costs": [{"object_id": "p", "element_id": "m", "quantity_per_output": 100,
                  "quantity_unit": "kg", "standard_rate": 8, "currency": "CNY"}],
                  "unit_conversions": [{"from_unit": "tonne", "to_unit": "kg", "factor": 1000, "effective_date": "2026-05-01"}],
                  "forecast": {"quantity_growth_pct": 10}}
        result = self.engine.analyze(CsvCostingAdapter().from_text(text, schema))
        self.assertEqual(D(result["standard_variances"][0]["total_variance"]), D(0))
        self.assertIsNone(result["forecast"]["forecast_revenue"])
        self.assertTrue(any(ref["source_uri"] == "upload://schema" for ref in result["standard_variances"][0]["source_refs"]))

    def test_sample_periods_and_all_scenarios_are_explicit_and_consistent(self):
        for plugin in (RestaurantPlugin(), ManufacturingPlugin()):
            dataset = CostingDatasetDefinition(dataset_id="sample", plugin_id=plugin.metadata.plugin_id, label="Sample")
            metadata = plugin.describe(dataset)
            self.assertEqual(metadata["data_kind"], "synthetic")
            costs = []
            for period in metadata["periods"]:
                for scenario in metadata["scenarios"]:
                    result = self.engine.analyze(plugin.load(dataset, period, scenario))
                    self.assertTrue(result["ledger_reconciled"])
                    self.assertIn("synthetic_sample", {issue["code"] for issue in result["quality"]})
                    if scenario == "actual":
                        costs.append(D(result["totals"]["total_cost"]))
            self.assertNotEqual(costs[0], costs[1])

    def test_pharma_metadata_comes_from_available_rows_and_never_fabricates_behavior(self):
        from industry_plugins.pharma.adapter import default_context
        dataset = CostingDatasetDefinition(dataset_id="pharma-test", plugin_id="pharma-costing", label="P")
        context = default_context(dataset.dataset_id)
        rows = tuple(CostObservation(dataset_id=dataset.dataset_id, organization=context.self_org,
            cost_object="fixture-product", period=period, quantity=D(10),
            unit_costs={"materials": D(2), "labor": D(1), "manufacturing_overhead": D(3)},
            reported_unit_cost=D(6), reported_total=D(60), currency="CNY", quantity_unit=context.quantity_unit)
            for period in ("2025-01", "2026-05"))
        with tempfile.TemporaryDirectory() as directory:
            plugin = PharmaCostingPlugin(Path(directory))
            with patch("industry_plugins.pharma.adapter.PharmaAdapter.load_observations", return_value=rows):
                metadata = plugin.describe(dataset)
                self.assertEqual(metadata["periods"], ["2025-01", "2026-05"])
                loaded = plugin.load(dataset, "2026-05", "actual")
                overhead = next(item for item in loaded.entries if item.element_id == "manufacturing_overhead")
                self.assertEqual(overhead.cost_type, "indirect")
                self.assertEqual(overhead.behavior, "unknown")
                result = self.engine.analyze(loaded)
                self.assertIsNone(result["totals"]["total_revenue"])
                self.assertIsNone(result["objects"][0]["variable_cost"])
            with patch("industry_plugins.pharma.adapter.PharmaAdapter.load_observations", side_effect=FileNotFoundError()):
                self.assertEqual(plugin.describe(dataset)["periods"], [])
                self.assertEqual(plugin.describe(dataset)["availability"], "unavailable")


class BaselinePeriodRegressionTests(unittest.TestCase):
    def setUp(self):
        self.context = DatasetContext(dataset_id="audit", plugin_id="generic", currency="CNY", quantity_unit="piece",
                                      self_org="own", peer_org="", category_labels={"material": "Material"})
        self.current = CostObservation(dataset_id="audit", organization="own", cost_object="p", period="2026-05",
            quantity=D(1), unit_costs={"material": D(2)}, reported_unit_cost=D(2), reported_total=D(2),
            currency="CNY", quantity_unit="piece")

    def test_a12_arbitrary_earlier_month_cannot_be_called_month_on_month(self):
        for role, wrong in (("previous", "2025-01"), ("year_ago", "2025-04")):
            with self.subTest(role=role), self.assertRaisesRegex(ValueError, "comparison input_periods"):
                analyze(AnalysisInput(context=self.context, cost_object="p", period=self.current.period,
                                      current=self.current, **{role: replace(self.current, period=wrong)}))
        analyze(AnalysisInput(context=self.context, cost_object="p", period=self.current.period,
                              current=self.current, previous=replace(self.current, period="2026-04"),
                              year_ago=replace(self.current, period="2025-05")))

    def test_quarter_alias_uses_real_input_window_and_rejects_mismatched_windows(self):
        current = replace(self.current, period="2026-06", source_kind="synthetic", input_periods=("2026-04", "2026-05", "2026-06"))
        previous = replace(current, period="2026-05", input_periods=("2026-01", "2026-02", "2026-03"))
        year_ago = replace(current, period="2025-06", input_periods=("2025-04", "2025-05", "2025-06"))
        analyze(AnalysisInput(context=self.context, cost_object="p", period=current.period, current=current,
                              previous=previous, year_ago=year_ago))
        for bad in (replace(previous, input_periods=("2026-02", "2026-03", "2026-04")),
                    replace(previous, input_periods=("2026-01", "2026-03")),
                    replace(previous, input_periods=("2026-03",))):
            with self.subTest(periods=bad.input_periods), self.assertRaises(ValueError):
                analyze(AnalysisInput(context=self.context, cost_object="p", period=current.period,
                                      current=current, previous=bad))


if __name__ == "__main__":
    unittest.main()
