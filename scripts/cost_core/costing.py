"""Industry-neutral cost ledger, allocation engine and plugin service.

All arithmetic is deterministic Decimal arithmetic. Industry plugins only map
source data to CostingInput; allocation, validation, variance and reporting live
here and are shared by every industry.
"""
from __future__ import annotations

from dataclasses import dataclass, field, fields, is_dataclass
from decimal import Decimal, InvalidOperation, ROUND_DOWN, ROUND_FLOOR, localcontext
from datetime import date
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping, Optional, Protocol, Sequence
import re

from .models import SourceRef
from .object_budgets import ObjectBudgetPlan, membership_sha256, summarize as summarize_object_budgets
from .object_rollups import ObjectRollupPlan, summarize as summarize_object_rollups
from .category_checklist import CategoryChecklistPlan, summarize as summarize_category_checklist


D = Decimal
_VALID_COST_TYPES = {"direct", "indirect"}
_VALID_BEHAVIORS = {"fixed", "variable", "semi_variable", "unknown"}
_VALID_METHODS = {"driver", "equal", "direct", "quantity", "labor_hours",
                  "machine_hours", "area", "energy", "revenue", "orders"}


def _decimal(value: Any, field_name: str, *, optional: bool = False) -> Optional[Decimal]:
    if value is None and optional:
        return None
    if isinstance(value, bool):
        raise ValueError(f"{field_name} must be a finite decimal")
    try:
        result = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError(f"{field_name} must be a finite decimal") from exc
    if not result.is_finite():
        raise ValueError(f"{field_name} must be finite")
    return result


