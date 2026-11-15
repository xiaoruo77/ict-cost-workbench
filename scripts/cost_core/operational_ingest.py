"""Validated, declarative operational CSV exports for physical quantities.

This compiler creates quantities/drivers, never financial postings. Vendor live
connections and accounting recognition remain outside this module.
"""
from __future__ import annotations
import calendar
import csv
import hashlib
import io
import re
import unicodedata
from datetime import datetime, timezone, timedelta
from decimal import Decimal, InvalidOperation, localcontext
from urllib.parse import urlencode
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

MAX_ROWS = 15000
MAX_FILES = 16
MAX_METRICS = 64
MAX_BINDINGS = 512
TOKEN = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
NUMBER = re.compile(r"^[+-]?\d+(?:\.\d+)?$")
DECIMAL_LIMIT = Decimal("1e24")
# Physical symbols are immutable. Business count units additionally carry an
# explicit quantity_kind; equal arithmetic dimensions do not make them fungible.
STANDARD_UNITS = {
    "kg": ({"mass":1}, "1", None), "g": ({"mass":1}, "0.001", None),
    "tonne": ({"mass":1}, "1000", None), "t": ({"mass":1}, "1000", None),
    "m": ({"length":1}, "1", None), "km": ({"length":1}, "1000", None),
    "second": ({"time":1}, "1", None), "s": ({"time":1}, "1", None),
    "minute": ({"time":1}, "60", None), "min": ({"time":1}, "60", None),
    "hour": ({"time":1}, "3600", None), "h": ({"time":1}, "3600", None),
    "day": ({"time":1}, "86400", None), "tonne_km": ({"mass":1,"length":1}, "1000000", None),
    "pallet": ({"pallet":1}, "1", "pallet"),
    "pallet_day": ({"pallet":1,"time":1}, "86400", "pallet_time"),
}
PHYSICAL_DIMENSIONS = {"mass","length","time","temperature","current","amount","luminosity"}


def _error(where, message):
    raise ValueError(f"operational {where}: {message}")


def _token(value, where):
    if not isinstance(value, str) or not TOKEN.fullmatch(value):
        _error(where, "expected non-empty identifier")
    return value


def _identity(value):
    return unicodedata.normalize("NFKC", str(value)).strip().casefold()


def _text(value, where):
    if not isinstance(value, str) or not value.strip() or len(value) > 256:
        _error(where, "expected non-empty text")
    if any(ord(c) < 32 for c in value):
        _error(where, "control characters forbidden")
    return value.strip()


def _keys(value, allowed, where):
    if not isinstance(value, dict):
        _error(where, "expected object")
    unknown = set(value) - set(allowed)
    if unknown:
        _error(where, f"unknown fields {sorted(unknown)}")


def _decimal(value, where, *, positive=False):
    if not isinstance(value, (str, int, Decimal)) or isinstance(value, bool):
        _error(where, "expected fixed-point number")
    raw = str(value)
    if not NUMBER.fullmatch(raw):
        _error(where, "expected finite fixed-point number")
    try:
        result = Decimal(raw)
    except InvalidOperation:
        _error(where, "invalid number")
    if not result.is_finite() or abs(result) > DECIMAL_LIMIT:
        _error(where, "number outside supported range")
    if result < 0 or (positive and result == 0):
        _error(where, "must be positive" if positive else "must be non-negative")
    if len(result.as_tuple().digits) > 30:
        _error(where, "too many significant digits")
    return result


def _string(value):
    if value == 0:
        return "0"
    raw = format(value, "f")
    return raw.rstrip("0").rstrip(".") if "." in raw else raw


def _file(value, where):
    value = _text(value, where)
    if value.endswith(".csv"):
        value = value[:-4]
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,40}", value):
        _error(where, "file must be a named CSV source")
    return value


def operational_files(spec):
    """Return declared auxiliary file keys for exact bundle manifest validation."""
    if not isinstance(spec, dict) or type(spec.get("version")) is not int or spec.get("version") != 1:
        _error("version", "expected version 1")
    sources = spec.get("sources")
    if not isinstance(sources, dict) or not 1 <= len(sources) <= MAX_FILES:
        _error("sources", "invalid source count")
    names, canonical_names = set(), set()
    for sid, source in sources.items():
        _token(sid, "source id")
        if not isinstance(source, dict):
            _error(sid, "expected source object")
        name = _file(source.get("file"), sid + ".file")
        if _identity(name) in canonical_names:
            _error(sid, "duplicate physical source declaration")
        canonical_names.add(_identity(name))
        names.add(name)
    return names


