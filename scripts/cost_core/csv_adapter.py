"""Declarative CSV adapter with strict, reusable schema validation.

No uploaded schema executes code. Row quantities describe the output object;
entry_quantity / entry_unit / unit_rate describe a cost entry's consumption.
"""
from __future__ import annotations

import csv
from dataclasses import replace
import io
from decimal import Decimal
from typing import Any, Mapping
from urllib.parse import urlencode

from .costing import (AllocationRule, CostBenchmark, CostDriver, CostEntry,
                      CostObject, CostPool, CostingInput, CurrencyRate,
                      ForecastAssumption, StandardCost, UnitConversion)
from .models import SourceRef
from .numbers import as_decimal

_MISSING = {"", "—", "-", "–", "N/A", "n/a", "null", "None"}
_COLUMNS = (
    "object_id", "object_name", "object_kind", "quantity", "quantity_unit",
    "currency", "selling_price", "record_type", "cost_element", "amount",
    "cost_type", "behavior", "pool_id", "pool_name", "pool_owner_id", "period",
    "include_incoming", "variable_fraction", "entry_quantity", "entry_unit",
    "unit_rate", "reported_total", "reported_unit_cost",
)


def _number(value: Any, field: str, optional: bool = False):
    if value is None or isinstance(value, str) and value.strip() in _MISSING:
        if optional:
            return None
        raise ValueError(f"{field} is required")
    number = as_decimal(value)
    if number is None:
        raise ValueError(f"{field} must be a finite number with valid thousands grouping")
    if len(number.as_tuple().digits) > 50 or abs(number.adjusted()) > 24:
        raise ValueError(f"{field} exceeds supported decimal precision or magnitude")
    return number


def _boolean(value, field, default=False):
    if value is None or value == "":
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, str) and value.strip().lower() in {"true", "false", "1", "0"}:
        return value.strip().lower() in {"true", "1"}
    if type(value) is int and value in {0, 1}:
        return bool(value)
    raise ValueError(f"{field} must be true or false")


def _integer(value, field, default):
    if value is None:
        return default
    number = _number(value, field)
    if number != number.to_integral_value():
        raise ValueError(f"{field} must be an integer")
    return int(number)


def _mapping(value, field, optional=False):
    if value is None and optional:
        return {}
    if not isinstance(value, Mapping):
        raise ValueError(f"{field} must be an object")
    return value


def _sequence(value, field, optional=False):
    if value is None and optional:
        return ()
    if not isinstance(value, (list, tuple)):
        raise ValueError(f"{field} must be an array")
    return value


def _required_text(item, field, location):
    value = item.get(field)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{location}.{field} must be a nonempty string")
    return value.strip()


def _factor_weights(rule, where):
    """Allow a bounded, non-executable product of each target's named factors."""
    spec = _mapping(rule.get("weight_factors"), where + ".weight_factors")
    if set(spec) != {"columns", "values"}:
        raise ValueError(where + ".weight_factors requires columns and values")
    cols = _sequence(spec["columns"], where + ".weight_factors.columns")
    if (not 1 <= len(cols) <= 4 or any(not isinstance(c, str) or not c.strip() for c in cols)
            or len(set(cols)) != len(cols)):
        raise ValueError(where + ".weight_factors columns must be 1-4 unique names")
    rows = _mapping(spec["values"], where + ".weight_factors.values")
    if set(rows) != set(rule["target_ids"]):
        raise ValueError(where + ".weight_factors values must match allocation targets")
    weights = {}
    from decimal import Decimal, localcontext
    for target, raw in rows.items():
        row = _mapping(raw, where + ".weight_factors." + target)
        if set(row) != set(cols):
            raise ValueError(where + ".weight_factors missing/unknown column for " + target)
        with localcontext() as ctx:
            ctx.prec = 200
            total = Decimal(1)
            for col in cols:
                value = _number(row[col], where + ".weight_factors." + col)
                if value < 0:
                    raise ValueError(where + ".weight_factors disallows negative factors")
                total *= value
            weights[target] = _number(total, where + ".weight_factors computed weight")
    if not any(weights.values()):
        raise ValueError(where + ".weight_factors needs a positive target weight")
    return weights