def _text(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be a non-empty string")
    return value.strip()


def _freeze_mapping(value: Mapping[str, Any]) -> Mapping[str, Any]:
    return MappingProxyType(dict(value))


@dataclass(frozen=True, kw_only=True)
class CostingPluginMetadata:
    plugin_id: str
    name: str
    version: str = "1"
    industry: str = "generic"
    capabilities: tuple[str, ...] = ()


@dataclass(frozen=True, kw_only=True)
class CostingDatasetDefinition:
    dataset_id: str
    plugin_id: str
    label: str
    version: str = "1"
    workspace_root: Optional[Path] = None


@dataclass(frozen=True, kw_only=True)
class CostObject:
    object_id: str
    name: str
    kind: str = "product"
    quantity: Optional[Decimal] = None
    quantity_unit: str = "unit"
    currency: str = "CNY"
    selling_price: Optional[Decimal] = None
    attributes: Mapping[str, Any] = field(default_factory=dict)
    source_refs: tuple[SourceRef, ...] = ()

    def __post_init__(self):
        object.__setattr__(self, "attributes", _freeze_mapping(self.attributes))


@dataclass(frozen=True, kw_only=True)
class CostEntry:
    entry_id: str
    object_id: str
    element_id: str
    amount: Decimal
    currency: str
    cost_type: str = "direct"
    behavior: str = "variable"
    quantity: Optional[Decimal] = None
    unit_rate: Optional[Decimal] = None
    unit: str = ""
    source_refs: tuple[SourceRef, ...] = ()
    variable_fraction: Optional[Decimal] = None


@dataclass(frozen=True, kw_only=True)
class CostPool:
    pool_id: str
    name: str
    owner_id: str
    amount: Decimal
    currency: str
    element_id: str = "overhead"
    behavior: str = "fixed"
    cost_type: str = "indirect"
    include_incoming: bool = False
    source_refs: tuple[SourceRef, ...] = ()
    variable_fraction: Optional[Decimal] = None


@dataclass(frozen=True, kw_only=True)
class CostDriver:
    driver_id: str
    name: str
    unit: str
    values: Mapping[str, Decimal]
    source_refs: tuple[SourceRef, ...] = ()

    def __post_init__(self):
        object.__setattr__(self, "values", _freeze_mapping(self.values))


@dataclass(frozen=True, kw_only=True)
class AllocationRule:
    rule_id: str
    pool_id: str
    target_ids: tuple[str, ...]
    method: str = "driver"
    driver_id: str = ""
    stage: int = 1
    rounding: int = 2
    allow_partial: bool = False
    description: str = ""


@dataclass(frozen=True, kw_only=True)
class CurrencyRate:
    from_currency: str
    to_currency: str
    rate: Decimal
    effective_date: str
    source_ref: SourceRef


@dataclass(frozen=True, kw_only=True)
class UnitConversion:
    from_unit: str
    to_unit: str
    factor: Decimal
    effective_date: str
    source_ref: SourceRef


@dataclass(frozen=True, kw_only=True)
class CostBenchmark:
    object_id: str
    scenario: str
    total_cost: Decimal
    currency: str
    quantity: Optional[Decimal] = None
    source_refs: tuple[SourceRef, ...] = ()


@dataclass(frozen=True, kw_only=True)
class StandardCost:
    object_id: str
    element_id: str
    quantity_per_output: Decimal
    quantity_unit: str
    standard_rate: Decimal
    currency: str
    source_refs: tuple[SourceRef, ...] = ()


@dataclass(frozen=True, kw_only=True)
class ForecastAssumption:
    quantity_growth_pct: Decimal = D("0")
    selling_price_growth_pct: Decimal = D("0")
    variable_cost_growth_pct: Decimal = D("0")
    fixed_cost_growth_pct: Decimal = D("0")
    source_refs: tuple[SourceRef, ...] = ()


@dataclass(frozen=True, kw_only=True)
class CostingInput:
    dataset_id: str
    plugin_id: str
    period: str
    scenario: str
    currency: str
    objects: tuple[CostObject, ...]
    entries: tuple[CostEntry, ...] = ()
    pools: tuple[CostPool, ...] = ()
    drivers: tuple[CostDriver, ...] = ()
    rules: tuple[AllocationRule, ...] = ()
    currency_rates: tuple[CurrencyRate, ...] = ()
    unit_conversions: tuple[UnitConversion, ...] = ()
    benchmarks: tuple[CostBenchmark, ...] = ()
    standard_costs: tuple[StandardCost, ...] = ()
    forecast: Optional[ForecastAssumption] = None
    reported_totals: Mapping[str, Optional[Decimal]] = field(default_factory=dict)
    reported_unit_costs: Mapping[str, Optional[Decimal]] = field(default_factory=dict)
    preflight_quality: tuple[Mapping[str, Any], ...] = ()
    method: str = "ledger_allocation"
    coverage: Optional[Mapping[str, Any]] = None
    recognition: Optional[Mapping[str, Any]] = None
    object_rollup_plan: Optional[ObjectRollupPlan] = None
    object_budget_plan: Optional[ObjectBudgetPlan] = None
    category_checklist_plan: Optional[CategoryChecklistPlan] = None
    business_plan: Optional[Mapping[str, Any]] = None

    def __post_init__(self):
        for name in ("objects", "entries", "pools", "drivers", "rules",
                     "currency_rates", "unit_conversions", "benchmarks",
                     "standard_costs", "preflight_quality"):
            object.__setattr__(self, name, tuple(getattr(self, name)))
        object.__setattr__(self, "reported_totals", _freeze_mapping(self.reported_totals))
        object.__setattr__(self, "reported_unit_costs", _freeze_mapping(self.reported_unit_costs))


class CostingPlugin(Protocol):
    metadata: CostingPluginMetadata
    def describe(self, dataset: CostingDatasetDefinition) -> Mapping[str, Any]: ...
    def load(self, dataset: CostingDatasetDefinition, period: str, scenario: str) -> CostingInput: ...


class CostingPluginRegistry:
    def __init__(self):
        self._plugins: dict[str, CostingPlugin] = {}

    def register(self, plugin: CostingPlugin) -> None:
        meta = plugin.metadata
        if not re.fullmatch(r"[a-z][a-z0-9_-]{0,63}", meta.plugin_id):
            raise ValueError("Invalid trusted costing plugin identifier")
        if meta.plugin_id in self._plugins:
            raise ValueError(f"Duplicate costing plugin: {meta.plugin_id}")
        self._plugins[meta.plugin_id] = plugin

    def get(self, plugin_id: str) -> CostingPlugin:
        try:
            return self._plugins[plugin_id]
        except KeyError:
            raise LookupError(f"Unknown costing plugin: {plugin_id}") from None

    def list(self) -> tuple[CostingPluginMetadata, ...]:
        return tuple(plugin.metadata for plugin in self._plugins.values())


def costing_to_primitive(value: Any) -> Any:
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, Path):
        return str(value)
    if is_dataclass(value):
        return {item.name: costing_to_primitive(getattr(value, item.name)) for item in fields(value)}
    if isinstance(value, Mapping):
        return {str(key): costing_to_primitive(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [costing_to_primitive(item) for item in value]
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    raise TypeError(f"Unsupported costing result value: {type(value).__name__}")


class CostingEngine:
    """Deterministic ledger engine with staged, driver-based allocation."""

    def _currency_rate(self, source: str, target: str, period: str,
                       rates: Sequence[CurrencyRate]) -> tuple[Decimal, tuple[SourceRef, ...]]:
        if source == target:
            return D("1"), ()
        eligible = [r for r in rates if r.effective_date[:7] <= period
                    and r.source_ref is not None]
        direct = [r for r in eligible if r.from_currency == source and r.to_currency == target]
        inverse = [r for r in eligible if r.from_currency == target and r.to_currency == source]
        if direct:
            chosen = max(direct, key=lambda r: r.effective_date)
            return _decimal(chosen.rate, "currency.rate"), (chosen.source_ref,)
        if inverse:
            chosen = max(inverse, key=lambda r: r.effective_date)
            return D("1") / _decimal(chosen.rate, "currency.rate"), (chosen.source_ref,)
        raise ValueError(f"Missing traceable FX rate {source}->{target} for {period}")

    def _convert_money(self, amount: Decimal, source: str, target: str,
                       period: str, rates: Sequence[CurrencyRate]) -> tuple[Decimal, tuple[SourceRef, ...]]:
        rate, refs = self._currency_rate(source, target, period, rates)
        if rate <= 0:
            raise ValueError("Currency rate must be positive")
        return amount * rate, refs

    def _convert_unit(self, quantity: Decimal, source: str, target: str,
                      period: str, conversions: Sequence[UnitConversion]) -> tuple[Decimal, tuple[SourceRef, ...]]:
        if source == target:
            return quantity, ()
        eligible = [c for c in conversions if c.effective_date[:7] <= period and c.source_ref is not None]
        direct = [c for c in eligible if c.from_unit == source and c.to_unit == target]
        inverse = [c for c in eligible if c.from_unit == target and c.to_unit == source]
        if direct:
            chosen = max(direct, key=lambda c: c.effective_date)
            factor = _decimal(chosen.factor, "unit.factor")
            if factor <= 0:
                raise ValueError("Unit conversion factor must be positive")
            return quantity * factor, (chosen.source_ref,)
        if inverse:
            chosen = max(inverse, key=lambda c: c.effective_date)
            factor = _decimal(chosen.factor, "unit.factor")
            if factor <= 0:
                raise ValueError("Unit conversion factor must be positive")
            return quantity / factor, (chosen.source_ref,)
        raise ValueError(f"Missing traceable unit conversion {source}->{target} for {period}")

    def analyze(self, request: CostingInput, cost_object: str = "") -> dict[str, Any]:
        """Allocate an immutable component ledger, then render one consistent scope.

        A component keeps its source cost element and behavior through every stage.
        Rounding is a constrained money-unit apportionment; sub-unit balances stay
        on their source object and are explicitly reported as unallocated.
        """
        if not isinstance(request, CostingInput):
            raise ValueError("request must be CostingInput")
        if not re.fullmatch(r"[0-9]{4}-(0[1-9]|1[0-2])", request.period) or request.period[:4] == "0000":
            raise ValueError("period must be YYYY-MM")
        for name in ("dataset_id", "plugin_id", "currency"):
            _text(getattr(request, name), name)
        if request.scenario not in {"actual", "budget", "standard", "forecast"}:
            raise ValueError("scenario must be actual, budget, standard or forecast")
        if not request.objects:
            raise ValueError("At least one cost object is required")
        self._validate_conversions(request)
        objects: dict[str, CostObject] = {}
        for obj in request.objects:
            for name in ("object_id", "name", "kind", "quantity_unit", "currency"):
                _text(getattr(obj, name), "object." + name)
            if obj.object_id in objects:
                raise ValueError(f"Duplicate cost object: {obj.object_id}")
            for name in ("quantity", "selling_price"):
                number = _decimal(getattr(obj, name), "object." + name, optional=True)
                if number is not None and number < 0:
                    raise ValueError(f"Object {name} cannot be negative")
            if "revenue_applicable" in obj.attributes and not isinstance(obj.attributes["revenue_applicable"], bool):
                raise ValueError("object.attributes.revenue_applicable must be boolean")
            objects[obj.object_id] = obj
        if cost_object and cost_object not in objects:
            raise LookupError(f"Unknown cost object: {cost_object}")

        drivers = {driver.driver_id: driver for driver in request.drivers}
        if len(drivers) != len(request.drivers):
            raise ValueError("Duplicate cost driver identifier")
        for driver in request.drivers:
            _text(driver.driver_id, "driver_id")
            for target, value in driver.values.items():
                _text(target, "driver target")
                if target not in objects:
                    raise ValueError(f"Cost driver references unknown object: {target}")
                if _decimal(value, f"driver.{driver.driver_id}.{target}") < 0:
                    raise ValueError("Cost driver values cannot be negative")

        # Credits and debits use identical classifications; an allocated-out
        # component can never be silently relabelled with a downstream pool name.
        ledger: dict[str, list[dict[str, Any]]] = {key: [] for key in objects}
        pending: dict[str, list[dict[str, Any]]] = {key: [] for key in objects}
        direct = {key: D(0) for key in objects}
        inbound = {key: D(0) for key in objects}
        outbound = {key: D(0) for key in objects}
        unallocated_by_object = {key: D(0) for key in objects}
        quality = [dict(item) for item in request.preflight_quality]
        audit: list[dict[str, Any]] = []
        entry_ids: set[str] = set()
        entry_money: dict[str, Decimal] = {}
        entry_refs: dict[str, tuple[SourceRef, ...]] = {}
        source_total = D(0)

        def component(item, origin_id: str, amount: Decimal, refs):
            if item.cost_type not in _VALID_COST_TYPES or item.behavior not in _VALID_BEHAVIORS:
                raise ValueError(f"Invalid cost classification on {origin_id}")
            _text(item.element_id, f"{origin_id}.element_id")
            fraction = _decimal(item.variable_fraction, f"{origin_id}.variable_fraction", optional=True)
            if fraction is not None and not D(0) <= fraction <= D(1):
                raise ValueError("variable_fraction must be between 0 and 1")
            if item.behavior in {"fixed", "variable"} and fraction is not None:
                expected = D(0) if item.behavior == "fixed" else D(1)
                if fraction != expected:
                    raise ValueError(f"variable_fraction conflicts with behavior on {origin_id}")
            if item.behavior == "fixed":
                fraction = D(0)
            elif item.behavior == "variable":
                fraction = D(1)
            return {"origin_id": origin_id, "element_id": item.element_id,
                    "cost_type": item.cost_type, "behavior": item.behavior,
                    "variable_fraction": fraction, "amount": amount,
                    "source_refs": tuple(dict.fromkeys(refs)), "allocation_path": ()}

        for entry in request.entries:
            _text(entry.entry_id, "entry_id")
            if entry.entry_id in entry_ids:
                raise ValueError(f"Duplicate cost entry: {entry.entry_id}")
            entry_ids.add(entry.entry_id)
            if entry.object_id not in objects:
                raise ValueError(f"Cost entry references unknown object: {entry.object_id}")
            amount = _decimal(entry.amount, f"entry.{entry.entry_id}.amount")
            quantity = _decimal(entry.quantity, f"entry.{entry.entry_id}.quantity", optional=True)
            rate = _decimal(entry.unit_rate, f"entry.{entry.entry_id}.unit_rate", optional=True)
            if quantity is not None and rate is not None:
                tolerance = max(D("0.00000001"), abs(amount) * D("1e-12"))
                if abs(quantity * rate - amount) > tolerance:
                    raise ValueError(f"Entry {entry.entry_id}: amount conflicts with quantity * unit_rate")
            converted, fx_refs = self._convert_money(amount, entry.currency, request.currency,
                                                     request.period, request.currency_rates)
            refs = tuple(dict.fromkeys(entry.source_refs + fx_refs))
            ledger[entry.object_id].append(component(entry, "entry:" + entry.entry_id, converted, refs))
            direct[entry.object_id] += converted
            source_total += converted
            entry_money[entry.entry_id] = converted
            entry_refs[entry.entry_id] = refs

        pools = {pool.pool_id: pool for pool in request.pools}
        if len(pools) != len(request.pools):
            raise ValueError("Duplicate cost pool identifier")
        pool_components = {}
        incoming_pool_by_owner = {}
        for pool in request.pools:
            _text(pool.pool_id, "pool_id")
            if pool.owner_id not in objects:
                raise ValueError(f"Cost pool owner is unknown: {pool.owner_id}")
            if not isinstance(pool.include_incoming, bool):
                raise ValueError("pool.include_incoming must be boolean")
            if pool.include_incoming:
                if pool.owner_id in incoming_pool_by_owner:
                    raise ValueError("Only one pool per cost center may absorb staged incoming allocations")
                incoming_pool_by_owner[pool.owner_id] = pool.pool_id
            converted, fx_refs = self._convert_money(_decimal(pool.amount, f"pool.{pool.pool_id}.amount"),
                pool.currency, request.currency, request.period, request.currency_rates)
            comp = component(pool, "pool:" + pool.pool_id, converted, pool.source_refs + fx_refs)
            pool_components[pool.pool_id] = comp
            ledger[pool.owner_id].append(comp)
            source_total += converted

        rules_by_pool = {rule.pool_id: rule for rule in request.rules}
        if len(rules_by_pool) != len(request.rules):
            raise ValueError("A cost pool may have only one allocation rule")
        if len({rule.rule_id for rule in request.rules}) != len(request.rules):
            raise ValueError("Duplicate allocation rule identifier")
        for rule in request.rules:
            _text(rule.rule_id, "rule_id")
            if rule.pool_id not in pools:
                raise ValueError(f"Allocation rule references unknown pool: {rule.pool_id}")
            if not rule.target_ids or len(set(rule.target_ids)) != len(rule.target_ids):
                raise ValueError(f"Allocation rule {rule.rule_id} must have unique, nonempty targets")
            if any(target not in objects for target in rule.target_ids):
                raise ValueError(f"Allocation rule {rule.rule_id} references unknown target")
            if rule.method not in _VALID_METHODS:
                raise ValueError(f"Unsupported allocation method: {rule.method}")
            if (not isinstance(rule.stage, int) or isinstance(rule.stage, bool) or rule.stage < 1
                    or not isinstance(rule.rounding, int) or isinstance(rule.rounding, bool)
                    or not 0 <= rule.rounding <= 8):
                raise ValueError("Allocation stage or rounding precision is invalid")
            if not isinstance(rule.allow_partial, bool):
                raise ValueError("allow_partial must be boolean")
            if rule.method == "direct" and len(rule.target_ids) != 1:
                raise ValueError("Direct allocation requires exactly one target")
            if rule.method not in {"direct", "equal"} and rule.driver_id not in drivers:
                raise ValueError(f"Allocation rule {rule.rule_id} requires a registered driver")
        owner_stage: dict[str, int] = {}
        for pool_id, rule in rules_by_pool.items():
            owner = pools[pool_id].owner_id
            owner_stage[owner] = min(owner_stage.get(owner, rule.stage), rule.stage)
        for rule in request.rules:
            for target in rule.target_ids:
                if target in owner_stage and owner_stage[target] <= rule.stage:
                    raise ValueError(f"Allocation chain is cyclic or out of stage order: {rule.rule_id}->{target}")

        allocations: list[dict[str, Any]] = []
        ordered_pools = sorted(request.pools, key=lambda pool: (
            rules_by_pool[pool.pool_id].stage if pool.pool_id in rules_by_pool else 10**9, pool.pool_id))
        for pool in ordered_pools:
            owner = pool.owner_id
            available_components = [pool_components[pool.pool_id]]
            if pool.include_incoming:
                available_components += pending[owner]
                pending[owner] = []
            available = sum((comp["amount"] for comp in available_components), D(0))
            rule = rules_by_pool.get(pool.pool_id)
            if rule is None:
                unallocated_by_object[owner] += available
                quality.append({"code": "unallocated_cost_pool", "object_id": owner,
                    "pool_id": pool.pool_id, "amount": str(available),
                    "message": "成本池没有分摊规则，金额保留在成本中心"})
                audit.append({"event": "pool_unallocated", "pool_id": pool.pool_id,
                              "owner_id": owner, "amount": str(available)})
                continue
            if rule.method in {"direct", "equal"}:
                weights = {target: D(1) for target in rule.target_ids}
                driver_refs = ()
            else:
                driver = drivers[rule.driver_id]
                missing = [target for target in rule.target_ids if target not in driver.values]
                if missing and not rule.allow_partial:
                    raise ValueError(f"Driver {driver.driver_id} is missing targets: {missing}")
                if missing:
                    quality.append({"code": "partial_driver_coverage", "pool_id": pool.pool_id,
                        "missing_targets": missing,
                        "message": "部分对象缺动因，已知正向动因重新归一分配；缺失对象不获分配"})
                weights = {target: _decimal(driver.values.get(target, D(0)), "driver.value")
                           for target in rule.target_ids}
                driver_refs = driver.source_refs
            weights = {target: value for target, value in weights.items() if value > 0}
            denominator = sum(weights.values(), D(0))
            if denominator <= 0:
                if not rule.allow_partial:
                    raise ValueError(f"Allocation driver total is zero for pool {pool.pool_id}")
                unallocated_by_object[owner] += available
                quality.append({"code": "zero_driver_total", "object_id": owner,
                    "pool_id": pool.pool_id, "amount": str(available),
                    "message": "成本动因合计为零，成本池未分配"})
                continue
            quantum = D(1).scaleb(-rule.rounding)
            matrix, residuals = self._allocate_components(available_components, weights, quantum)
            sent = D(0)
            for target in weights:
                event_id = f"{rule.rule_id}:{target}"
                lines = []
                source_refs = []
                for comp, shares in zip(available_components, matrix):
                    amount = shares[target]
                    if amount == 0:
                        continue
                    refs = tuple(dict.fromkeys(comp["source_refs"] + driver_refs))
                    path = comp["allocation_path"] + (event_id,)
                    allocated_component = {**comp, "amount": amount, "source_refs": refs,
                                           "allocation_path": path}
                    pending[target].append(allocated_component)
                    ledger[target].append(allocated_component)
                    ledger[owner].append({**comp, "amount": -amount})
                    lines.append(costing_to_primitive(allocated_component))
                    source_refs.extend(refs)
                amount = sum((D(line["amount"]) for line in lines), D(0))
                if not lines:
                    # A positive driver can receive zero after constrained
                    # rounding. Keep its decision inputs in the event, not in
                    # the ledger: no financial component was transferred.
                    source_refs.extend(driver_refs)
                    for comp in available_components:
                        source_refs.extend(comp["source_refs"])
                inbound[target] += amount
                outbound[owner] += amount
                sent += amount
                line = {
                    "event_id": event_id, "rule_id": rule.rule_id,
                    "pool_id": pool.pool_id, "pool_name": pool.name,
                    "from_object": owner, "to_object": target,
                    "element_id": pool.element_id, "method": rule.method,
                    "driver_id": rule.driver_id,
                    "driver_unit": drivers[rule.driver_id].unit if rule.driver_id in drivers else "",
                    "driver_value": str(weights[target]),
                    "share_pct": str(weights[target] / denominator * 100),
                    "amount": format(amount, "f"), "currency": request.currency,
                    "stage": rule.stage, "pool_available": format(available, "f"),
                    "formula": f"{available} * {weights[target]} / {denominator}; constrained rounding {quantum}",
                    "rounding_method": "largest_remainder_with_component_conservation",
                    "components": lines,
                    "source_refs": costing_to_primitive(tuple(dict.fromkeys(source_refs))),
                }
                allocations.append(line)
                audit.append({"event": "cost_allocated", **line})
            residual = sum(residuals, D(0))
            if any(value != 0 for value in residuals):
                unallocated_by_object[owner] += residual
                quality.append({"code": "allocation_precision_residual", "object_id": owner,
                    "pool_id": pool.pool_id, "amount": format(residual, "f"),
                    "message": "小于分摊精度的来源余额保留在原成本中心；没有将尾差转成负分摊"})
            if sent + residual != available:
                raise ArithmeticError("Component allocation does not conserve the source pool")

        # Incoming costs left on an intermediate center are visible even if no
        # include_incoming pool was configured for that center.
        for object_id, comps in pending.items():
            if objects[object_id].kind == "cost_center" or object_id in owner_stage:
                remainder = sum((comp["amount"] for comp in comps), D(0))
                if remainder or (request.coverage is not None and any(comp["amount"] != 0 for comp in comps)):
                    unallocated_by_object[object_id] += remainder
                    quality.append({"code": "unabsorbed_incoming_cost", "object_id": object_id,
                        "amount": str(remainder),
                        "message": "成本中心收到的上游分摊没有进入后续成本池，余额保留在该中心"})

        object_results = []
        for object_id, obj in objects.items():
            # Net same-source debits before deciding whether a missing behavior
            # split remains relevant; settled centers correctly contain zeros.
            components = self._net_components(ledger[object_id])
            total = sum((comp["amount"] for comp in components), D(0))
            elements: dict[str, Decimal] = {}
            types: dict[str, Decimal] = {}
            variable = fixed = mixed = D(0)
            unresolved = False
            for comp in components:
                elements[comp["element_id"]] = elements.get(comp["element_id"], D(0)) + comp["amount"]
                types[comp["cost_type"]] = types.get(comp["cost_type"], D(0)) + comp["amount"]
                if comp["variable_fraction"] is None:
                    mixed += comp["amount"]
                    unresolved = True
                else:
                    variable += comp["amount"] * comp["variable_fraction"]
                    fixed += comp["amount"] * (1 - comp["variable_fraction"])
            if unresolved:
                quality.append({"code": "unknown_cost_behavior_split", "object_id": object_id,
                    "amount": format(mixed, "f"),
                    "message": "混合或未知性态成本缺少明确变动比例，贡献、保本量及成本预测不作假定"})
            quantity = _decimal(obj.quantity, "object.quantity", optional=True)
            price = _decimal(obj.selling_price, "object.selling_price", optional=True)
            price_refs = ()
            if price is not None:
                price, price_refs = self._convert_money(price, obj.currency, request.currency,
                                                       request.period, request.currency_rates)
            applicable = obj.attributes.get("revenue_applicable", obj.kind != "cost_center")
            revenue = quantity * price if applicable and quantity is not None and price is not None else None
            revenue_status = "not_applicable" if not applicable else "complete" if revenue is not None else "missing"
            if revenue_status == "missing":
                quality.append({"code": "missing_revenue", "object_id": object_id,
                    "missing_fields": [name for name, val in (("quantity", quantity), ("selling_price", price)) if val is None],
                    "message": "收入资料不完整；收入和经营毛差保持未知，不将成本判定为亏损"})
            unit_cost = total / quantity if quantity is not None and quantity > 0 else None
            variable_unit = variable / quantity if not unresolved and quantity is not None and quantity > 0 else None
            breakeven = None
            if applicable and price is not None and variable_unit is not None and price > variable_unit:
                breakeven = fixed / (price - variable_unit)
            refs = tuple(dict.fromkeys((*obj.source_refs, *price_refs,
                *(ref for comp in components for ref in comp["source_refs"]))))
            object_results.append({
                "object_id": object_id, "name": obj.name, "kind": obj.kind,
                "quantity": _money(quantity), "quantity_unit": obj.quantity_unit,
                "currency": request.currency, "selling_price": _money(price),
                "direct_cost": _money(direct[object_id]), "allocated_in": _money(inbound[object_id]),
                "allocated_out": _money(outbound[object_id]), "total_cost": _money(total),
                "unit_cost": _money(unit_cost), "revenue": _money(revenue),
                "revenue_status": revenue_status,
                "variable_cost": None if unresolved else _money(variable),
                "fixed_cost": None if unresolved else _money(fixed),
                "known_variable_cost": _money(variable), "known_fixed_cost": _money(fixed),
                "mixed_cost": _money(mixed), "cost_behavior_status": "incomplete" if unresolved else "complete",
                "contribution": _money(revenue - variable) if revenue is not None and not unresolved else None,
                "operating_margin": _money(revenue - total) if revenue is not None else None,
                "break_even_quantity": _money(breakeven),
                "unallocated_cost": _money(unallocated_by_object[object_id]),
                "cost_structure": {key: _money(value) for key, value in sorted(elements.items()) if value != 0},
                "cost_type_structure": {key: _money(value) for key, value in sorted(types.items()) if value != 0},
                "cost_components": costing_to_primitive(components),
                "source_refs": costing_to_primitive(refs),
            })
        ledger_total = sum((D(row["total_cost"]) for row in object_results), D(0))
        if ledger_total != source_total:
            raise ArithmeticError("Cost ledger does not reconcile after allocation")
        by_id = {row["object_id"]: row for row in object_results}

        benchmark_rows = []
        benchmark_keys = set()
        for benchmark in request.benchmarks:
            key = (benchmark.object_id, benchmark.scenario)
            if key in benchmark_keys:
                raise ValueError(f"Duplicate benchmark for {key}")
            benchmark_keys.add(key)
            if benchmark.object_id not in objects:
                raise ValueError(f"Benchmark references unknown object: {benchmark.object_id}")
            benchmark_total, fx_refs = self._convert_money(_decimal(benchmark.total_cost, "benchmark.total_cost"),
                benchmark.currency, request.currency, request.period, request.currency_rates)
            actual_total = D(by_id[benchmark.object_id]["total_cost"])
            variance = actual_total - benchmark_total
            benchmark_rows.append({"object_id": benchmark.object_id, "scenario": benchmark.scenario,
                "actual_cost": _money(actual_total), "benchmark_cost": _money(benchmark_total),
                "variance": _money(variance),
                "variance_pct": _money(variance / benchmark_total * 100) if benchmark_total != 0 else None,
                "source_refs": costing_to_primitive(benchmark.source_refs + fx_refs)})
        standard_variances = self._standard_variances(request, objects, entry_money, entry_refs, quality)
        for mapping, metric in ((request.reported_totals, "total_cost"), (request.reported_unit_costs, "unit_cost")):
            for object_id, reported in mapping.items():
                if object_id not in objects:
                    raise ValueError(f"Reported value references unknown object: {object_id}")
                if reported is None:
                    continue
                converted, fx_refs = self._convert_money(_decimal(reported, "reported." + metric),
                    objects[object_id].currency, request.currency, request.period, request.currency_rates)
                calculated = by_id[object_id][metric]
                if calculated is not None and D(calculated) != converted:
                    label = "total" if metric == "total_cost" else "unit_cost"
                    quality.append({"code": f"reported_{label}_mismatch", "object_id": object_id,
                        "reported_" + label: _money(converted), "calculated_" + label: calculated,
                        "deviation": _money(D(calculated) - converted),
                        "source_refs": costing_to_primitive(fx_refs)})

        selected_ids = {cost_object} if cost_object else set(objects)
        selected = [row for row in object_results if row["object_id"] in selected_ids]
        scoped_allocations = [line for line in allocations
            if line["to_object"] in selected_ids or line["from_object"] in selected_ids]
        relevant_pools = {line["pool_id"] for line in scoped_allocations}
        for pool in request.pools:
            if pool.owner_id in selected_ids:
                relevant_pools.add(pool.pool_id)
        scoped_quality = [item for item in quality
            if (item.get("object_id") in selected_ids if item.get("object_id") else
                item.get("pool_id") in relevant_pools if item.get("pool_id") else True)]
        selected_events = {line["event_id"] for line in scoped_allocations}
        scoped_audit = [item for item in audit if item.get("event_id") in selected_events
                        or item.get("owner_id") in selected_ids]
        # Upstream lineage is separate from scoped allocation totals, so a
        # single-product report cannot accidentally sum unrelated product flows.
        lineage_events = {event for row in selected for comp in row["cost_components"]
                          for event in comp["allocation_path"]}
        lineage = [line for line in allocations if line["event_id"] in lineage_events
                   and line["event_id"] not in selected_events]
        totals = self._totals(selected, request.currency)
        forecast = self._forecast(selected, request.forecast) if request.forecast is not None else None
        result = {"dataset_id": request.dataset_id, "plugin_id": request.plugin_id,
            "period": request.period, "scenario": request.scenario,
            "currency": request.currency, "method": request.method,
            "scope": {"type": "object" if cost_object else "dataset", "object_ids": sorted(selected_ids)},
            "ledger_reconciled": True, "objects": selected, "allocations": scoped_allocations,
            "lineage_allocations": lineage, "quality": scoped_quality, "audit": scoped_audit,
            "benchmarks": [row for row in benchmark_rows if row["object_id"] in selected_ids],
            "standard_variances": [row for row in standard_variances if row["object_id"] in selected_ids],
            "forecast": forecast, "totals": totals}
        if request.coverage is not None:
            coverage = costing_to_primitive(request.coverage)
            coverage["allocated_or_retained_total"] = str(sum(D(x["total_cost"]) for x in object_results))
            coverage["unallocated_total"] = str(sum(unallocated_by_object.values(), D(0)))
            unresolved_codes={"unallocated_cost_pool","zero_driver_total","allocation_precision_residual","partial_driver_coverage","unabsorbed_incoming_cost"}
            unresolved=[{k:q[k] for k in ("code","object_id","pool_id","amount") if k in q}
                        for q in quality if q["code"] in unresolved_codes and
                        (q["code"] in {"allocation_precision_residual","partial_driver_coverage","unabsorbed_incoming_cost"} or D(q["amount"]) != 0)]
            if D(coverage["unallocated_total"]) != 0 or unresolved:
                coverage["issues"].append({"code":"coverage_unallocated_amount","amount":coverage["unallocated_total"],
                                           "unresolved":unresolved})
                coverage["status"] = "incomplete"
            result["coverage"] = coverage
        if request.recognition is not None:
            recognition = costing_to_primitive(request.recognition)
            if cost_object and "operational_inputs" in recognition:
                # The operating quantities and drivers here cover the complete
                # bundle. Each selected object still carries its own source refs.
                recognition["operational_inputs"] = {
                    "status": "requires_book_scope",
                    "reason": "full operational evidence requires all cost objects",
                }
            rollup_plan=request.object_rollup_plan
            if rollup_plan is not None and not cost_object:
                recognition["object_rollups"]=summarize_object_rollups(rollup_plan, object_results, ledger_total)
                for group in recognition["object_rollups"]["groups"]:
                    group["membership_sha256"] = membership_sha256(recognition["context"], group["id"], group["members"])
                if request.object_budget_plan is not None:
                    recognition["object_budgets"]=summarize_object_budgets(
                        request.object_budget_plan,recognition["object_rollups"])
                if request.category_checklist_plan is not None:
                    recognition["category_checklist"]=summarize_category_checklist(
                        request.category_checklist_plan, recognition["object_rollups"], object_results,
                        recognition["context"])
            unresolved = {"unallocated_cost_pool","partial_driver_coverage","zero_driver_total",
                          "allocation_precision_residual","unabsorbed_incoming_cost"}
            has_open_allocation = bool(any(
                q.get("code") in unresolved and
                (q["code"] != "unallocated_cost_pool" or D(q.get("amount", 0))!=0)
                for q in quality)) or any(x != 0 for x in unallocated_by_object.values())
            recognition["allocation_status"] = ("needs_review" if has_open_allocation else
                "declared_allocations_resolved" if request.pools else "not_required")
            result["recognition"] = recognition
        if request.business_plan is not None:
            from .business_views import evaluate_business_views
            plan = dict(request.business_plan)
            if cost_object:
                plan['classifications'] = {k:v for k,v in plan['classifications'].items() if k in selected_ids}
                for name in ('lifecycle','settlement'):
                    if name in plan:
                        plan[name] = dict(plan[name], records=[x for x in plan[name]['records'] if x['object_id'] in selected_ids])
                if 'settlement' in plan and 'expected_objects' in plan['settlement']:
                    plan['settlement']['expected_objects'] = [
                        oid for oid in plan['settlement']['expected_objects'] if oid in selected_ids]
                if plan.get('dispositions'):
                    controls = plan['dispositions']
                    records = [record for record in controls['records'] if record['object_id'] in selected_ids]
                    counts = {action: sum(record['action'] == action for record in records) for action in controls['counts']}
                    plan['dispositions'] = dict(controls, records=records, counts=counts, total_count=len(records))
                if 'lifecycle' in plan:
                    plan['lifecycle']['units'] = {k:v for k,v in plan['lifecycle']['units'].items() if k in selected_ids}
            result['business_views'] = evaluate_business_views(plan, result, self._apportion)
        result["report"] = _render_costing_report(result)
        return result

    @staticmethod
    def _validate_conversions(request):
        for values, label, left, right, value_name in (
            (request.currency_rates, "currency rate", "from_currency", "to_currency", "rate"),
            (request.unit_conversions, "unit conversion", "from_unit", "to_unit", "factor")):
            seen = set()
            for item in values:
                for key in (left, right):
                    _text(getattr(item, key), key)
                number = _decimal(getattr(item, value_name), value_name)
                if number <= 0:
                    raise ValueError(f"{label} must be positive")
                try:
                    if not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", item.effective_date):
                        raise ValueError()
                    date.fromisoformat(item.effective_date)
                except (TypeError, ValueError):
                    raise ValueError(f"{label} effective_date must be a valid YYYY-MM-DD") from None
                if not isinstance(item.source_ref, SourceRef) or not item.source_ref.source_uri:
                    raise ValueError(f"{label} requires a traceable source")
                key = (getattr(item, left), getattr(item, right), item.effective_date)
                if key in seen:
                    raise ValueError(f"Duplicate {label} for {key}")
                seen.add(key)

    @staticmethod
    def _apportion(amount: Decimal, weights: Mapping[str, Decimal], quantum: Decimal):
        """Largest remainder in integer units, stable on declared target order."""
        if amount < 0 or amount % quantum:
            raise ValueError("apportion amount must be nonnegative and money-unit aligned")
        if amount == 0:
            return {key: D(0) for key in weights}
        positive_targets = [key for key, value in weights.items() if value > 0]
        if len(positive_targets) == 1:
            return {key: amount if key == positive_targets[0] else D(0) for key in weights}
        denominator = sum(weights.values(), D(0))
        if denominator <= 0:
            raise ArithmeticError("No capacity for component allocation")
        units = int(amount / quantum)
        with localcontext() as ctx:
            ctx.prec = max(50, len(str(units)) + 30)
            exact = {key: D(units) * weight / denominator for key, weight in weights.items()}
            floor = {key: int(value.to_integral_value(rounding=ROUND_FLOOR)) for key, value in exact.items()}
            remaining = units - sum(floor.values())
            ranked = sorted(weights, key=lambda key: exact[key] - floor[key], reverse=True)
            for key in ranked[:remaining]:
                floor[key] += 1
        return {key: D(value) * quantum for key, value in floor.items()}

    def _allocate_components(self, components, weights, quantum):
        transferable = [comp["amount"] if comp["amount"] % quantum == 0 else
                        comp["amount"].quantize(quantum, rounding=ROUND_DOWN) for comp in components]
        residuals = [comp["amount"] - value for comp, value in zip(components, transferable)]
        positive = sum((value for value in transferable if value > 0), D(0))
        negative = -sum((value for value in transferable if value < 0), D(0))
        net = positive - negative
        # Credits/refunds can coexist with costs. Match gross quotas so each
        # target's net follows the pool's sign even at one-cent precision.
        if net >= 0:
            negative_quota = self._apportion(negative, weights, quantum)
            net_quota = self._apportion(net, weights, quantum)
            positive_quota = {key: negative_quota[key] + net_quota[key] for key in weights}
        else:
            positive_quota = self._apportion(positive, weights, quantum)
            net_quota = self._apportion(-net, weights, quantum)
            negative_quota = {key: positive_quota[key] + net_quota[key] for key in weights}
        matrix = []
        for value in transferable:
            capacities = positive_quota if value >= 0 else negative_quota
            shares = self._apportion(abs(value), capacities, quantum)
            for key in weights:
                capacities[key] -= shares[key]
                if capacities[key] < 0:
                    raise ArithmeticError("Component allocation exceeded a target quota")
            matrix.append({key: amount if value >= 0 else -amount for key, amount in shares.items()})
        return matrix, residuals

    @staticmethod
    def _net_components(components):
        grouped = {}
        for comp in components:
            key = (comp["origin_id"], comp["element_id"], comp["cost_type"], comp["behavior"], comp["variable_fraction"])
            if key not in grouped:
                grouped[key] = {**comp, "amount": D(0), "source_refs": (), "allocation_path": ()}
            row = grouped[key]
            row["amount"] += comp["amount"]
            row["source_refs"] = tuple(dict.fromkeys(row["source_refs"] + comp["source_refs"]))
            row["allocation_path"] = tuple(dict.fromkeys(row["allocation_path"] + comp["allocation_path"]))
        return [comp for comp in grouped.values() if comp["amount"] != 0]

    def _standard_variances(self, request, objects, entry_money, entry_refs, quality):
        entries_by_key = {}
        for entry in request.entries:
            entries_by_key.setdefault((entry.object_id, entry.element_id), []).append(entry)
        results, seen = [], set()
        for standard in request.standard_costs:
            key = (standard.object_id, standard.element_id)
            if key in seen:
                raise ValueError(f"Duplicate standard cost for {key}")
            seen.add(key)
            if standard.object_id not in objects:
                raise ValueError(f"Standard cost references unknown object: {standard.object_id}")
            per_output = _decimal(standard.quantity_per_output, "standard.quantity_per_output")
            rate = _decimal(standard.standard_rate, "standard.standard_rate")
            if per_output < 0 or rate < 0:
                raise ValueError("Standard usage and price cannot be negative")
            _text(standard.quantity_unit, "standard.quantity_unit")
            output = _decimal(objects[standard.object_id].quantity, "object.quantity", optional=True)
            actual_rows = entries_by_key.get(key, [])
            incomplete = (output is None or not actual_rows
                          or any(entry.quantity is None or not entry.unit for entry in actual_rows))
            if incomplete:
                row = {"object_id": key[0], "element_id": key[1], "status": "unavailable",
                    "actual_quantity": None, "standard_quantity": None, "price_variance": None,
                    "usage_variance": None, "total_variance": None, "currency": request.currency,
                    "quantity_unit": standard.quantity_unit,
                    "source_refs": costing_to_primitive(standard.source_refs)}
                quality.append({"code": "standard_variance_unavailable", "object_id": key[0], "element_id": key[1],
                    "message": "标准差异缺少完整产量、全部实际耗用量或耗用单位，未假定单位或将缺失量填零"})
                results.append(row)
                continue
            standard_rate, rate_refs = self._convert_money(rate, standard.currency, request.currency,
                                                            request.period, request.currency_rates)
            refs = list(standard.source_refs + rate_refs + objects[standard.object_id].source_refs)
            actual_quantity = actual_cost = D(0)
            normalized = []
            for entry in actual_rows:
                raw_quantity = _decimal(entry.quantity, "entry.quantity")
                unit = entry.unit
                factor, unit_refs = self._convert_unit(D(1), unit, standard.quantity_unit,
                                                       request.period, request.unit_conversions)
                quantity = raw_quantity * factor
                amount = entry_money[entry.entry_id]
                rate = None
                if entry.unit_rate is not None:
                    rate, fx_refs = self._convert_money(_decimal(entry.unit_rate, "entry.unit_rate"),
                        entry.currency, request.currency, request.period, request.currency_rates)
                    rate /= factor  # price and quantity transform inversely
                    refs.extend(fx_refs)
                elif quantity != 0:
                    rate = amount / quantity
                actual_quantity += quantity
                actual_cost += amount
                refs.extend((*entry_refs[entry.entry_id], *unit_refs))
                normalized.append({"entry_id": entry.entry_id, "quantity": _money(quantity),
                    "unit_rate": _money(rate), "amount": _money(amount),
                    "quantity_unit": standard.quantity_unit, "currency": request.currency})
            expected = output * per_output
            # One complete standard consumption per object/element, irrespective
            # of how many source purchase/usage rows carry the actual quantity.
            price_variance = actual_cost - actual_quantity * standard_rate
            usage_variance = (actual_quantity - expected) * standard_rate
            results.append({"object_id": key[0], "element_id": key[1], "status": "complete",
                "actual_quantity": _money(actual_quantity), "standard_quantity": _money(expected),
                "actual_cost": _money(actual_cost), "standard_rate": _money(standard_rate),
                "actual_unit_rate": _money(actual_cost / actual_quantity) if actual_quantity else None,
                "price_variance": _money(price_variance), "usage_variance": _money(usage_variance),
                "total_variance": _money(price_variance + usage_variance),
                "quantity_unit": standard.quantity_unit, "currency": request.currency,
                "actual_rows": normalized, "source_refs": costing_to_primitive(tuple(dict.fromkeys(refs)))})
        return results

    @staticmethod
    def _totals(rows, currency):
        total = sum((D(row["total_cost"]) for row in rows), D(0))
        known_revenue = sum((D(row["revenue"]) for row in rows if row["revenue"] is not None), D(0))
        applicable = [row for row in rows if row["revenue_status"] != "not_applicable"]
        missing = [row["object_id"] for row in applicable if row["revenue"] is None]
        known_count = len(applicable) - len(missing)
        revenue_status = ("not_applicable" if not applicable else "complete" if not missing
                          else "partial" if known_count else "missing")
        revenue = known_revenue if revenue_status == "complete" else None
        variable = sum((D(row["known_variable_cost"]) for row in rows), D(0))
        fixed = sum((D(row["known_fixed_cost"]) for row in rows), D(0))
        mixed = sum((D(row["mixed_cost"]) for row in rows), D(0))
        behavior_complete = all(row["cost_behavior_status"] == "complete" for row in rows)
        quantities, elements, types = {}, {}, {}
        for row in rows:
            if row["quantity"] is not None:
                unit = row["quantity_unit"]
                quantities[unit] = quantities.get(unit, D(0)) + D(row["quantity"])
            for key, value in row["cost_structure"].items():
                elements[key] = elements.get(key, D(0)) + D(value)
            for key, value in row["cost_type_structure"].items():
                types[key] = types.get(key, D(0)) + D(value)
        return {"total_cost": _money(total), "total_revenue": _money(revenue),
            "known_revenue": _money(known_revenue), "revenue_status": revenue_status,
            "revenue_coverage": {"known_objects": known_count, "total_objects": len(applicable), "missing_object_ids": missing},
            "total_variable_cost": _money(variable) if behavior_complete else None,
            "total_fixed_cost": _money(fixed) if behavior_complete else None,
            "known_variable_cost": _money(variable), "known_fixed_cost": _money(fixed), "mixed_cost": _money(mixed),
            "cost_behavior_status": "complete" if behavior_complete else "incomplete",
            "total_contribution": _money(revenue - variable) if revenue is not None and behavior_complete else None,
            "total_operating_margin": _money(revenue - total) if revenue is not None else None,
            "unallocated_cost": _money(sum((D(row["unallocated_cost"]) for row in rows), D(0))),
            "quantity_by_unit": {key: _money(value) for key, value in sorted(quantities.items())},
            "cost_structure": {key: _money(value) for key, value in sorted(elements.items()) if value != 0},
            "cost_type_structure": {key: _money(value) for key, value in sorted(types.items()) if value != 0},
            "currency": currency}

    @staticmethod
    def _forecast(rows, assumptions):
        values = {}
        for name in ("quantity_growth_pct", "selling_price_growth_pct", "variable_cost_growth_pct", "fixed_cost_growth_pct"):
            value = _decimal(getattr(assumptions, name), "forecast." + name)
            if value < -100:
                raise ValueError("Forecast percentage cannot reduce a quantity or cost below zero")
            values[name] = 1 + value / 100
        objects = []
        for row in rows:
            cost = None
            if row["cost_behavior_status"] == "complete":
                cost = (D(row["variable_cost"]) * values["quantity_growth_pct"] * values["variable_cost_growth_pct"]
                        + D(row["fixed_cost"]) * values["fixed_cost_growth_pct"])
            revenue = (D(row["revenue"]) * values["quantity_growth_pct"] * values["selling_price_growth_pct"]
                       if row["revenue"] is not None else None)
            objects.append({"object_id": row["object_id"], "forecast_cost": _money(cost),
                "forecast_revenue": _money(revenue),
                "forecast_margin": _money(revenue - cost) if revenue is not None and cost is not None else None})
        totals = CostingEngine._totals(rows, "")
        forecast_cost = (sum((D(row["forecast_cost"]) for row in objects), D(0))
                         if all(row["forecast_cost"] is not None for row in objects) else None)
        forecast_revenue = (sum((D(row["forecast_revenue"]) for row in objects if row["forecast_revenue"] is not None), D(0))
                            if totals["revenue_status"] == "complete" else None)
        return {"forecast_cost": _money(forecast_cost), "forecast_revenue": _money(forecast_revenue),
            "forecast_margin": _money(forecast_revenue - forecast_cost) if forecast_cost is not None and forecast_revenue is not None else None,
            "assumptions": costing_to_primitive(assumptions), "objects": objects,
            "source_refs": costing_to_primitive(assumptions.source_refs),
            "status": "scenario_estimate" if forecast_cost is not None and forecast_revenue is not None else "incomplete_inputs"}


def _money(value):
    return format(value, "f") if value is not None else None


def _render_costing_report(result):
    """The report is generated from the final scoped result, never a stale copy."""
    def cell(value):
        return "未知 / 不适用" if value is None else str(value).replace("|", "\\|").replace("\n", " ")
    totals, currency = result["totals"], result["currency"]
    lines = [f"# 成本分析报告：{cell(result['dataset_id'])}", "",
        f"- 期间：{result['period']}", f"- 情景：{result['scenario']}", f"- 币种：{currency}",
        f"- 核算方法：{cell(result['method'])}",
        f"- 统计范围：{'整个数据集' if result['scope']['type'] == 'dataset' else ' / '.join(result['scope']['object_ids'])}",
        "", "## 汇总", "", f"- 总成本：{cell(totals['total_cost'])} {currency}",
        f"- 总收入：{cell(totals['total_revenue'])} {currency}",
        f"- 已知收入小计：{cell(totals['known_revenue'])} {currency}",
        f"- 收入覆盖：{totals['revenue_coverage']['known_objects']} / {totals['revenue_coverage']['total_objects']} 个收入对象",
        f"- 经营毛差：{cell(totals['total_operating_margin'])} {currency}",
        f"- 未分配成本：{cell(totals['unallocated_cost'])} {currency}"]
    if totals["revenue_status"] in {"partial", "missing"}:
        lines += ["- 收入资料不完整，无法据此给出盈利或亏损结论。"]
    if totals["cost_behavior_status"] != "complete":
        lines += ["- 部分成本性态比例未知，贡献、保本量及相关预测保持未知。"]
    lines += ["", "## 成本对象", "",
        "| 对象 | 数量 | 单位成本 | 总成本 | 收入 | 经营毛差 |", "|---|---:|---:|---:|---:|---:|"]
    for row in result["objects"]:
        lines.append("| " + " | ".join(cell(value) for value in (
            row["name"], f"{cell(row['quantity'])} {row['quantity_unit']}", row["unit_cost"],
            row["total_cost"], row["revenue"], row["operating_margin"])) + " |")
    lines += ["", "## 成本构成与性态", "",
        "| 对象 | 分类 | 金额 | 已知变动成本 | 已知固定成本 | 待明确性态成本 |", "|---|---|---:|---:|---:|---:|"]
    for row in result["objects"]:
        for element, amount in row["cost_structure"].items():
            lines.append(f"| {cell(row['name'])} | {cell(element)} | {cell(amount)} | — | — | — |")
        lines.append(f"| {cell(row['name'])} | 性态小计 | {cell(row['total_cost'])} | {cell(row['known_variable_cost'])} | {cell(row['known_fixed_cost'])} | {cell(row['mixed_cost'])} |")
    lines += ["", "## 分摊过程", "", "| 阶段 | 来源对象 | 目标对象 | 成本池 | 金额 | 公式 |",
        "|---:|---|---|---|---:|---|"]
    for row in (*result["lineage_allocations"], *result["allocations"]):
        lines.append("| " + " | ".join(cell(row[key]) for key in ("stage", "from_object", "to_object", "pool_name", "amount", "formula")) + " |")
    if result["allocations"] or result["lineage_allocations"]:
        lines += ["", "## 分摊组成与上游追溯", "",
            "| 分摊事件 | 原始成本 | 类别 | 性态 | 金额 | 完整分摊链 |", "|---|---|---|---|---:|---|"]
        for row in (*result["lineage_allocations"], *result["allocations"]):
            for comp in row["components"]:
                lines.append("| " + " | ".join(cell(value) for value in (row["event_id"], comp["origin_id"],
                    comp["element_id"], comp["behavior"], comp["amount"], " → ".join(comp["allocation_path"]))) + " |")
    if result["benchmarks"]:
        lines += ["", "## 基准差异", "", "| 对象 | 基准情景 | 实际成本 | 基准成本 | 差异 | 差异率 % |", "|---|---|---:|---:|---:|---:|"]
        for row in result["benchmarks"]:
            lines.append("| " + " | ".join(cell(row[key]) for key in ("object_id", "scenario", "actual_cost", "benchmark_cost", "variance", "variance_pct")) + " |")
    if result["standard_variances"]:
        lines += ["", "## 标准耗用与价格差异", "", "| 对象 | 成本类别 | 实际数量 | 标准数量 | 价格差异 | 耗用差异 | 合计差异 |", "|---|---|---:|---:|---:|---:|---:|"]
        for row in result["standard_variances"]:
            lines.append("| " + " | ".join(cell(row[key]) for key in ("object_id", "element_id", "actual_quantity", "standard_quantity", "price_variance", "usage_variance", "total_variance")) + " |")
    if result["forecast"]:
        forecast = result["forecast"]
        lines += ["", "## 情景测算", "", f"- 预测成本：{cell(forecast['forecast_cost'])}",
            f"- 预测收入：{cell(forecast['forecast_revenue'])}", f"- 预测毛差：{cell(forecast['forecast_margin'])}",
            f"- 状态：{forecast['status']}", f"- 显式假设：{cell(forecast['assumptions'])}"]
    lines += ["", "## 质量与审计", "", f"- 质量提示：{len(result['quality'])} 项",
        f"- 分摊记录：{len(result['allocations'])} 条", f"- 审计事件：{len(result['audit'])} 条", "- 总账守恒：通过"]
    for issue in result["quality"]:
        lines.append(f"- {cell(issue.get('code'))} / {cell(issue.get('object_id', '全局'))}：{cell(issue.get('message', issue))}")
    sources, seen = [], set()
    for section in ("objects", "allocations", "lineage_allocations", "benchmarks", "standard_variances"):
        for row in result[section]:
            for ref in row.get("source_refs", []):
                key = tuple(sorted(ref.items()))
                if key not in seen:
                    seen.add(key)
                    sources.append({"source_id": f"S{len(sources) + 1}", **ref})
    for ref in (result.get("forecast") or {}).get("source_refs", []):
        key = tuple(sorted(ref.items()))
        if key not in seen:
            seen.add(key)
            sources.append({"source_id": f"S{len(sources) + 1}", **ref})
    lines += ["", "## 来源定位", "", "| 来源号 | 类型 | 文件或记录 | 字段 | 行 / 页 / 条件 | 公式 |", "|---|---|---|---|---|---|"]
    for ref in sources:
        lines.append("| " + " | ".join(cell(ref.get(key, "")) for key in ("source_id", "kind", "source_uri", "field", "selector", "formula")) + " |")
    if not sources:
        lines += ["来源定位未提供；上述结果不得视为已核验的原始数据结论。"]
    return {"markdown": "\n".join(lines), "source_index": sources}


class CostingService:
    def __init__(self, registry: CostingPluginRegistry,
                 datasets: tuple[CostingDatasetDefinition, ...]):
        self.registry = registry
        self._datasets: dict[str, CostingDatasetDefinition] = {}
        for dataset in datasets:
            if not re.fullmatch(r"[a-z0-9][a-z0-9._-]{0,63}", dataset.dataset_id):
                raise ValueError("Invalid costing dataset identifier")
            if dataset.dataset_id in self._datasets:
                raise ValueError(f"Duplicate costing dataset: {dataset.dataset_id}")
            registry.get(dataset.plugin_id)
            self._datasets[dataset.dataset_id] = dataset

    def datasets(self) -> tuple[dict[str, Any], ...]:
        result = []
        for dataset in self._datasets.values():
            plugin = self.registry.get(dataset.plugin_id)
            try:
                description = dict(plugin.describe(dataset))
            except (ValueError, OSError) as exc:
                description = {"availability": "unavailable", "periods": [], "scenarios": [],
                               "availability_reason": str(exc)}
            description.setdefault("dataset_id", dataset.dataset_id)
            description.setdefault("plugin_id", dataset.plugin_id)
            description.setdefault("label", dataset.label)
            description.setdefault("version", dataset.version)
            result.append(description)
        return tuple(result)

    def analyze(self, dataset_id: str, period: str, scenario: str = "actual",
                cost_object: str = "") -> dict[str, Any]:
        try:
            dataset = self._datasets[dataset_id]
        except KeyError:
            raise LookupError(f"Unknown costing dataset: {dataset_id}") from None
        plugin = self.registry.get(dataset.plugin_id)
        request = plugin.load(dataset, period, scenario)
        if request.dataset_id != dataset.dataset_id or request.plugin_id != dataset.plugin_id:
            raise ValueError("Costing plugin returned a different dataset or plugin scope")
        if request.period != period or request.scenario != scenario:
            raise ValueError("Costing plugin returned a different period or scenario")
        result = CostingEngine().analyze(request, cost_object=cost_object)
        return {"result": result}
