"""Deterministic analysis of normalized observations. No industry imports or I/O."""
from __future__ import annotations

from decimal import Decimal
import re
from typing import Mapping
from urllib.parse import quote

from .models import (AnalysisInput, AnalysisResult, ComparisonRow, CostObservation,
                     DatasetContext, Metric, QualityIssue, SourceRef)
from .numbers import compare_values, pct_change


def month_index(period: str) -> int:
    if not isinstance(period, str) or not re.fullmatch(r"[0-9]{4}-(?:0[1-9]|1[0-2])", period):
        raise ValueError("period must be a valid YYYY-MM")
    year, month = map(int, period.split("-"))
    if year < 1:
        raise ValueError("year must be positive")
    return year * 12 + month - 1


def _text(value, field):
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be a non-empty string")


def _number(value, field):
    if value is not None and (not isinstance(value, Decimal) or not value.is_finite()):
        raise ValueError(f"{field} must be a finite Decimal or None")


def validate_context(context: DatasetContext) -> None:
    if not isinstance(context, DatasetContext):
        raise ValueError("context must be DatasetContext")
    for name in ("dataset_id", "plugin_id", "currency", "quantity_unit", "self_org"):
        _text(getattr(context, name), name)
    if not isinstance(context.peer_org, str):
        raise ValueError("peer_org must be a string; empty means unavailable")
    if context.peer_org and context.peer_org == context.self_org:
        raise ValueError("self_org and peer_org must differ")
    if not isinstance(context.category_labels, Mapping):
        raise ValueError("category_labels must be a mapping")
    for key, label in context.category_labels.items():
        _text(key, "category key")
        _text(label, "category label")
        if "." in key:
            raise ValueError("category keys cannot contain metric path separators")


def validate_observation(observation: CostObservation, context: DatasetContext | None = None) -> None:
    if not isinstance(observation, CostObservation):
        raise ValueError("observation must be CostObservation")
    for name in ("dataset_id", "organization", "cost_object", "currency", "quantity_unit", "scenario", "source_kind"):
        _text(getattr(observation, name), name)
    month_index(observation.period)
    for name in ("quantity", "reported_unit_cost", "reported_total"):
        _number(getattr(observation, name), name)
    if observation.quantity is not None and observation.quantity < 0:
        raise ValueError("quantity cannot be negative")
    if not isinstance(observation.unit_costs, Mapping):
        raise ValueError("unit_costs must be a mapping")
    for key, value in observation.unit_costs.items():
        _text(key, "category key")
        if "." in key:
            raise ValueError("category keys cannot contain metric path separators")
        _number(value, f"unit_costs.{key}")
    if context is not None:
        for name in ("dataset_id", "currency", "quantity_unit"):
            if getattr(observation, name) != getattr(context, name):
                raise ValueError(f"observation {name} does not match dataset context")
        if set(observation.unit_costs) != set(context.category_labels):
            raise ValueError("observation categories do not match the declared schema")
    if not isinstance(observation.sources, Mapping):
        raise ValueError("sources must be a mapping")
    for key, refs in observation.sources.items():
        _text(key, "source metric key")
        if not isinstance(refs, tuple):
            raise ValueError("source references must be tuples")
        for ref in refs:
            if not isinstance(ref, SourceRef) or ref.dataset_id != observation.dataset_id:
                raise ValueError("source reference belongs to another dataset or has invalid schema")
            _text(ref.source_uri, "source_uri")
            for name in ("field", "selector", "kind", "formula"):
                if not isinstance(getattr(ref, name), str):
                    raise ValueError(f"source reference {name} must be a string")
    if not isinstance(observation.input_periods, tuple):
        raise ValueError("input_periods must be a tuple")
    if len(set(observation.input_periods)) != len(observation.input_periods):
        raise ValueError("input_periods contains duplicates")
    for period in observation.input_periods:
        if month_index(period) > month_index(observation.period):
            raise ValueError("synthetic observation contains future input periods")


def source_refs(observation: CostObservation | None, metric: str) -> tuple[SourceRef, ...]:
    if observation is None:
        return ()
    aliases = {"unit_cost": "reported_unit_cost", "total_cost": "reported_total"}
    candidates = (metric, aliases.get(metric, ""))
    if metric.startswith("category.") and metric.endswith(".unit_cost"):
        candidates += ("unit_costs." + metric[len("category."):-len(".unit_cost")],)
    refs = []
    for key in candidates:
        if key:
            for ref in observation.sources.get(key, ()):
                if ref not in refs:
                    refs.append(ref)
    return tuple(refs)


def _refs(*groups):
    return tuple(dict.fromkeys(ref for group in groups for ref in group))