class CsvCostingAdapter:
    def from_files(self, files: Mapping[str, str], schema: Mapping[str, Any], *,
                   source_uris=None, schema_uri: str = "upload://schema") -> CostingInput:
        """Compile named original files without merging away their evidence coordinates."""
        from .source_bundle import compile_bundle
        return compile_bundle(self, files, schema, source_uris=source_uris, schema_uri=schema_uri)

    @staticmethod
    def validate_schema(schema: Mapping[str, Any]) -> None:
        schema = _mapping(schema, "schema")
        columns = _mapping(schema.get("columns"), "schema.columns", optional=True)
        for key, value in columns.items():
            if not isinstance(key, str) or not isinstance(value, str) or not value.strip():
                raise ValueError("schema.columns keys and mapped headers must be nonempty strings")
            if key not in _COLUMNS:
                raise ValueError(f"Unknown column mapping: {key}")
        identity = _sequence(schema.get("row_identity"), "schema.row_identity", optional=True)
        if (len(identity) > 8 or any(not isinstance(k, str) or not k.strip() for k in identity)
                or len(set(identity)) != len(identity)):
            raise ValueError("schema.row_identity must contain up to 8 unique CSV headers")
        if "raw_composite" in schema:
            from .composite_ingest import validate_composite
            validate_composite(CsvCostingAdapter,schema)
        if "raw_meter" in schema:
            from .meter_ingest import validate_meter
            validate_meter(schema)
        if "raw_inventory" in schema:
            from .inventory_ingest import validate_inventory
            validate_inventory(schema)
        if "raw_recognition" in schema:
            from .recognition_ingest import validate_option
            validate_option(schema["raw_recognition"])
        for field in ("dataset_id", "plugin_id", "period", "scenario", "currency"):
            if field in schema and (not isinstance(schema[field], str) or not schema[field].strip()):
                raise ValueError(f"schema.{field} must be a nonempty string")
        collections = {
            "allocation_rules": ("pool_id",),
            "currency_rates": ("from_currency", "to_currency", "effective_date"),
            "unit_conversions": ("from_unit", "to_unit", "effective_date"),
            "standard_costs": ("object_id", "element_id", "quantity_unit", "currency"),
            "benchmarks": ("object_id", "scenario", "currency"),
        }
        for name, required in collections.items():
            for index, raw in enumerate(_sequence(schema.get(name), "schema." + name, optional=True)):
                location = f"schema.{name}[{index}]"
                item = _mapping(raw, location)
                for field in required:
                    _required_text(item, field, location)
                if name == "allocation_rules":
                    for field in ("rule_id", "method", "driver_id", "driver_name", "driver_unit", "description"):
                        if field in item and not isinstance(item[field], str):
                            raise ValueError(f"{location}.{field} must be a string")
                    targets = _sequence(item.get("target_ids"), location + ".target_ids")
                    if not targets or any(not isinstance(target, str) or not target.strip() for target in targets):
                        raise ValueError(f"{location}.target_ids must contain nonempty strings")
                    if len(targets) != len(set(targets)):
                        raise ValueError(f"{location}.target_ids must be unique")
                    weights = _mapping(item.get("weights"), location + ".weights", optional=True)
                    for target, value in weights.items():
                        if not isinstance(target, str) or not target.strip():
                            raise ValueError(f"{location}.weights has an invalid object identifier")
                        _number(value, location + ".weights." + target)
                    factors = item.get("weight_factors")
                    if factors is not None:
                        if "weights" in item or item.get("method", "driver") != "driver":
                            raise ValueError(f"{location}.weight_factors requires driver and excludes weights")
                        _factor_weights(item, location)
                    _integer(item.get("stage"), location + ".stage", 1)
                    _integer(item.get("rounding"), location + ".rounding", 2)
                    _boolean(item.get("allow_partial"), location + ".allow_partial")
                elif name == "currency_rates":
                    _number(item.get("rate"), location + ".rate")
                elif name == "unit_conversions":
                    _number(item.get("factor"), location + ".factor")
                elif name == "standard_costs":
                    _number(item.get("quantity_per_output"), location + ".quantity_per_output")
                    _number(item.get("standard_rate"), location + ".standard_rate")
                elif name == "benchmarks":
                    _number(item.get("total_cost"), location + ".total_cost")
                    _number(item.get("quantity"), location + ".quantity", optional=True)
        forecast = _mapping(schema.get("forecast"), "schema.forecast", optional=True)
        forecast_fields = {"quantity_growth_pct", "selling_price_growth_pct", "variable_cost_growth_pct", "fixed_cost_growth_pct"}
        if set(forecast) - forecast_fields:
            raise ValueError("schema.forecast contains unsupported fields")
        for key, value in forecast.items():
            _number(value, "schema.forecast." + key)
        options = _mapping(schema.get("pool_options"), "schema.pool_options", optional=True)
        for pool_id, option in options.items():
            if not isinstance(pool_id, str) or not pool_id:
                raise ValueError("schema.pool_options requires nonempty pool identifiers")
            option = _mapping(option, "schema.pool_options." + pool_id)
            if set(option) - {"include_incoming", "variable_fraction"}:
                raise ValueError("schema.pool_options contains unsupported fields")
            _boolean(option.get("include_incoming"), "pool_options.include_incoming")
            _number(option.get("variable_fraction"), "pool_options.variable_fraction", optional=True)

    def from_text(self, csv_text: str, schema: Mapping[str, Any], *,
                  source_uri: str = "upload://data.csv", schema_uri: str = "upload://schema",
                  trusted_source_book=None) -> CostingInput:
        self.validate_schema(schema)
        if not isinstance(csv_text, str):
            raise ValueError("CSV content must be text")
        if len(csv_text.encode("utf-8")) > 10 * 1024 * 1024:
            raise ValueError("CSV exceeds 10 MiB")
        try:
            reader = csv.DictReader(io.StringIO(csv_text.lstrip("\ufeff")), strict=True)
            headers = reader.fieldnames
            if not headers or any(not header for header in headers) or len(headers) != len(set(headers)):
                raise ValueError("CSV headers must be nonempty and unique")
            rows = list(reader)
        except csv.Error as exc:
            raise ValueError(f"Invalid CSV: {exc}") from None
        if not rows:
            raise ValueError("CSV has no data rows")
        if len(rows) > 100000:
            raise ValueError("CSV exceeds 100000 rows")
        if "raw_recognition" in schema:
            from .recognition_ingest import recognize_and_compile
            return recognize_and_compile(self, headers, rows, schema, source_uri, schema_uri, trusted_source_book)
        if "raw_inventory" in schema:
            from .inventory_ingest import compile_inventory
            if trusted_source_book is not None:
                raise ValueError("inventory: trusted monetary manifest is not a movement registry")
            return compile_inventory(self, headers, rows, schema, source_uri, schema_uri)
        if "raw_meter" in schema:
            from .meter_ingest import compile_meter
            if trusted_source_book is not None:
                raise ValueError("meter: monetary manifest does not verify consumption readings")
            return compile_meter(self,headers,rows,schema,source_uri,schema_uri)
        if "raw_composite" in schema:
            from .composite_ingest import compile_composite
            if trusted_source_book is not None:
                raise ValueError("composite: no independent consolidated source registry configured")
            return compile_composite(self,headers,rows,schema,source_uri,schema_uri)
        columns = {key: key for key in _COLUMNS}
        columns.update(schema.get("columns") or {})
        for key in ("object_id", "amount", "cost_element", "currency"):
            if columns[key] not in headers:
                raise ValueError(f"CSV is missing mapped column {columns[key]}")
        dataset_id = schema.get("dataset_id", "csv-unregistered").strip()
        plugin_id = schema.get("plugin_id", "csv-generic").strip()
        period = schema.get("period", "").strip()
        scenario = schema.get("scenario", "actual").strip()
        base_currency = schema.get("currency", "").strip()
        objects, entries, pools, reported_totals, reported_unit_costs = {}, [], [], {}, {}
        used_pool_options = set()
        coverage_postings = []

        def source(field, selector, kind="raw", uri=source_uri):
            return SourceRef(dataset_id=dataset_id, source_uri=uri, field=field,
                             selector=selector, kind=kind)

        def schema_source(field, index):
            return source(field, f"{field}[{index}]", "configuration", schema_uri)

        row_identity = tuple(schema.get("row_identity") or ())
        if set(row_identity) - set(headers):
            raise ValueError("schema.row_identity refers to missing CSV headers")
        seen = {}
        for index, row in enumerate(rows, 2):
            if None in row or any(value is None for value in row.values()):
                raise ValueError(f"CSV row {index} has a different number of fields than the header")
            if row_identity:
                identity_values = tuple(row[k].strip() for k in row_identity)
                if not all(identity_values):
                    raise ValueError(f"CSV row {index} has an empty configured row identity")
                if identity_values in seen:
                    raise ValueError(f"CSV row {index} duplicates configured identity at row {seen[identity_values]}")
                seen[identity_values] = index
            def get(key, default=""):
                value = row.get(columns[key], default)
                return str(value).strip() if value is not None else default
            object_id = get("object_id")
            if not object_id:
                raise ValueError(f"CSV row {index} has no object_id")
            currency = get("currency") or base_currency
            if not currency:
                raise ValueError(f"CSV row {index} has no currency")
            if not base_currency:
                base_currency = currency
            quantity = _number(get("quantity"), f"row {index}.quantity", optional=True)
            price = _number(get("selling_price"), f"row {index}.selling_price", optional=True)
            selector = urlencode({"row": index, "object": object_id})
            object_refs = tuple(source(columns[key], selector) for key, value in
                                (("quantity", quantity), ("selling_price", price)) if value is not None)
            candidate = CostObject(object_id=object_id, name=get("object_name") or object_id,
                kind=get("object_kind") or "product", quantity=quantity,
                quantity_unit=get("quantity_unit") or "unit", currency=currency,
                selling_price=price, source_refs=object_refs)
            if object_id in objects:
                previous = objects[object_id]
                identity = ("name", "kind", "quantity", "quantity_unit", "currency", "selling_price")
                if any(getattr(previous, field) != getattr(candidate, field) for field in identity):
                    raise ValueError(f"CSV row {index} conflicts with object {object_id} definition (including selling_price)")
                candidate = replace(previous, source_refs=tuple(dict.fromkeys(previous.source_refs + object_refs)))
            objects[object_id] = candidate
            row_period = get("period")
            if row_period:
                if period and row_period != period:
                    raise ValueError(f"CSV row {index} belongs to another period: {row_period}")
                period = period or row_period
            if get("record_type") == "object":
                prohibited=("amount","cost_element","pool_id","cost_type","behavior","entry_quantity","unit_rate","variable_fraction")
                if any(get(k) for k in prohibited) or any(row.get(k,"").strip() for k in ("source_system","document_id","line_id","split_id")):
                    raise ValueError(f"CSV row {index}: object declaration cannot carry posting fields")
                continue
            amount = _number(get("amount"), f"row {index}.amount")
            element = get("cost_element")
            if not element:
                raise ValueError(f"CSV row {index} has no cost_element")
            record_type = get("record_type") or "entry"
            behavior = get("behavior") or "unknown"
            fraction = _number(get("variable_fraction"), f"row {index}.variable_fraction", optional=True)
            refs = (source(columns["amount"], selector),)
            refs += tuple(source(columns[key], selector) for key in
                          ("behavior", "cost_type", "variable_fraction", "entry_quantity", "entry_unit", "unit_rate")
                          if get(key))
            if record_type == "pool":
                pool_id = get("pool_id")
                if not pool_id:
                    raise ValueError(f"CSV row {index} pool_id is required")
                option = (schema.get("pool_options") or {}).get(pool_id, {})
                if option:
                    refs += (source("pool_options", urlencode({"pool_id": pool_id}), "configuration", schema_uri),)
                if get("include_incoming"):
                    refs += (source(columns["include_incoming"], selector),)
                used_pool_options.add(pool_id)
                incoming = _boolean(get("include_incoming"), f"row {index}.include_incoming")
                if "include_incoming" in option:
                    configured = _boolean(option["include_incoming"], "pool_options.include_incoming")
                    if get("include_incoming") and incoming != configured:
                        raise ValueError(f"CSV row {index} include_incoming conflicts with pool_options")
                    incoming = configured
                if "variable_fraction" in option:
                    configured = _number(option["variable_fraction"], "pool_options.variable_fraction", optional=True)
                    if fraction is not None and fraction != configured:
                        raise ValueError(f"CSV row {index} variable_fraction conflicts with pool_options")
                    fraction = configured
                pools.append(CostPool(pool_id=pool_id, name=get("pool_name") or pool_id,
                    owner_id=get("pool_owner_id") or object_id, amount=amount, currency=currency,
                    element_id=element, cost_type=get("cost_type") or "indirect", behavior=behavior,
                    include_incoming=incoming, variable_fraction=fraction, source_refs=refs))
                coverage_postings.append((index,row,"pool",pools[-1]))
            elif record_type == "entry":
                entries.append(CostEntry(entry_id=f"csv-row-{index}", object_id=object_id,
                    element_id=element, amount=amount, currency=currency,
                    cost_type=get("cost_type") or "direct", behavior=behavior,
                    quantity=_number(get("entry_quantity"), f"row {index}.entry_quantity", optional=True),
                    unit_rate=_number(get("unit_rate"), f"row {index}.unit_rate", optional=True),
                    unit=get("entry_unit"), variable_fraction=fraction, source_refs=refs))
                coverage_postings.append((index,row,"entry",entries[-1]))
            else:
                raise ValueError(f"CSV row {index} has unsupported record_type {record_type}")
            for field, mapping in (("reported_total", reported_totals), ("reported_unit_cost", reported_unit_costs)):
                value = _number(get(field), f"row {index}.{field}", optional=True)
                if value is not None:
                    if object_id in mapping and mapping[object_id] != value:
                        raise ValueError(f"CSV row {index} conflicts with reported {field} for {object_id}")
                    mapping[object_id] = value
        unused = set(schema.get("pool_options") or {}) - used_pool_options
        if unused:
            raise ValueError(f"pool_options references unknown pools: {sorted(unused)}")
        if not period:
            raise ValueError("CSV schema or a mapped column must specify period")
        rules, drivers = [], {}
        for index, raw in enumerate(schema.get("allocation_rules") or (), 1):
            method = raw.get("method", "driver")
            driver_id = raw.get("driver_id", f"csv-driver-{index}")
            refs = (schema_source("allocation_rules", index - 1),)
            if method not in {"equal", "direct"}:
                factors = raw.get("weight_factors")
                values = (_factor_weights(raw, f"allocation_rules[{index-1}]") if factors is not None else
                          {key: _number(value, "driver." + key) for key, value in (raw.get("weights") or {}).items()})
                if factors is not None:
                    refs += (source("weight_factors", f"allocation_rules[{index-1}].weight_factors", "configuration", schema_uri),)
                driver = CostDriver(driver_id=driver_id, name=raw.get("driver_name", driver_id),
                    unit=raw.get("driver_unit", "weight"), values=values, source_refs=refs)
                if driver_id in drivers and dict(drivers[driver_id].values) != values:
                    raise ValueError(f"Conflicting repeated driver {driver_id}")
                drivers[driver_id] = driver
            rules.append(AllocationRule(rule_id=raw.get("rule_id", f"csv-rule-{index}"),
                pool_id=raw["pool_id"], target_ids=tuple(raw["target_ids"]), method=method,
                driver_id=driver_id if method not in {"equal", "direct"} else "",
                stage=_integer(raw.get("stage"), "rule.stage", 1),
                rounding=_integer(raw.get("rounding"), "rule.rounding", 2),
                allow_partial=_boolean(raw.get("allow_partial"), "rule.allow_partial"),
                description=raw.get("description", "")))
        rates = tuple(CurrencyRate(from_currency=item["from_currency"], to_currency=item["to_currency"],
            rate=_number(item["rate"], "currency_rates.rate"), effective_date=item["effective_date"],
            source_ref=schema_source("currency_rates", index))
            for index, item in enumerate(schema.get("currency_rates") or ()))
        conversions = tuple(UnitConversion(from_unit=item["from_unit"], to_unit=item["to_unit"],
            factor=_number(item["factor"], "unit_conversions.factor"), effective_date=item["effective_date"],
            source_ref=schema_source("unit_conversions", index))
            for index, item in enumerate(schema.get("unit_conversions") or ()))
        standards = tuple(StandardCost(object_id=item["object_id"], element_id=item["element_id"],
            quantity_per_output=_number(item["quantity_per_output"], "standard.quantity_per_output"),
            quantity_unit=item["quantity_unit"], standard_rate=_number(item["standard_rate"], "standard.standard_rate"),
            currency=item["currency"], source_refs=(schema_source("standard_costs", index),))
            for index, item in enumerate(schema.get("standard_costs") or ()))
        benchmarks = tuple(CostBenchmark(object_id=item["object_id"], scenario=item["scenario"],
            total_cost=_number(item["total_cost"], "benchmark.total_cost"), currency=item["currency"],
            quantity=_number(item.get("quantity"), "benchmark.quantity", optional=True),
            source_refs=(schema_source("benchmarks", index),))
            for index, item in enumerate(schema.get("benchmarks") or ()))
        forecast = (ForecastAssumption(**{key: _number(value, "forecast." + key)
                                         for key, value in schema["forecast"].items()},
                                       source_refs=(source("forecast", "forecast", "configuration", schema_uri),))
                    if schema.get("forecast") is not None else None)
        coverage = None
        if "completeness" in schema:
            from .completeness import evaluate
            coverage = evaluate(schema["completeness"], coverage_postings, rules, headers,
                                base_currency, dataset_id, schema_uri, period)
        return CostingInput(dataset_id=dataset_id, plugin_id=plugin_id, period=period,
            scenario=scenario, currency=base_currency, objects=tuple(objects.values()),
            entries=tuple(entries), pools=tuple(pools), drivers=tuple(drivers.values()), rules=tuple(rules),
            currency_rates=rates, unit_conversions=conversions, standard_costs=standards,
            benchmarks=benchmarks, forecast=forecast, reported_totals=reported_totals,
            reported_unit_costs=reported_unit_costs, method="mapped_csv_ledger", coverage=coverage)