def _registry(spec):
    values = spec.get("unit_registry")
    if not isinstance(values, dict) or not 1 <= len(values) <= 64:
        _error("unit_registry", "1..64 explicit units required")
    registry = {}
    for name, definition in values.items():
        _token(name, "unit name")
        _keys(definition, {"dimensions", "factor", "aliases", "quantity_kind"}, "unit " + name)
        dims = definition.get("dimensions")
        if not isinstance(dims, dict) or not 1 <= len(dims) <= 8:
            _error(name, "explicit dimensions required")
        clean = {}
        for dimension, power in dims.items():
            _token(dimension, name + ".dimension")
            if isinstance(power, bool) or not isinstance(power, int) or not -8 <= power <= 8 or power == 0:
                _error(name, "invalid dimension power")
            clean[dimension] = power
        factor = _decimal(definition.get("factor"), name + ".factor", positive=True)
        quantity_kind = definition.get("quantity_kind")
        if name in STANDARD_UNITS:
            standard_dims, standard_factor, standard_kind = STANDARD_UNITS[name]
            if clean != standard_dims or factor != Decimal(standard_factor):
                _error(name, "standard unit dimensions/factor cannot be redefined")
            if quantity_kind is not None and quantity_kind != standard_kind:
                _error(name, "standard quantity_kind cannot be redefined")
            quantity_kind = standard_kind
        elif set(clean) - PHYSICAL_DIMENSIONS:
            quantity_kind = _token(quantity_kind, name + ".quantity_kind")
        elif quantity_kind is not None:
            quantity_kind = _token(quantity_kind, name + ".quantity_kind")
        aliases = definition.get("aliases", [])
        if not isinstance(aliases, list) or len(aliases) > 16:
            _error(name, "invalid aliases")
        record = {"name": name, "dimensions": clean, "factor": factor, "quantity_kind": quantity_kind}
        for spelling in [name] + aliases:
            _token(spelling, name + ".alias")
            if spelling in STANDARD_UNITS:
                sd, sf, sk = STANDARD_UNITS[spelling]
                if clean != sd or factor != Decimal(sf) or quantity_kind != sk:
                    _error(name, "standard unit alias cannot change dimension/factor/kind")
            if spelling in registry:
                _error(name, "duplicate unit/alias")
            registry[spelling] = record
    return registry


def _unit(value, registry, where):
    if not isinstance(value, str) or value not in registry:
        _error(where, f"unit {value!r} is not in approved unit_registry")
    return registry[value]


def _ref(dataset_id, source, row, field):
    return {"dataset_id": dataset_id, "source_uri": "upload://sources/" + source["file"] + ".csv",
            "field": field, "selector": urlencode({"row": row["record"], source["fields"]["id"]: row["id"]}),
            "kind": "raw", "formula": ""}


def _unique_refs(refs):
    result, seen = [], set()
    for ref in refs:
        key = (ref["source_uri"], ref["selector"], ref["field"])
        if key not in seen:
            seen.add(key)
            result.append(ref)
    return result


def _timestamp(value, where):
    if not isinstance(value, str) or not value.strip():
        _error(where, "timestamp missing")
    try:
        result = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        _error(where, "invalid ISO timestamp")
    if result.tzinfo is None or result.utcoffset() is None:
        _error(where, "timezone-aware timestamp required")
    return result.astimezone(timezone.utc)


