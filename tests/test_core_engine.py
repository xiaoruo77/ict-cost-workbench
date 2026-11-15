"""Independent industry-neutral contract and numerical regressions. Pure stdlib."""
from dataclasses import replace, FrozenInstanceError
from decimal import Decimal
from pathlib import Path
import ast
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from cost_core import (AnalysisInput, CostObservation, DatasetContext, SourceRef, analyze,
                       as_decimal, compare_values, format_number, pct_change,
                       weighted_observations)

D = Decimal


def context(count=2, unit="portion", dataset="dataset-a"):
    return DatasetContext(dataset_id=dataset, plugin_id="arbitrary-profile", currency="CNY",
                          quantity_unit=unit, self_org="self", peer_org="peer",
                          category_labels={f"c{index}": f"Category {index}" for index in range(count)})


def observation(ctx, *, period="2026-05", organization="self", quantity=D("10"),
                values=None, scenario="actual"):
    values = values if values is not None else {key: D(index + 1) for index, key in enumerate(ctx.category_labels)}
    unit = sum(values.values(), D(0)) if all(value is not None for value in values.values()) else None
    ref = SourceRef(dataset_id=ctx.dataset_id, source_uri="fixture://record", selector=period)
    return CostObservation(dataset_id=ctx.dataset_id, organization=organization, cost_object="object-1",
                           period=period, quantity=quantity, unit_costs=values,
                           reported_unit_cost=unit, reported_total=unit * quantity if unit is not None and quantity is not None else None,
                           currency=ctx.currency, quantity_unit=ctx.quantity_unit, scenario=scenario,
                           sources={"quantity": (ref,), "unit_cost": (ref,), "total_cost": (ref,),
                                    **{f"category.{key}.unit_cost": (ref,) for key in values}})