def analyze(request: AnalysisInput) -> AnalysisResult:
    if not isinstance(request, AnalysisInput):
        raise ValueError("request must be AnalysisInput")
    context = request.context
    validate_context(context)
    _text(request.cost_object, "cost_object")
    month_index(request.period)
    if request.current is None:
        raise ValueError("current observation is required")
    observations = {"current": request.current, "previous": request.previous,
                    "year_ago": request.year_ago, "budget": request.budget, "peer": request.peer}
    for role, observation in observations.items():
        if observation is None:
            continue
        validate_observation(observation, context)
        if observation.cost_object != request.cost_object:
            raise ValueError(f"{role} belongs to another cost object")
        expected_org = context.peer_org if role == "peer" else context.self_org
        if observation.organization != expected_org:
            raise ValueError(f"{role} belongs to another organization")
        if role in ("current", "budget", "peer") and observation.period != request.period:
            raise ValueError(f"{role} must match analysis period")
        if role in ("previous", "year_ago") and month_index(observation.period) >= month_index(request.period):
            raise ValueError(f"{role} must precede analysis period")
        expected_scenario = "budget" if role == "budget" else "actual"
        if observation.scenario != expected_scenario:
            raise ValueError(f"{role} scenario must be {expected_scenario}")
    # Compare the real input windows. A legacy quarterly adapter may label a
    # previous-quarter aggregate with the preceding month for compatibility;
    # that alias is never evidence that the underlying periods are comparable.
    def input_window(observation):
        periods = observation.input_periods or (observation.period,)
        window = tuple(sorted(month_index(value) for value in periods))
        if window != tuple(range(window[0], window[-1] + 1)):
            raise ValueError("comparison input_periods must be a contiguous monthly window")
        return window

    current_window = input_window(request.current)
    if current_window[-1] != month_index(request.period):
        raise ValueError("current input_periods must end at the analysis period")
    for role in ("previous", "year_ago", "budget", "peer"):
        observation = observations[role]
        if observation is None:
            continue
        shift = len(current_window) if role == "previous" else 12 if role == "year_ago" else 0
        expected_window = tuple(value - shift for value in current_window)
        if input_window(observation) != expected_window:
            raise ValueError(f"{role} has an incompatible comparison input_periods window")
    seen = set()
    for observation in request.history:
        validate_observation(observation, context)
        if (observation.organization != context.self_org or observation.cost_object != request.cost_object
                or observation.scenario != "actual"):
            raise ValueError("history belongs to another object, organization or scenario")
        if month_index(observation.period) > month_index(request.period):
            raise ValueError("history contains observations after the analysis period")
        if observation.period in seen:
            raise ValueError("history contains duplicate periods")
        seen.add(observation.period)

    current = request.current
    metrics, quality = {}, []
    unit_cost_unit = f"{context.currency}/{context.quantity_unit}"
    fact_prefix = "/".join(quote(part, safe="") for part in
                           (context.dataset_id, context.plugin_id, context.self_org,
                            request.cost_object, request.period))

    def add(key, value, unit, refs=(), formula="", status="observed"):
        metrics[key] = Metric(value=value, unit=unit, fact_id=f"{fact_prefix}/{quote(key, safe='')}",
                              source_refs=refs, formula=formula,
                              status=("missing" if value is None and status == "observed" else status))

    base = {"quantity": ("quantity", context.quantity_unit),
            "unit_cost": ("reported_unit_cost", unit_cost_unit),
            "total_cost": ("reported_total", context.currency)}
    for key, (attribute, unit) in base.items():
        value = getattr(current, attribute)
        refs = source_refs(current, key)
        observed_formula = "; ".join(dict.fromkeys(ref.formula for ref in refs if ref.formula))
        origin_status = "derived" if current.source_kind != "raw" else "observed"
        add(key, value, unit, refs, observed_formula, origin_status if value is not None else "missing")
        for role, suffix in (("previous", "mom_pct"), ("year_ago", "yoy_pct"), ("budget", "budget_pct")):
            baseline = observations[role]
            prior = getattr(baseline, attribute) if baseline is not None else None
            status = "undefined_zero_base" if prior == 0 else "missing" if value is None or prior is None else "derived"
            add(f"{key}.{suffix}", pct_change(value, prior), "%",
                _refs(refs, source_refs(baseline, key)), f"({key}.current - {key}.{role}) / {key}.{role} * 100", status)

    # Cost structure uses the available category amounts, never overwrites reported totals.
    parts = list(current.unit_costs.values())
    component_total = sum(parts, Decimal(0)) if all(v is not None for v in parts) else None
    previous_parts = list(request.previous.unit_costs.values()) if request.previous else []
    previous_total = (sum(previous_parts, Decimal(0)) if request.previous is not None
                      and all(v is not None for v in previous_parts) else None)
    total_change = (component_total - previous_total if component_total is not None
                    and previous_total is not None else None)
    all_current_refs = _refs(*(source_refs(current, f"category.{key}.unit_cost") for key in context.category_labels))
    all_previous_refs = _refs(*(source_refs(request.previous, f"category.{key}.unit_cost") for key in context.category_labels))
    for category in context.category_labels:
        key = f"category.{category}.unit_cost"
        prefix = f"category.{category}"
        value = current.unit_costs[category]
        refs = source_refs(current, key)
        observed_formula = "; ".join(dict.fromkeys(ref.formula for ref in refs if ref.formula))
        add(key, value, unit_cost_unit, refs, observed_formula,
            "missing" if value is None else "derived" if current.source_kind != "raw" else "observed")
        share = value / component_total * 100 if value is not None and component_total not in (None, 0) else None
        add(prefix + ".share_pct", share, "%", _refs(refs, all_current_refs),
            f"{key} / sum(category.*.unit_cost) * 100",
            "derived" if share is not None else "undefined_zero_base" if component_total == 0 else "missing")
        for role, suffix in (("previous", "mom_pct"), ("budget", "budget_pct")):
            baseline = observations[role]
            prior = baseline.unit_costs[category] if baseline else None
            status = "undefined_zero_base" if prior == 0 else "missing" if value is None or prior is None else "derived"
            add(prefix + "." + suffix, pct_change(value, prior), "%",
                _refs(refs, source_refs(baseline, key)), f"({key}.current - {key}.{role}) / {key}.{role} * 100", status)
        prior = request.previous.unit_costs[category] if request.previous else None
        contribution = ((value - prior) / total_change * 100 if value is not None and prior is not None
                        and total_change not in (None, 0) else None)
        add(prefix + ".contribution_pct", contribution, "%", _refs(all_current_refs, all_previous_refs),
            f"({key}.current - {key}.previous) / sum(category changes) * 100",
            "derived" if contribution is not None else "undefined_zero_base" if total_change == 0 else "missing")

    for role, observation in observations.items():
        if observation is None:
            continue
        values = list(observation.unit_costs.values())
        if observation.reported_unit_cost is not None and all(v is not None for v in values):
            deviation = observation.reported_unit_cost - sum(values, Decimal(0))
            if deviation:
                quality.append(QualityIssue(code="category_unit_total_mismatch", message=f"{role}: reported unit cost differs from category sum",
                                            metric_id="unit_cost", deviation=deviation,
                                            source_refs=_refs(source_refs(observation, "unit_cost"),
                                                              *(source_refs(observation, f"category.{k}.unit_cost") for k in context.category_labels))))
        if observation.quantity is not None and observation.reported_unit_cost is not None and observation.reported_total is not None:
            deviation = observation.reported_total - observation.quantity * observation.reported_unit_cost
            if deviation:
                quality.append(QualityIssue(code="reported_total_mismatch", message=f"{role}: reported total differs from quantity times reported unit cost",
                                            metric_id="total_cost", deviation=deviation,
                                            source_refs=_refs(source_refs(observation, "quantity"), source_refs(observation, "unit_cost"), source_refs(observation, "total_cost"))))
        if observation.quantity is not None and observation.reported_total is not None and all(v is not None for v in values):
            deviation = observation.reported_total - observation.quantity * sum(values, Decimal(0))
            if deviation:
                quality.append(QualityIssue(code="category_amount_total_mismatch", message=f"{role}: reported total differs from quantity times category sum",
                                            metric_id="total_cost", deviation=deviation,
                                            source_refs=_refs(source_refs(observation, "quantity"), source_refs(observation, "total_cost"),
                                                              *(source_refs(observation, f"category.{k}.unit_cost") for k in context.category_labels))))
        if any(value is None for value in (observation.quantity, observation.reported_unit_cost, observation.reported_total)) or any(v is None for v in values):
            quality.append(QualityIssue(code="missing_values", message=f"{role}: some observed cost fields are missing"))

    comparison = []
    if request.peer is not None:
        for key in ("unit_cost", *(f"category.{category}.unit_cost" for category in context.category_labels)):
            own = current.reported_unit_cost if key == "unit_cost" else current.unit_costs[key[len("category."):-len(".unit_cost")]]
            peer = request.peer.reported_unit_cost if key == "unit_cost" else request.peer.unit_costs[key[len("category."):-len(".unit_cost")]]
            comparison.append(ComparisonRow(metric_id=key, unit=unit_cost_unit,
                                            source_refs=_refs(source_refs(current, key), source_refs(request.peer, key)),
                                            **compare_values(own, peer)))
    return AnalysisResult(context=context, cost_object=request.cost_object, period=request.period,
                          metrics=metrics, comparison=tuple(comparison),
                          history=tuple(sorted(request.history, key=lambda observation: observation.period)),
                          quality=tuple(quality))
