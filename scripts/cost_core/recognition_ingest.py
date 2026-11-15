"""Opt-in recognition compiler for original CSV records and declarative rule maps.

Use the existing mapped-CSV adapter for posting/allocation validation; do not
persist generated rows instead of the original source file.
"""
from dataclasses import replace
from decimal import Decimal
import csv
import io
from urllib.parse import urlencode

from .recognition_eval import run_document
from .models import SourceRef
from . import foreign_booked

_SOURCE_FIELDS = {
    "id", "book", "period", "currency", "kind", "status", "amount",
    "economic_key", "evidence",
}
_OPT_SOURCE_FIELDS = {"bridge_group","origin_period"}
_CONFIG = {"version", "name", "context", "rules", "outputs", "excluded_sources", "posting_map", "ledger_basis"}

def need(condition, message):
    if not condition:
        raise ValueError("recognition: " + message)

def validate_option(option):
    need(isinstance(option, dict), "invalid profile shape")
    version=option.get("version")
    need(type(version) is int and version in (1,2), "unsupported version")
    required = _CONFIG if version==1 else (_CONFIG-{"excluded_sources"}) | {"excluded_tags"}
    allowed = required | {"target_objects"} | ({foreign_booked.CONFIG} if version == 1 else set())
    need(required <= set(option) <= allowed, "invalid profile shape")
    if foreign_booked.CONFIG in option:
        need(option[foreign_booked.CONFIG] == foreign_booked.MODE, "unsupported foreign booked trace")

    need(option["ledger_basis"] == "recognized_expenses_incurred", "unsupported ledger basis")
    need(isinstance(option["context"],dict), "recognition context must be an object")
    need(isinstance(option["rules"],list) and 0<len(option["rules"])<=500, "invalid recognition rules array")
    need(isinstance(option["outputs"],list) and 0<len(option["outputs"])<=100, "invalid recognition outputs array")
    exclusion_key="excluded_sources" if version==1 else "excluded_tags"
    need(isinstance(option[exclusion_key],dict), "exclusions must be an object")
    for rule in option["rules"]:
        need(isinstance(rule,dict) and isinstance(rule.get("id"),str) and bool(rule["id"].strip()),
             "each recognition rule needs a text identifier")
    need(isinstance(option["posting_map"], dict), "posting map must be an object")
    need(isinstance(option.get("target_objects", []), list), "target_objects must be an array")

