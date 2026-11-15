"""Normalize a finite declarative tariff against real CSV source usage into V3 postings."""
from dataclasses import replace
from decimal import localcontext
from urllib.parse import urlencode
import csv
import io
from .meter_eval import recognize, SCOPES, ROW_BASE, MAX_ROWS
from .models import SourceRef

POSTINGS={"metered_usage","fixed_fee","vendor_credit"}
EXTRA={"qty","unit","meter","amount","origin_period"}

def need(ok,msg):
    if not ok: raise ValueError("meter: " + msg)

def validate_meter(schema):
    option=schema["raw_meter"]
    need(isinstance(option,dict) and set(option)=={"profile","scope","posting_map"}, "invalid schema fields")
    need(not ({"columns","raw_recognition","raw_inventory","completeness","trusted_source_book",
               "trusted_authority_id","verified_source_book","source_book_manifest"} & set(schema)),
         "mutually exclusive or protected fields")
    need(schema.get("scenario","actual")=="actual","only actual metered usage accepted")
    profile=option["profile"]
    need(isinstance(profile,dict) and profile.get("currency")=="CNY" and profile.get("currency_scale")==2,
         "currency limited to two-decimal CNY")
    scope=option["scope"]
    need(isinstance(scope,dict) and set(scope)==SCOPES, "invalid scope")
    targets=option["posting_map"]
    want={"object_id","object_kind","cost_element","cost_type","behavior"}
    need(isinstance(targets,dict) and set(targets)==POSTINGS and
         all(isinstance(x,dict) and want<=set(x)<=want|{"object_name"} and
             all(isinstance(v,str) and v.strip() for v in x.values()) for x in targets.values()),
         "invalid posting map")
    need(len({t["cost_element"] for t in targets.values()})==3,"quantity, fixed basis and credit require distinct cost elements")

def compile_meter(adapter,headers,rows,schema,source_uri,schema_uri):
    option=schema["raw_meter"];profile=option["profile"]
    scope=dict(option["scope"])
    if scope["period"]=="from_schema": scope["period"]=schema.get("period")
    period,currency=scope["period"],scope["currency"]
    need(schema.get("period",period)==period and schema.get("currency",currency)==currency,"outer scope mismatch")
    need(ROW_BASE<=set(headers)<=ROW_BASE|EXTRA,"invalid canonical columns")
    need(len(rows)<=MAX_ROWS,"invalid record count")
    refs={}; items=[]
    for pos,row in enumerate(rows,2):
        need(None not in row and not any(v is None for v in row.values()), f"ragged meter row {pos}")
        item={k:v.strip() for k,v in row.items() if k in ROW_BASE or v.strip()}
        refs[item["id"]]=pos; items.append(item)
    tiers=profile.get("tiers")
    need(isinstance(tiers,list) and profile.get("tier_scope") in ("meter","billing_account"),"invalid tariff form")
    group_bound=(len({r.get("meter") for r in items if r["kind"]=="usage"}) if profile["tier_scope"]=="meter" else 1)
    need(group_bound*len(tiers)<=12000,"meter band evidence exceeds this batch limit")
    billed=recognize(profile,scope,items)
    dataset=schema.get("dataset_id","csv-unregistered")
    def ref(sid,field):
        return SourceRef(dataset_id=dataset,source_uri=source_uri,field=field,
                         selector=urlencode({"row":refs[sid],"id":sid}))
    billed["profile_ref"]=SourceRef(dataset_id=dataset,source_uri=schema_uri,field="raw_meter",selector="raw_meter.profile")
    for group in billed["groups"].values():
        for entry in group["events"]:
            entry["source_refs"]=(ref(entry["source_id"],"qty"),)
    for cat in ("basis_events","credit_events"):
        for entry in billed[cat]:
            entry["source_refs"]=(ref(entry["source_id"],"amount"),)
    with localcontext() as ctx:
        ctx.prec=60
        usage = billed["reported_net"] - billed["fixed"] - billed["credit"]
        billed["rounding_adjustment"]=usage-billed["raw_variable"]
    components=[
        ("metered_usage",usage,tuple(x for d in billed["groups"].values() for e in d["events"] for x in e["source_refs"])),
        ("fixed_fee",billed["fixed"],tuple(x for e in billed["basis_events"] for x in e["source_refs"])),
        ("vendor_credit",billed["credit"],tuple(x for e in billed["credit_events"] for x in e["source_refs"])),
    ]
    ledger=io.StringIO()
    writer=csv.DictWriter(ledger,fieldnames=["record_type","object_id","object_kind","object_name","period","currency",
                                             "amount","cost_element","cost_type","behavior"])
    writer.writeheader()
    entry_refs={}
    for cat,amount,trace in components:
        if not trace: continue
        target=option["posting_map"][cat]
        writer.writerow({**target,"record_type":"entry","amount":str(amount),"period":period,"currency":currency})
        entry_refs[f"csv-row-{len(entry_refs)+2}"]=trace
    normal={k:v for k,v in schema.items() if k!="raw_meter"}
    normal.update(period=period,currency=currency)
    compile_result=adapter.from_text(ledger.getvalue(),normal,source_uri=source_uri,schema_uri=schema_uri)
    entries=tuple(replace(e,source_refs=entry_refs[e.entry_id]) for e in compile_result.entries)
    with localcontext() as ctx:
        ctx.prec=60
        need(sum(x.amount for x in entries)==billed["reported_net"],"metered postings do not reconcile")
    return replace(compile_result,entries=entries,recognition={
        "operation":"tiered_meter","meter_billing":billed,"can_claim_complete":False,
        "ledger_recognized_total":billed["reported_net"],"source_recognition_status":"accepted_declared_source_set",
        "unrecognized_nonzero_sources":[],"memo_sources":[],"derived_views":{},
        "posted_pool_totals":{cat:amount for cat,amount,_ in components},
    })