def _period_bounds(scope, period_timezone):
    if not re.fullmatch(r"\d{4}-\d{2}", str(scope.get("period", ""))):
        _error("scope.period", "YYYY-MM required for duration clipping")
    year, month = map(int, scope["period"].split("-"))
    if not isinstance(period_timezone, str) or not period_timezone:
        _error("period_timezone", "explicit period timezone required")
    if period_timezone == "UTC":
        zone = timezone.utc
    elif re.fullmatch(r"[+-]\d{2}:\d{2}", period_timezone):
        hours, minutes = map(int, period_timezone[1:].split(":"))
        if hours > 14 or minutes > 59 or (hours == 14 and minutes):
            _error("period_timezone", "invalid fixed offset")
        zone = timezone(timedelta(minutes=(hours*60+minutes)*(1 if period_timezone[0]=="+" else -1)))
    else:
        try:
            zone = ZoneInfo(period_timezone)
        except (ZoneInfoNotFoundError, ValueError):
            _error("period_timezone", "unknown/unsupported timezone")
    try:
        start = datetime(year, month, 1, tzinfo=zone).astimezone(timezone.utc)
        end = datetime(year + (month == 12), 1 if month == 12 else month + 1, 1, tzinfo=zone).astimezone(timezone.utc)
    except ValueError:
        _error("scope.period", "invalid month")
    return start, end


def _factor(factor, rows, sources, registry, scope, dataset_id, where, period_timezone):
    _keys(factor, {"op", "field", "source", "unit", "unit_field", "start_field", "end_field",
                   "clip_to_period", "open_end", "power"}, where)
    source_id = factor.get("source", next(iter(rows)))
    if source_id not in rows:
        _error(where, "factor source must be primary or joined")
    row, source = rows[source_id], sources[source_id]
    where += f" [{source_id} row={row['record']} id={row['id']}]"
    data = row["data"]
    op = factor.get("op", "field")
    expected = _unit(factor.get("unit"), registry, where + ".unit")
    power = factor.get("power", 1)
    if isinstance(power, bool) or not isinstance(power, int) or power not in {-2, -1, 1, 2}:
        _error(where, "factor power must be -2,-1,1,2")
    refs = []
    def cell(name):
        field = _text(name, where + ".field")
        if field not in data:
            _error(where, "mapped column missing: " + field)
        return data[field], field
    if op == "duration":
        start_value, start_field = cell(factor.get("start_field"))
        end_value, end_field = cell(factor.get("end_field"))
        start = _timestamp(start_value, where + "." + start_field)
        if not end_value:
            if factor.get("open_end") != "scope_end":
                _error(where, "missing end requires explicit open_end=scope_end")
            _, end = _period_bounds(scope, period_timezone)
        else:
            end = _timestamp(end_value, where + "." + end_field)
        if end < start:
            _error(where, "end precedes start")
        if factor.get("clip_to_period", True):
            low, high = _period_bounds(scope, period_timezone)
            start, end = max(start, low), min(end, high)
        delta = max(end - start, timedelta(0))
        seconds = Decimal(delta.days * 86400 + delta.seconds) + Decimal(delta.microseconds) / Decimal(1000000)
        second = _unit("second", registry, where)
        if second["dimensions"] != {"time": 1} or second["factor"] != 1:
            _error(where, "duration requires second={time:1}, factor=1")
        if expected["dimensions"] != {"time": 1}:
            _error(where, "duration output must have time dimension")
        value_base = seconds
        refs = [_ref(dataset_id, source, row, start_field), _ref(dataset_id, source, row, end_field)]
    else:
        raw_unit_value, unit_field = cell(factor.get("unit_field"))
        actual = _unit(raw_unit_value, registry, where + "." + unit_field)
        if actual["dimensions"] != expected["dimensions"] or actual["quantity_kind"] != expected["quantity_kind"]:
            _error(where + "." + unit_field, "raw unit dimension/quantity_kind differs from approved factor")
        if op == "field":
            value, value_field = cell(factor.get("field"))
            quantity = _decimal(value, where + "." + value_field)
            refs = [_ref(dataset_id, source, row, value_field)]
        elif op == "difference":
            start_value, start_field = cell(factor.get("start_field"))
            end_value, end_field = cell(factor.get("end_field"))
            first = _decimal(start_value, where + "." + start_field)
            last = _decimal(end_value, where + "." + end_field)
            if last < first:
                _error(where, "negative cumulative difference")
            quantity = last - first
            refs = [_ref(dataset_id, source, row, start_field), _ref(dataset_id, source, row, end_field)]
        else:
            _error(where, "unknown factor op")
        value_base = quantity * actual["factor"]
    if value_base == 0 and power < 0:
        _error(where, "zero factor denominator")
    dims = {k: v * power for k, v in expected["dimensions"].items()}
    return value_base ** power, dims, refs