class CoreEngineTests(unittest.TestCase):
    def test_fact_identity_includes_organization_and_escapes_dynamic_segments(self):
        first_context = replace(context(), dataset_id="dataset/shared", plugin_id="profile/cost",
                                self_org="organization/main")
        second_context = replace(first_context, self_org="organization-other")
        first = observation(first_context, organization=first_context.self_org)
        second = observation(second_context, organization=second_context.self_org,
                             values={"c0": D(10), "c1": D(20)})
        first_result = analyze(AnalysisInput(context=first_context, cost_object=first.cost_object,
                                            period=first.period, current=first))
        second_result = analyze(AnalysisInput(context=second_context, cost_object=second.cost_object,
                                             period=second.period, current=second))
        first_metric, second_metric = first_result.metrics["unit_cost"], second_result.metrics["unit_cost"]
        self.assertNotEqual(first_metric.value, second_metric.value)
        self.assertNotEqual(first_metric.fact_id, second_metric.fact_id)
        self.assertEqual(first_metric.fact_id.split("/"),
                         ["dataset%2Fshared", "profile%2Fcost", "organization%2Fmain",
                          "object-1", "2026-05", "unit_cost"])
        escaped_context = replace(context(), category_labels={"a/b": "A", "a%2Fb": "B"})
        escaped = observation(escaped_context, values={"a/b": D(1), "a%2Fb": D(2)})
        escaped_result = analyze(AnalysisInput(context=escaped_context, cost_object=escaped.cost_object,
                                              period=escaped.period, current=escaped))
        self.assertTrue(escaped_result.metrics["category.a/b.unit_cost"].fact_id.endswith("category.a%2Fb.unit_cost"))
        self.assertNotEqual(escaped_result.metrics["category.a/b.unit_cost"].fact_id,
                            escaped_result.metrics["category.a%2Fb.unit_cost"].fact_id)

    def test_arbitrary_two_and_five_categories_and_units(self):
        for count, unit in ((2, "portion"), (5, "kg")):
            ctx = context(count, unit)
            current = observation(ctx)
            previous = observation(ctx, period="2026-04", values={key: value / 2 for key, value in current.unit_costs.items()})
            peer = observation(ctx, organization="peer", values={key: value * 2 for key, value in current.unit_costs.items()})
            year_ago = observation(ctx, period="2025-05", quantity=D(5))
            budget = observation(ctx, scenario="budget", quantity=D(8))
            result = analyze(AnalysisInput(context=ctx, cost_object=current.cost_object, period=current.period,
                                           current=current, previous=previous, year_ago=year_ago,
                                           budget=budget, peer=peer, history=(previous, current)))
            self.assertEqual(result.metrics["unit_cost"].value, sum(current.unit_costs.values()))
            self.assertEqual(result.metrics["unit_cost.mom_pct"].value, D(100))
            self.assertEqual(result.metrics["quantity.yoy_pct"].value, D(100))
            self.assertEqual(result.metrics["quantity.budget_pct"].value, D(25))
            self.assertEqual(result.metrics["unit_cost"].unit, f"CNY/{unit}")
            self.assertEqual(len(result.comparison), count + 1)
            self.assertEqual(result.comparison[0].direction, "peer_higher")
            self.assertEqual(sum(result.metrics[f"category.{key}.share_pct"].value for key in ctx.category_labels), D(100))
            self.assertEqual(sum(result.metrics[f"category.{key}.contribution_pct"].value for key in ctx.category_labels), D(100))
            self.assertTrue(result.metrics["unit_cost.mom_pct"].source_refs)
            with self.assertRaises(FrozenInstanceError):
                current.period = "2026-06"

    def test_missing_true_zero_and_zero_denominator_are_distinct(self):
        ctx = context()
        current = observation(ctx, values={"c0": D(0), "c1": None})
        previous = observation(ctx, period="2026-04", values={"c0": D(0), "c1": D(1)})
        result = analyze(AnalysisInput(context=ctx, cost_object=current.cost_object, period=current.period,
                                      current=current, previous=previous))
        self.assertEqual(result.metrics["category.c0.unit_cost"].value, D(0))
        self.assertIsNone(result.metrics["category.c1.unit_cost"].value)
        self.assertEqual(result.metrics["category.c0.mom_pct"].status, "undefined_zero_base")
        self.assertIsNone(result.metrics["category.c0.share_pct"].value)
        self.assertTrue(any(issue.code == "missing_values" for issue in result.quality))
        self.assertIsNone(compare_values(D(0), D(2))["rate"])
        self.assertEqual(compare_values(D(0), D(2))["direction"], "peer_higher")

    def test_decimal_precision_and_display_rounding(self):
        self.assertEqual(format_number("2.675"), "2.68")
        self.assertEqual(format_number("-2.675"), "-2.68")
        self.assertEqual(format_number(None), "—")
        self.assertIsNone(as_decimal("1,2"))
        self.assertIsNone(as_decimal("12%"))
        self.assertIsNone(as_decimal(True))
        self.assertIsNone(as_decimal("NaN"))
        self.assertEqual(as_decimal("1,234.50"), D("1234.50"))
        self.assertEqual(pct_change(D(11), D(3)), D(8) / D(3) * 100)
        self.assertNotEqual(pct_change(D(11), D(3)), D("266.67"))
        self.assertEqual(compare_values(D("10.50"), D("11.00"))["difference"], D("0.50"))

    def test_identity_schema_and_future_observations_are_rejected(self):
        ctx = context()
        current = observation(ctx)
        invalid = [replace(current, dataset_id="dataset-b"), replace(current, quantity_unit="kg"),
                   replace(current, currency="USD"), replace(current, organization="other"),
                   replace(current, unit_costs={"c0": D(1)}), replace(current, period="2026-13"),
                   replace(current, quantity=10.0), replace(current, quantity=D(-1)),
                   replace(current, sources={"unit_cost": (SourceRef(dataset_id="other", source_uri="fixture://other"),)})]
        for bad in invalid:
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                analyze(AnalysisInput(context=ctx, cost_object=current.cost_object, period=current.period, current=bad))
        with self.assertRaises(ValueError):
            analyze(AnalysisInput(context=ctx, cost_object=current.cost_object, period=current.period,
                                  current=current, history=(observation(ctx, period="2026-06"),)))

    def test_reported_values_are_preserved_and_quality_records_disagreement(self):
        ctx = context()
        current = replace(observation(ctx), reported_unit_cost=D(4), reported_total=D(50))
        result = analyze(AnalysisInput(context=ctx, cost_object=current.cost_object, period=current.period, current=current))
        self.assertEqual(result.metrics["unit_cost"].value, D(4))
        self.assertEqual(result.metrics["total_cost"].value, D(50))
        self.assertEqual({issue.code for issue in result.quality},
                         {"category_unit_total_mismatch", "reported_total_mismatch", "category_amount_total_mismatch"})

    def test_weighted_periods_use_each_organizations_own_quantity(self):
        ctx = context()
        months = ("2026-01", "2026-02", "2026-03")
        rows = [observation(ctx, period=period, quantity=D(qty), values={"c0": D(value), "c1": D(0)})
                for period, qty, value in zip(months, (1, 2, 7), (10, 20, 30))]
        aggregate = weighted_observations(rows, months, context=ctx)
        self.assertEqual(aggregate.quantity, D(10))
        self.assertEqual(aggregate.reported_unit_cost, D(26))
        self.assertEqual(aggregate.unit_costs["c1"], D(0))
        self.assertEqual(aggregate.input_periods, months)
        self.assertEqual(aggregate.source_kind, "synthetic")
        self.assertTrue(any(ref.formula for ref in aggregate.sources["unit_cost"]))
        aggregate_result = analyze(AnalysisInput(context=ctx, cost_object=aggregate.cost_object,
                                                period=aggregate.period, current=aggregate))
        self.assertEqual(aggregate_result.metrics["unit_cost"].status, "derived")
        self.assertTrue(aggregate_result.metrics["unit_cost"].formula)
        peer_rows = [replace(row, organization="peer", quantity=D(qty))
                     for row, qty in zip(rows, (7, 2, 1))]
        peer = weighted_observations(peer_rows, months)
        self.assertEqual(peer.reported_unit_cost, D(14))
        self.assertNotEqual(peer.reported_unit_cost, aggregate.reported_unit_cost)

    def test_aggregation_rejects_missing_duplicate_and_mixed_period_inputs(self):
        ctx = context()
        months = ("2026-01", "2026-02")
        one, two = observation(ctx, period=months[0]), observation(ctx, period=months[1])
        invalid = [([one], months), ([one, one], months), ([one, two], (months[0], months[0])),
                   ([one, replace(two, dataset_id="different")], months),
                   ([one, replace(two, organization="peer")], months),
                   ([one, replace(two, currency="USD")], months),
                   ([one, replace(two, quantity_unit="kg")], months),
                   ([one, replace(two, source_kind="synthetic", input_periods=())], months),
                   ([one, replace(two, quantity=None)], months)]
        for rows, expected in invalid:
            with self.subTest(rows=rows), self.assertRaises(ValueError):
                weighted_observations(rows, expected)

    def test_zero_quantity_and_missing_totals_are_not_fabricated(self):
        ctx = context()
        months = ("2026-01", "2026-02")
        rows = [observation(ctx, period=period, quantity=D(0)) for period in months]
        aggregate = weighted_observations(rows, months)
        self.assertEqual(aggregate.quantity, D(0))
        self.assertEqual(aggregate.reported_total, D(0))
        self.assertIsNone(aggregate.reported_unit_cost)
        self.assertTrue(all(value is None for value in aggregate.unit_costs.values()))
        rows = [observation(ctx, period=period) for period in months]
        rows[1] = replace(rows[1], reported_total=None)
        aggregate = weighted_observations(rows, months)
        self.assertIsNone(aggregate.reported_total)
        self.assertEqual(aggregate.reported_unit_cost, D(3))

    def test_mapping_inputs_are_detached_and_read_only(self):
        labels = {"a": "A", "b": "B"}
        ctx = replace(context(), category_labels=labels)
        values = {"a": D(1), "b": D(2)}
        refs = [SourceRef(dataset_id=ctx.dataset_id, source_uri="fixture://immutable")]
        sources = {"unit_cost": refs}
        current = replace(observation(ctx, values=values), sources=sources)
        labels["a"] = "CHANGED"
        values["a"] = D(999)
        refs.clear()
        sources.clear()
        self.assertEqual(ctx.category_labels["a"], "A")
        self.assertEqual(current.unit_costs["a"], D(1))
        self.assertEqual(len(current.sources["unit_cost"]), 1)
        with self.assertRaises(TypeError):
            current.unit_costs["a"] = D(7)
        with self.assertRaises(TypeError):
            ctx.category_labels["a"] = "changed"
        result = analyze(AnalysisInput(context=ctx, cost_object=current.cost_object,
                                       period=current.period, current=current))
        with self.assertRaises(TypeError):
            result.metrics["unit_cost"] = None

    def test_core_has_no_application_or_industry_imports(self):
        allowed_roots = set(sys.stdlib_module_names) | {"cost_core"}
        root = Path(__file__).resolve().parents[1] / "scripts" / "cost_core"
        for file in root.glob("*.py"):
            tree = ast.parse(file.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    self.assertTrue(all(item.name.split(".")[0] in allowed_roots for item in node.names), file.name)
                if isinstance(node, ast.ImportFrom) and not node.level:
                    self.assertIn((node.module or "").split(".")[0], allowed_roots, file.name)


if __name__ == "__main__":
    unittest.main()