def recognize_and_compile(adapter, headers, rows, schema, source_uri, schema_uri, trusted_source_book=None):
    reserved={"trusted_source_book","trusted_authority_id","verified_source_book","source_book_manifest"}
    need(not (reserved & set(schema)), "trust and independent source manifests are not upload schema fields")
    option = schema["raw_recognition"]
    validate_option(option)
    version=option["version"]
    if version==2:
        from .recognition_fold import MAX_INPUT_EVENTS
        need(len(rows)<=MAX_INPUT_EVENTS,"v2 source event limit is 10000")
    mandatory=_SOURCE_FIELDS | ({"tag"} if version==2 else set())
    optional=_OPT_SOURCE_FIELDS | ({"target_object_id"} if version==2 else set())
    fx_on = foreign_booked.CONFIG in option
    if fx_on:
        need("currency_rates" not in schema, "foreign booked trace does not accept rates or revaluation")
        need(set(schema) <= foreign_booked.ALLOWED_SCHEMA,
             "unknown top-level field in foreign booked trace; rates or revaluation are unsupported")
        mandatory |= foreign_booked.FIELDS
    need(mandatory<=set(headers)<=mandatory|optional,"invalid source headers")
    need("columns" not in schema, "v1 requires canonical raw headers")
    need("completeness" not in schema, "posted-ledger completeness is a different mode")
    need(schema.get("scenario","actual") == "actual", "v1 only normalizes actual events")

    sources = []
    foreign_rows = []
    raw_indexes = {}; tags={}; row_metadata={}
    for index,row in enumerate(rows,2):
        need(None not in row and all(x is not None for x in row.values()), f"ragged raw record {index}")
        if fx_on:
            need(all(row[k] == row[k].strip() for k in foreign_booked.FIELDS),
                 f"noncanonical foreign booked field at raw row {index}")
        value = {k: v.strip() for k,v in row.items() if v.strip() or k in _SOURCE_FIELDS}
        if fx_on:
            foreign_rows.append(dict(value))
            for key in foreign_booked.FIELDS:
                value.pop(key,None)
        need(value["id"] not in raw_indexes, f"duplicate reference at raw row {index}")
        raw_indexes[value["id"]]=index
        if not value["amount"]:
            value["amount"]=None
        tags[value["id"]]=value.pop("tag","")
        row_metadata[value["id"]]={"target_object_id":value.pop("target_object_id","")}
        sources.append(value)
    raw = {k: option[k] for k in ("name","context","rules","outputs")}
    if version==2 and isinstance(raw["context"],dict) and raw["context"].get("period")=="from_schema":
        need("period" in schema, "batch period is required by this template")
        raw["context"]={**raw["context"],"period":schema["period"]}
    groups={}
    raw["sources"] = sources
    if version==1:
        raw["excluded_sources"]=option["excluded_sources"]
        kinds=option["posting_map"]
    else:
        from .recognition_templates import expand_template
        from .recognition_fold import fold
        posting_tags = [r.get("source_tag") for r in option["rules"]
                        if r.get("op") in ("recognize", "monthly_straight_line")]
        need(len(posting_tags) == len(set(posting_tags)),
             "Duplicate posting economic component: repeated posting source tag")
        raw["sources"],groups=fold(option,raw["context"],sources,tags,row_metadata,need)
        raw["rules"],kinds,raw["excluded_sources"]=expand_template(option,raw["sources"],tags,row_metadata,need)
    trace = foreign_booked.decode(foreign_rows, raw["context"], need) if fx_on else {}
    if version == 2:
        from .recognition_batches import requires_batching, run_bounded_document
        resolved = run_bounded_document(raw) if requires_batching(raw) else run_document(raw)
    else:
        resolved = run_document(raw)
    need(resolved["status"] == "valid", resolved.get("error","invalid source records"))
    if groups:
        from .recognition_fold import expand_result
        expand_result(resolved,groups)
    period = resolved["context"]["period"]
    currency = resolved["context"]["currency"]
    need(schema.get("period",period) == period, "outer period does not match recognition scope")
    need(schema.get("currency",currency) == currency, "outer currency does not match recognition scope")
    need(set(kinds) == {r["recognition_rule"] for r in resolved["posted_costs"]}, "posting map must name exactly posting rules")
    dataset = schema.get("dataset_id", "csv-unregistered").strip()

    def trace_ref(source_id, field="amount"):
        return SourceRef(dataset_id=dataset, source_uri=source_uri, field=field,
                         selector=urlencode({"row":raw_indexes[source_id], "id":source_id}))

    def evidence_refs(sid):
        refs = (trace_ref(sid),)
        if sid in trace:
            refs += tuple(trace_ref(sid, field) for field in sorted(foreign_booked.FIELDS))
        return refs

    # The declaration is an explicit object contract, not a second object
    # inferred from a later normalized CSV record. Apply its non-posting
    # dimensions to financial rows before the ordinary strict adapter sees them.
    declared_objects = {}
    declared_object_refs = {}
    for pos, obj in enumerate(option.get("target_objects", [])):
        mandatory = {"object_id", "object_kind"}
        extra = {"object_name", "quantity", "quantity_unit"}
        need(isinstance(obj, dict) and mandatory <= set(obj) <= mandatory | extra,
             "invalid target object declaration")
        need(all(isinstance(obj[k], str) and obj[k].strip() for k in mandatory),
             "invalid target object identifier or kind")
        need(obj["object_id"] not in declared_objects, "duplicate declared cost object")
        declared_objects[obj["object_id"]] = dict(obj)
        declared_object_refs[obj["object_id"]] = tuple(
            SourceRef(dataset_id=dataset, source_uri=schema_uri, field=field,
                      selector=f"raw_recognition.target_objects[{pos}].{field}", kind="configuration")
            for field in ("quantity", "quantity_unit")
            if obj.get(field) not in (None, "")
        )

    def posting_dimensions(spec):
        declared = declared_objects.get(spec["object_id"])
        if declared is None:
            return spec
        need(spec["object_kind"] == declared["object_kind"],
             f"posting target kind conflicts with object {spec['object_id']} declaration")
        if "object_name" in spec and "object_name" in declared:
            need(spec["object_name"] == declared["object_name"],
                 f"posting target name conflicts with object {spec['object_id']} declaration")
        fields = {k: declared[k] for k in ("quantity", "quantity_unit", "object_name")
                  if k in declared and k not in spec}
        return dict(spec, **fields)

    out = io.StringIO()
    w = csv.DictWriter(out,fieldnames=[
        "object_id","object_name","object_kind","currency","period","quantity","quantity_unit",
        "record_type","cost_element","cost_type","behavior","amount","pool_id"])
    w.writeheader()
    refs_by_entry = {}
    refs_by_pool = {}
    pool_rows = {}
    row_number = 2
    for posting in resolved["posted_costs"]:
        rid = posting["recognition_rule"]
        spec = kinds[rid]
        critical = {"object_id","object_kind","record_type","cost_element","behavior","cost_type"}
        need(isinstance(spec, dict) and critical <= set(spec) <= critical | {"object_name","pool_id"}, f"invalid map for {rid}")
        need(all(isinstance(v,str) and v.strip() for v in spec.values()), f"invalid map value for {rid}")
        record_type = spec["record_type"]
        need(record_type in ("entry","pool"), "only entries or pools can carry recognized expenses")
        need((record_type == "pool") == ("pool_id" in spec), "pool identity must be explicit")
        refs = tuple(ref for x in posting["source_ids"] for ref in evidence_refs(x))
        if record_type == "pool":
            pid=spec["pool_id"]
            prior,total=pool_rows.get(pid,(spec,Decimal(0)))
            need(prior==spec, "same mapped pool has inconsistent posting dimensions")
            pool_rows[pid]=(spec,total+Decimal(posting["amount"]))
            refs_by_pool[pid]=tuple(dict.fromkeys(refs_by_pool.get(pid,())+refs))
        else:
            refs_by_entry[f"csv-row-{row_number}"] = refs
            w.writerow(dict(posting_dimensions(spec),period=period,currency=currency,amount=posting["amount"]))
            row_number += 1
    for pid,(spec,amount) in pool_rows.items():
        w.writerow(dict(posting_dimensions(spec),period=period,currency=currency,amount=format(amount,"f")))
    for obj in declared_objects.values():
        w.writerow(dict(obj,period=period,currency=currency,record_type="object"))
    normalized_schema = dict(schema)
    normalized_schema.pop("raw_recognition")
    normalized_schema.update(period=period,currency=currency)
    compiled = adapter.from_text(out.getvalue(), normalized_schema, source_uri=source_uri, schema_uri=schema_uri)
    # The ordinary adapter numbers entries from the normalized records. Replace
    # those synthetic coordinates with original CSV evidence before returning.
    entries = tuple(replace(x,source_refs=refs_by_entry[x.entry_id]) for x in compiled.entries)
    pools = tuple(replace(x,source_refs=refs_by_pool[x.pool_id]) for x in compiled.pools)
    expected=sum(Decimal(x["amount"]) for x in resolved["posted_costs"])
    need(sum(x.amount for x in entries + pools)==expected, "recognized expense ledger does not tie")
    for sid,status in resolved["source_coverage"].items():
        status["source_refs"]=evidence_refs(sid)
    if trace:
        posted = {sid for row in resolved["posted_costs"] for sid in row["source_ids"]}
        resolved[foreign_booked.CONFIG] = {
            "basis": "upstream_declared_no_revaluation", "can_claim_independent_verification": False,
            "rows": [{**item,"recognition_status":"recognized" if sid in posted else "not_recognized",
                      "source_refs":evidence_refs(sid)} for sid,item in sorted(trace.items())],
        }
    resolved.update(ledger_basis=option["ledger_basis"],ledger_recognized_total=str(expected),profile_version=version,
                    source_recognition_status=("partial" if resolved["coverage_status"]=="partial_declared_exclusions"
                                                   else "accepted_declared_source_set"),can_claim_complete=False)
    from .recognition_authority import compare_source_population
    resolved["source_book_check"]=compare_source_population(trusted_source_book,sources,tags,row_metadata,resolved["context"])
    objects=tuple(replace(x,source_refs=declared_object_refs.get(x.object_id,())) for x in compiled.objects)
    return replace(compiled,objects=objects,entries=entries,pools=pools,recognition=resolved)