def _compile_operational_bundle(spec, files, scope, *, dataset_id="csv-operational"):
    """Compile approved structured exports into source-backed object quantities."""
    _keys(spec, {"version", "rule_version", "policy_source", "sources", "source_hashes",
                 "unit_registry", "metrics", "object_bindings", "driver_bindings", "relations", "period_timezone"}, "spec")
    names = operational_files(spec)
    version = _text(spec.get("rule_version"), "rule_version")
    policy = _text(spec.get("policy_source"), "policy_source")
    registry = _registry(spec)
    period_timezone = spec.get("period_timezone")
    if not isinstance(scope, dict) or not isinstance(files, dict):
        _error("context", "scope and files must be objects")
    for key in ("book", "period", "currency"):
        _text(scope.get(key), "scope." + key)
    sources, hashes, total_rows, object_spellings = {}, {}, 0, {}
    expected_hashes = spec.get("source_hashes", {})
    if not isinstance(expected_hashes, dict) or set(expected_hashes) - names:
        _error("source_hashes", "unknown source hash")
    for sid, source in spec["sources"].items():
        _keys(source, {"file", "fields", "role"}, sid)
        name = _file(source["file"], sid)
        if name not in files:
            _error(sid, "missing named operational file")
        content = files[name]
        if not isinstance(content, (str, bytes)):
            _error(sid, "CSV input must be text or bytes")
        raw = content if isinstance(content, bytes) else str(content).encode("utf-8")
        hashes[name] = hashlib.sha256(raw).hexdigest()
        if name in expected_hashes and expected_hashes[name] != hashes[name]:
            _error(sid, "source hash mismatch")
        try:
            text = raw.decode("utf-8-sig")
        except UnicodeDecodeError:
            _error(sid, "UTF-8 CSV required")
        reader = csv.DictReader(io.StringIO(text, newline=""))
        headers = reader.fieldnames
        if not headers or len({_identity(h) for h in headers}) != len(headers):
            _error(sid, "missing/duplicate/aliased column")
        fields = source.get("fields")
        _keys(fields, {"id", "object_id", "book", "period", "currency", "action", "original_id",
                       "status", "economic_key"}, sid + ".fields")
        role = source.get("role", "events")
        if role not in {"events", "lookup"}:
            _error(sid, "invalid source role")
        required = {"id", "book", "period", "currency"} | ({"object_id", "action"} if role == "events" else set())
        if not required <= set(fields):
            _error(sid, "required field mappings missing")
        if isinstance(fields.get("id"), str) and fields["id"].casefold() == "row":
            _error(sid, "id column row conflicts with reserved record selector")
        if any(not isinstance(v, str) or v not in headers for v in fields.values()):
            _error(sid, "mapped column missing")
        records, ids, economic = [], set(), set()
        for record, data in enumerate(reader, 2):
            total_rows += 1
            if total_rows > MAX_ROWS:
                _error("sources", "operational row limit exceeded")
            if None in data or any(v is None for v in data.values()):
                _error(f"{sid} row {record}", "ragged CSV record")
            where = f"{sid} row {record}"
            values = {key: data[column].strip() for key, column in fields.items()}
            eid = _text(values["id"], where + ".id")
            ident = _identity(eid)
            if ident in ids:
                _error(where, "duplicate or aliased event ID")
            ids.add(ident)
            for key in ("book", "period", "currency"):
                if values[key] != scope[key]:
                    _error(where, "cross-book/period/currency")
            if values.get("economic_key"):
                econ = _identity(values["economic_key"])
                if econ in economic:
                    _error(where, "duplicate economic event")
                economic.add(econ)
            action = values.get("action", "measure")
            if action not in {"measure", "reverse"}:
                _error(where, "unknown action")
            status = values.get("status", "data")
            if status not in {"data", "missing", "not_applicable"}:
                _error(where, "unknown data status")
            oid = values.get("object_id", "")
            if role == "events":
                _text(oid, where + ".object_id")
            if oid:
                canonical_object = _identity(oid)
                if canonical_object in object_spellings and object_spellings[canonical_object] != oid:
                    _error(where, "aliased operational object ID")
                object_spellings[canonical_object] = oid
            records.append({"id": eid, "record": record, "object_id": oid, "action": action,
                            "original_id": values.get("original_id", ""), "status": status, "data": data})
        sources[sid] = {"file": name, "records": records, "fields": fields, "role": role, "headers": set(headers)}
    metrics = spec.get("metrics")
    if not isinstance(metrics, list) or not 1 <= len(metrics) <= MAX_METRICS:
        _error("metrics", "invalid metric count")
    outputs, used, indexes, metric_units = {}, {sid: set() for sid in sources}, {}, {}
    for metric in metrics:
        _keys(metric, {"id", "source", "unit", "factors", "match", "joins"}, "metric")
        mid = _token(metric.get("id"), "metric.id")
        if mid in outputs:
            _error(mid, "duplicate metric ID")
        sid = metric.get("source")
        if not isinstance(sid, str) or sid not in sources or sources[sid]["role"] != "events":
            _error(mid, "metric primary must be event source")
        output_unit = _unit(metric.get("unit"), registry, mid + ".unit")
        factors = metric.get("factors")
        if not isinstance(factors, list) or not 1 <= len(factors) <= 4:
            _error(mid, "1..4 factors required")
        match = metric.get("match", {})
        if not isinstance(match, dict) or any(not isinstance(k, str) or not isinstance(v, str) for k,v in match.items()):
            _error(mid, "match must be field/value object")
        joins = metric.get("joins", [])
        if not isinstance(joins, list) or len(joins) > 4:
            _error(mid, "invalid joins")
        declared_sources = {sid}
        for join in joins:
            _keys(join, {"source", "left_field", "right_field"}, mid + ".join")
            right = join.get("source")
            if not isinstance(right, str) or right not in sources or right in declared_sources:
                _error(mid, "invalid joined source")
            if join.get("left_field") not in sources[sid]["headers"] or join.get("right_field") not in sources[right]["headers"]:
                _error(mid, "join column missing")
            declared_sources.add(right)
        static_dimensions = {}
        for index, factor in enumerate(factors):
            where = f"{mid}.factor[{index}]"
            _keys(factor, {"op", "field", "source", "unit", "unit_field", "start_field", "end_field",
                           "clip_to_period", "open_end", "power"}, where)
            expected_factor_unit = _unit(factor.get("unit"), registry, where)
            fsid = factor.get("source", sid)
            if not isinstance(fsid, str) or fsid not in declared_sources:
                _error(where, "factor source must be primary or joined")
            power = factor.get("power", 1)
            if type(power) is not int or power not in {-2, -1, 1, 2}:
                _error(where, "factor power must be -2,-1,1,2")
            op = factor.get("op", "field")
            if op == "field":
                needed = {"field", "unit_field"}
            elif op == "difference":
                needed = {"start_field", "end_field", "unit_field"}
            elif op == "duration":
                needed = {"start_field", "end_field"}
                _period_bounds(scope, period_timezone)
                second = _unit("second", registry, where)
                if second["dimensions"] != {"time": 1} or second["factor"] != 1 or expected_factor_unit["dimensions"] != {"time": 1}:
                    _error(where, "duration requires approved time dimension and second factor 1")
                if "clip_to_period" in factor and type(factor["clip_to_period"]) is not bool:
                    _error(where, "clip_to_period must be boolean")
                if "open_end" in factor and factor["open_end"] != "scope_end":
                    _error(where, "unknown open_end policy")
            else:
                _error(where, "unknown factor op")
            if any(not isinstance(factor.get(key), str) or factor[key] not in sources[fsid]["headers"] for key in needed):
                _error(where, "mapped factor column missing")
            for dimension, exponent in expected_factor_unit["dimensions"].items():
                static_dimensions[dimension] = static_dimensions.get(dimension, 0) + exponent * power
        if {k:v for k,v in static_dimensions.items() if v} != output_unit["dimensions"]:
            _error(mid, "metric output unit dimensions differ from factor expression")
        metric_units[mid] = output_unit["name"]
        quantities, references, statuses, events, reverse_rows = {}, {}, {}, {}, []
        for row in sources[sid]["records"]:
            if any(key not in row["data"] for key in match):
                _error(mid, "match column missing")
            if any(row["data"][key] != value for key, value in match.items()):
                continue
            oid = row["object_id"]
            used[sid].add(row["record"])
            refs = [_ref(dataset_id, sources[sid], row, sources[sid]["fields"]["id"])]
            statuses.setdefault(oid, []).append(row["status"])
            if row["status"] != "data":
                if row["action"] == "reverse":
                    _error(mid, "reverse requires data")
                references.setdefault(oid, []).extend(refs)
                continue
            row_sources = {sid: row}
            for join in joins:
                _keys(join, {"source", "left_field", "right_field"}, mid + ".join")
                right = join.get("source")
                if not isinstance(right, str) or right not in sources or right == sid or right in row_sources:
                    _error(mid, "invalid joined source")
                left_field = join.get("left_field")
                right_field = join.get("right_field")
                if left_field not in row["data"]:
                    _error(mid, "left join column missing")
                cache_key = (right, right_field)
                if cache_key not in indexes:
                    index = {}
                    for rr in sources[right]["records"]:
                        if right_field not in rr["data"]:
                            _error(mid, "right join column missing")
                        key = _identity(_text(rr["data"][right_field], mid + ".join_key"))
                        if key in index:
                            _error(mid, "duplicate/aliased join key")
                        index[key] = rr
                    indexes[cache_key] = index
                key = _identity(_text(row["data"][left_field], mid + ".join_key"))
                if key not in indexes[cache_key]:
                    _error(mid, "unmatched operational join")
                joined = indexes[cache_key][key]
                if joined["status"] != "data":
                    _error(mid, "joined measurement missing")
                if joined["object_id"] and joined["object_id"] != oid:
                    _error(mid, "joined object mismatch")
                row_sources[right] = joined
                used[right].add(joined["record"])
            quantity, dimensions = Decimal(1), {}
            with localcontext() as context:
                context.prec = 40
                for index, factor in enumerate(factors):
                    value, dims, fr = _factor(factor, row_sources, sources, registry, scope, dataset_id, f"{mid}.factor[{index}]", period_timezone)
                    quantity *= value
                    for key, power in dims.items():
                        dimensions[key] = dimensions.get(key, 0) + power
                    refs.extend(fr)
                dimensions = {key: power for key, power in dimensions.items() if power}
                if dimensions != output_unit["dimensions"]:
                    _error(mid, "metric output unit dimensions differ from factor expression")
                quantity /= output_unit["factor"]
            if quantity < 0 or quantity > DECIMAL_LIMIT:
                _error(mid, "derived quantity outside supported range")
            refs = _unique_refs(refs)
            if row["action"] == "reverse":
                if not row["original_id"]:
                    _error(mid, "reverse missing original event")
                reverse_rows.append((row, quantity, refs))
            else:
                events[_identity(row["id"])] = {"object_id": oid, "quantity": quantity, "remaining": quantity, "refs": refs}
                quantities[oid] = quantities.get(oid, Decimal(0)) + quantity
                references.setdefault(oid, []).extend(refs)
        for row, quantity, refs in reverse_rows:
            original = events.get(_identity(row["original_id"]))
            if not original or original["object_id"] != row["object_id"]:
                _error(mid, "reverse original not found or object mismatch")
            if quantity > original["remaining"]:
                _error(mid, "reverse exceeds original remaining quantity")
            original["remaining"] -= quantity
            oid = row["object_id"]
            quantities[oid] = quantities.get(oid, Decimal(0)) - quantity
            references.setdefault(oid, []).extend(refs + original["refs"])
        result = {}
        for oid, states in statuses.items():
            quantity = quantities.get(oid, Decimal(0))
            status = "missing" if "missing" in states else "provided" if "data" in states else "not_applicable"
            if status == "provided" and quantity == 0:
                status = "explicit_zero"
            result[oid] = {"quantity": None if status in {"missing", "not_applicable"} else _string(quantity),
                           "unit": output_unit["name"], "status": status, "source_refs": _unique_refs(references.get(oid, []))}
        outputs[mid] = result
    relation_results = []
    relations = spec.get("relations", [])
    if not isinstance(relations, list) or len(relations) > 64:
        _error("relations", "invalid relation count")
    for relation in relations:
        _keys(relation, {"id", "source", "original_source", "reference_field", "original_id_field",
                         "quantity_field", "unit_field", "original_quantity_field", "original_unit_field",
                         "unit", "same_fields"}, "relation")
        rid = _token(relation.get("id"), "relation.id")
        sid, original_sid = relation.get("source"), relation.get("original_source")
        if not isinstance(sid, str) or not isinstance(original_sid, str) or sid not in sources or original_sid not in sources:
            _error(rid, "unknown relation source")
        expected_unit = _unit(relation.get("unit"), registry, rid + ".unit")
        fields = [relation.get(key) for key in ("reference_field", "quantity_field", "unit_field")]
        original_fields = [relation.get(key) for key in ("original_id_field", "original_quantity_field", "original_unit_field")]
        if any(not isinstance(f, str) or f not in sources[sid]["headers"] for f in fields) or any(not isinstance(f, str) or f not in sources[original_sid]["headers"] for f in original_fields):
            _error(rid, "relation column missing")
        same_fields = relation.get("same_fields", {})
        if not isinstance(same_fields, dict) or any(not isinstance(k, str) or not isinstance(v, str) or k not in sources[sid]["headers"] or v not in sources[original_sid]["headers"] for k,v in same_fields.items()):
            _error(rid, "invalid same_fields mapping")
        originals, returned, relation_refs = {}, {}, []
        for row in sources[original_sid]["records"]:
            key = _identity(_text(row["data"][original_fields[0]], rid + ".original_id"))
            if key in originals:
                _error(rid, "duplicate/aliased original relation key")
            unit = _unit(row["data"][original_fields[2]], registry, rid)
            if unit["dimensions"] != expected_unit["dimensions"] or unit["quantity_kind"] != expected_unit["quantity_kind"]:
                _error(rid, "original relation unit dimension/kind mismatch")
            qty = _decimal(row["data"][original_fields[1]], rid + ".original_quantity") * unit["factor"]
            originals[key] = (row, qty)
        for row in sources[sid]["records"]:
            if row["status"] != "data":
                _error(rid, "relation event data missing")
            key = _identity(_text(row["data"][fields[0]], rid + ".reference"))
            if key not in originals:
                _error(rid, "relation original record not found")
            original, capacity = originals[key]
            if original["status"] != "data":
                _error(rid, "original relation data missing")
            if any(row["data"][left] != original["data"][right] for left,right in same_fields.items()):
                _error(rid, "relation identity mismatch")
            unit = _unit(row["data"][fields[2]], registry, rid)
            if unit["dimensions"] != expected_unit["dimensions"] or unit["quantity_kind"] != expected_unit["quantity_kind"]:
                _error(rid, "relation unit dimension/kind mismatch")
            qty = _decimal(row["data"][fields[1]], rid + ".quantity") * unit["factor"]
            returned[key] = returned.get(key, Decimal(0)) + qty
            if returned[key] > capacity:
                _error(rid, "relation return exceeds original remaining quantity")
            used[sid].add(row["record"])
            used[original_sid].add(original["record"])
            relation_refs.extend([_ref(dataset_id, sources[sid], row, fields[0]), _ref(dataset_id, sources[original_sid], original, original_fields[0])])
        relation_results.append({"id": rid, "status": "verified", "matched_events": len(sources[sid]["records"]),
                                 "source_refs": _unique_refs(relation_refs)})
    for sid, source in sources.items():
        missing = {r["record"] for r in source["records"]} - used[sid]
        if missing:
            _error(sid, f"unmapped operational rows {sorted(missing)[:8]}")
    objects, drivers = [], {}
    bindings = spec.get("object_bindings", [])
    dbindings = spec.get("driver_bindings", [])
    if not isinstance(bindings, list) or not isinstance(dbindings, list) or not 1 <= len(bindings) + len(dbindings) <= MAX_BINDINGS:
        _error("bindings", "invalid binding count")
    seen, binding_metadata = set(), []
    for binding_index, binding in enumerate(bindings):
        _keys(binding, {"object_id", "metric_id", "quantity_unit", "name", "kind", "currency", "required"}, "object_binding")
        oid = _text(binding.get("object_id"), "object_binding.object_id")
        if _identity(oid) in seen:
            _error(oid, "duplicate/aliased object binding")
        seen.add(_identity(oid))
        mid = binding.get("metric_id")
        if not isinstance(mid, str) or mid not in outputs:
            _error(oid, "unknown metric binding")
        if "required" in binding and type(binding["required"]) is not bool:
            _error(oid, "required must be boolean")
        declared = _unit(binding.get("quantity_unit"), registry, oid)
        if declared["name"] != metric_units[mid]:
            _error(oid, "object declared unit differs from source-verified metric")
        value = outputs[mid].get(oid)
        if value is None:
            if binding.get("required", True):
                _error(oid, "required operational quantity missing")
            value = {"quantity": None, "unit": metric_units[mid], "status": "missing", "source_refs": []}
        declared = _unit(binding.get("quantity_unit"), registry, oid)
        if declared["name"] != value["unit"]:
            _error(oid, "object declared unit differs from source-verified metric")
        if binding.get("required", True) and value["quantity"] is None:
            _error(oid, "required operational quantity missing/not applicable")
        if "currency" in binding and binding["currency"] != scope["currency"]:
            _error(oid, "object currency mismatch")
        item = {"object_id": oid, "quantity": value["quantity"], "quantity_unit": value["unit"],
                "currency": scope["currency"], "source_refs": value["source_refs"]}
        for key in ("name", "kind"):
            if key in binding:
                item[key] = _text(binding[key], oid + "." + key)
        item["source_refs"] = list(item["source_refs"]) + [{"dataset_id":dataset_id,"source_uri":"upload://schema",
            "field":"operational_inputs","selector":f"raw_composite.operational_inputs.object_bindings[{binding_index}]",
            "kind":"configuration","formula":""}]
        objects.append(item)
        binding_metadata.append({"object_id":oid,"metric_id":mid,"quantity":value["quantity"],"unit":value["unit"],
                                 "status":value["status"],"source_refs":item["source_refs"]})
    for binding in dbindings:
        _keys(binding, {"rule_id", "metric_id", "quantity_unit", "target_ids"}, "driver_binding")
        rule_id = _token(binding.get("rule_id"), "driver_binding.rule_id")
        mid = binding.get("metric_id")
        targets = binding.get("target_ids")
        if rule_id in drivers or not isinstance(mid, str) or mid not in outputs or not isinstance(targets, list) or not targets or len(targets) > 512:
            _error(rule_id, "invalid driver binding")
        if len({_identity(t) for t in targets}) != len(targets):
            _error(rule_id, "duplicate/aliased driver targets")
        unit = _unit(binding.get("quantity_unit"), registry, rule_id)
        weights, refs = {}, []
        for target in targets:
            target = _text(target, rule_id + ".target")
            value = outputs[mid].get(target)
            if value is None or value["quantity"] is None:
                _error(rule_id, "required driver quantity missing")
            if value["unit"] != unit["name"]:
                _error(rule_id, "driver declared unit differs from source-verified metric")
            weights[target] = value["quantity"]
            refs.extend(value["source_refs"])
        if sum(Decimal(v) for v in weights.values()) <= 0:
            _error(rule_id, "zero operational driver denominator")
        drivers[rule_id] = {"weights": weights, "unit": unit["name"], "source_refs": _unique_refs(refs)}
    return {"objects": objects, "drivers": drivers, "generated_inputs": {},
            "metadata": {"version": 1, "rule_version": version, "policy_source": policy, "scope": dict(scope),
                         "source_hashes": hashes, "unit_validation_status": "source_verified", "period_timezone": period_timezone,
                         "source_rows": {sid: len(source["records"]) for sid, source in sources.items()},
                         "metrics": outputs, "object_bindings": binding_metadata, "driver_bindings": drivers,
                         "relations": relation_results,
                         "boundary": "source_verified means uploaded rows agree with the declared unit contract only; vendor identity and enterprise approval authenticity are not verified. Reversals are metric corrections, not extra reverse-service costs.",
                         "unit_registry_status": "standard_symbols_protected;custom_quantity_kinds_declared"}}

def compile_operational_bundle(spec, files, scope, *, dataset_id="csv-operational"):
    """Use a local Decimal context; never change the caller/global precision."""
    with localcontext() as context:
        context.prec = 40
        return _compile_operational_bundle(spec, files, scope, dataset_id=dataset_id)