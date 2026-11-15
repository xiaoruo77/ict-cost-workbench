"""Quantity-weighted aggregation of complete, compatible monthly observations."""
from __future__ import annotations

from decimal import Decimal
from typing import Iterable

from .engine import month_index, source_refs, validate_context, validate_observation
from .models import CostObservation, DatasetContext, SourceRef


def weighted_observations(observations: Iterable[CostObservation], periods: Iterable[str], *,
                          period: str | None = None,
                          context: DatasetContext | None = None) -> CostObservation:
    rows, wanted = tuple(observations), tuple(periods)
    if not wanted or len(wanted) != len(set(wanted)):
        raise ValueError("expected periods must be non-empty and unique")
    for expected in wanted:
        month_index(expected)
    if not rows:
        raise ValueError("observations are empty; requested periods are missing")
    if context is not None:
        validate_context(context)
    first = rows[0]
    seen = set()
    for row in rows:
        validate_observation(row, context)
        if context is not None and row.organization not in (context.self_org, context.peer_org):
            raise ValueError("organization is outside the declared dataset context")
        if row.period in seen:
            raise ValueError("duplicate observation period")
        seen.add(row.period)
        for name in ("dataset_id", "organization", "cost_object", "currency", "quantity_unit", "scenario"):
            if getattr(row, name) != getattr(first, name):
                raise ValueError(f"cannot aggregate different {name}")
        if set(row.unit_costs) != set(first.unit_costs):
            raise ValueError("cannot aggregate different category schemas")
        if row.quantity is None:
            raise ValueError("quantity is missing; quantity weights are unavailable")
        if row.source_kind == "synthetic" and not row.input_periods:
            raise ValueError("synthetic observations need explicit input periods before aggregation")
        if row.input_periods and row.input_periods != (row.period,):
            raise ValueError("cannot aggregate already aggregated observations as monthly rows")
    if seen != set(wanted):
        raise ValueError(f"period coverage mismatch: missing={sorted(set(wanted) - seen)}, extra={sorted(seen - set(wanted))}")
    target = period if period is not None else max(wanted)
    if month_index(target) < max(month_index(value) for value in wanted):
        raise ValueError("aggregation label precedes an input period")
    quantity = sum((row.quantity for row in rows), Decimal(0))

    def weighted(values):
        values = tuple(values)
        if quantity == 0 or any(value is None for value in values):
            return None
        return sum((value * row.quantity for value, row in zip(values, rows)), Decimal(0)) / quantity

    total_values = [row.reported_total for row in rows]
    total = sum(total_values, Decimal(0)) if all(value is not None for value in total_values) else None
    unit_total = weighted(row.reported_unit_cost for row in rows)
    categories = {category: weighted(row.unit_costs[category] for row in rows)
                  for category in first.unit_costs}
    formulas = {"quantity": "sum(input.quantity)", "total_cost": "sum(input.reported_total)",
                "unit_cost": "sum(input.reported_unit_cost * input.quantity) / sum(input.quantity)"}
    formulas.update({f"category.{category}.unit_cost":
                     f"sum(input.category.{category}.unit_cost * input.quantity) / sum(input.quantity)"
                     for category in categories})
    refs = {}
    for key, formula in formulas.items():
        collected = tuple(dict.fromkeys(ref for row in rows for ref in source_refs(row, key)))
        if key != "quantity" and key != "total_cost":
            collected = tuple(dict.fromkeys((*collected, *(ref for row in rows for ref in source_refs(row, "quantity")))))
        refs[key] = (*collected, SourceRef(dataset_id=first.dataset_id,
                                         source_uri=f"derived:{first.cost_object}@{target}", field=key,
                                         selector=",".join(sorted(wanted)), kind="derived", formula=formula))
    return CostObservation(dataset_id=first.dataset_id, organization=first.organization,
                           cost_object=first.cost_object, period=target, quantity=quantity,
                           unit_costs=categories, reported_unit_cost=unit_total, reported_total=total,
                           currency=first.currency, quantity_unit=first.quantity_unit,
                           sources=refs, scenario=first.scenario, source_kind="synthetic",
                           input_periods=tuple(sorted(wanted)))
