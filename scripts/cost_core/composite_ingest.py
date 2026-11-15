"""Versioned one-file raw source orchestration, not a new costing engine."""
from dataclasses import replace
from decimal import Decimal, localcontext
import csv
import io
import re
import unicodedata
from urllib.parse import parse_qsl, urlencode

from .inventory_ingest import REQUIRED as INV_REQUIRED, OPTIONAL as INV_OPTIONAL, TRANSFER_COLUMNS
from .meter_ingest import ROW_BASE, EXTRA as METER_OPTIONAL
from .meter_eval import MAX_ROWS as MAX_METER_ROWS
from .models import SourceRef
from . import foreign_booked, category_checklist
from .object_budgets import compile_plan as compile_budgets, validate as validate_budgets
from .object_rollups import compile_plan, safe_text, validate as validate_rollups
from .recognition_ingest import _SOURCE_FIELDS as REC_REQUIRED, _OPT_SOURCE_FIELDS as REC_OPTIONAL

IDENTITY={"input_name","posting_identity"}
MODES={"raw_recognition","raw_inventory","raw_meter"}
TOP_FIELDS={"dataset_id","plugin_id","currency","period","scenario","raw_composite","industry","label"}
SCOPE_FIELDS={"book","period","currency"}
MAX_V3_BRANCHES=6
MAX_V3_METERS=4
MAX_V3_METER_BANDS=12000

def need(ok,msg):
    if not ok: raise ValueError("composite: " + msg)

def token(value):
    return isinstance(value,str) and re.fullmatch(r"[A-Za-z0-9_-]{1,40}",value) is not None

def find_mode(cfg):
    modes = set(cfg)&MODES
    need(len(modes)==1,"each branch must choose one raw mode")
    return next(iter(modes))

def validate_composite(adapter,schema):
    if isinstance(schema.get("raw_composite"),dict) and schema["raw_composite"].get("version")==4:
        from .source_bundle import validate_bundle_schema
        return validate_bundle_schema(adapter,schema)
    need(set(schema)<=TOP_FIELDS,"undeclared options, upload trust and mixed legacy modes are unsupported")
    val=schema["raw_composite"]
    need(isinstance(val,dict) and {"version","scope","inputs"}<=set(val) and type(val["version"]) is int and
         val["version"] in (1,2,3),"invalid envelope")
    required={"version","scope","inputs"}
    allowed=required|({"object_rollups","object_budgets","category_checklist"} if val["version"]==3 else set())
    need(set(val)<=allowed,"invalid envelope")
    scope=val["scope"]
    need(isinstance(scope,dict) and set(scope)==SCOPE_FIELDS and all(isinstance(v,str) and v.strip() for v in scope.values())
         and scope["currency"]=="CNY","a common declared book, period and two-decimal CNY is required")
    need(schema.get("period",scope["period"])==scope["period"] and schema.get("currency",scope["currency"])==scope["currency"]
         and schema.get("scenario","actual")=="actual","outer scope mismatch")
    sections=val["inputs"]
    version=val["version"]
    if version==3:
        need(isinstance(sections,dict) and 2<=len(sections)<=MAX_V3_BRANCHES,
             "v3 requires two to six named sources")
    else:
        need(isinstance(sections,dict) and 2<=len(sections)<=3,"exactly two or three named sources required")
    if "object_rollups" in val:
        validate_rollups(val["object_rollups"],sections)
    if "object_budgets" in val:
        validate_budgets(val["object_budgets"],scope,val.get("object_rollups"))
    if "category_checklist" in val:
        category_checklist.validate(val["category_checklist"],scope,val.get("object_rollups"))
    seen=set()
    accounts=set()
    for name,cfg in sections.items():
        need(token(name) and isinstance(cfg,dict),"invalid named source")
        mode=find_mode(cfg)
        need(not (mode == 'raw_inventory' and 'production_policy' in cfg[mode]),
             'production inventory composition requires named-file v4 for economic identity and duplicate-cost checks')
        options={"allocation_rules"} if version>=2 and mode=="raw_recognition" else set()
        need(set(cfg)<={mode}|options,"unsupported branch fields")
        if mode == "raw_recognition" and isinstance(cfg[mode],dict) and foreign_booked.CONFIG in cfg[mode]:
            need(version==3,"foreign booked trace requires v3 composite")
        independent_meter=version==3 and mode=="raw_meter"
        need(independent_meter or mode not in seen,"mode repeated")
        seen.add(mode)
        adapter.validate_schema({**cfg,"period":scope["period"],"currency":scope["currency"]})
        if independent_meter:
            bill=cfg[mode]["scope"]
            account=bill["billing_account"]
            need(type(account) is str and 0<len(account)<=128 and account==account.strip(),
                 "invalid meter account")
            need(not any(unicodedata.category(char) in {"Cc","Cf"} for char in account),
                 "meter account contains control or formatting characters")
            need(bill["book"]==scope["book"] and bill["currency"]==scope["currency"]
                 and bill["period"] in ("from_schema",scope["period"]),"meter branch scope mismatch")
            identity=unicodedata.normalize("NFKC",account).casefold().strip()
            need(identity and identity not in accounts,"duplicate meter billing account")
            accounts.add(identity)
            need(len(accounts)<=MAX_V3_METERS,"too many meter branches")

def mode_fields(mode,cfg):
    if mode=="raw_meter": return ROW_BASE,METER_OPTIONAL
    if mode=="raw_inventory":
        optional = INV_OPTIONAL | (TRANSFER_COLUMNS if "transfer_policy" in cfg[mode] else set())
        if "production_policy" in cfg[mode]:
            optional |= {"production_object_id", "economic_key"}
        return INV_REQUIRED, optional
    if cfg[mode]["version"]==2:
        return REC_REQUIRED|{"tag"},REC_OPTIONAL|{"target_object_id"}
    return REC_REQUIRED,REC_OPTIONAL|(foreign_booked.FIELDS if foreign_booked.CONFIG in cfg[mode] else set())

def real_refs(value,row_map,name,mode,source_uri,schema_uri):
    if isinstance(value,SourceRef):
        if value.source_uri==source_uri:
            filters=dict(parse_qsl(value.selector,keep_blank_values=True))
            need(str(filters.get("row","")).isdigit() and int(filters["row"]) in row_map,
                 "sub-ledger produced an unmappable original row reference")
            filters["row"]=row_map[int(filters["row"])]
            filters["input_name"]=name
            return replace(value,selector=urlencode(filters))
        if value.source_uri==schema_uri:
            prefix=mode+"."
            need(value.selector.startswith(prefix) or re.fullmatch(r"allocation_rules\[\d{1,4}\](?:\.weight_factors)?",value.selector),
                 "unscoped source configuration reference")
            return replace(value,selector=f"raw_composite.inputs.{name}.{value.selector}")
        return value
    if isinstance(value,dict):
        return {k:real_refs(v,row_map,name,mode,source_uri,schema_uri) for k,v in value.items()}
    if isinstance(value,(tuple,list)):
        return type(value)(real_refs(v,row_map,name,mode,source_uri,schema_uri) for v in value)
    return value

def source_collision_key(value, limit, row_number):
    # For duplicate detection, not for matching original CSV evidence or
    # changing the economic identity the source supplied.
    need(safe_text(value,limit),f"invalid v3 source identity at row {row_number}")
    normalized=unicodedata.normalize("NFKC",unicodedata.normalize("NFKC",value).casefold())
    need(safe_text(normalized,2*limit),f"invalid canonical v3 source identity at row {row_number}")
    return normalized


def compile_composite(adapter,headers,rows,schema,source_uri,schema_uri):
    need(schema["raw_composite"]["version"] != 4,"v4 requires named files via from_files")
    envelope=schema["raw_composite"]; scope=envelope["scope"]; sections=envelope["inputs"]
    allowed=IDENTITY|set().union(*(req|optional for req,optional in
                                  (mode_fields(find_mode(c),c) for c in sections.values())))
    need(IDENTITY<=set(headers)<=allowed,"invalid combined CSV headers")
    need(len(rows)<=15000,"combined source row limit")
    ids=set(); native_ids=set(); groups={k:[] for k in sections}
    v3=envelope["version"]==3
    posting_collision_keys=set(); native_collision_keys=set()
    for pos,row in enumerate(rows,2):
        need(None not in row and all(v is not None for v in row.values()),f"ragged source row {pos}")
        inp,ident=(row[k].strip() for k in ("input_name","posting_identity"))
        need(inp in sections,f"undeclared input at row {pos}")
        need(ident and len(ident)<=128 and ident not in ids,f"blank or duplicate posting identity at row {pos}")
        ids.add(ident)
        cfg=sections[inp]; mode=find_mode(cfg)
        native=(row.get("source_id","") if mode=="raw_inventory" else row.get("id","")).strip()
        need(native and native not in native_ids,f"blank or duplicate original source ID at row {pos}")
        native_ids.add(native)
        if v3:
            post_key=source_collision_key(ident,128,pos)
            native_key=source_collision_key(native,4096,pos)
            need(post_key not in posting_collision_keys,f"ambiguous aliased posting identity at row {pos}")
            need(native_key not in native_collision_keys,f"ambiguous aliased original source ID at row {pos}")
            posting_collision_keys.add(post_key)
            native_collision_keys.add(native_key)
        required,optional=mode_fields(mode,cfg)
        need(required<=set(row),f"missing source columns at row {pos}")
        need(not any(v.strip() for k,v in row.items() if k not in required|optional|IDENTITY),
             f"unexpected source data at row {pos}")
        groups[inp].append((pos,{k:v for k,v in row.items() if k in required|optional}))
    # A foreign original cannot alias any native, economic or posting identity.
    foreign_ids = [item["fx_original_id"] for branch in groups.values() for _,item in branch
                   if item.get("fx_original_id")]
    if foreign_ids:
        economic_ids = {row["economic_key"].strip() for branch in groups.values() for _,row in branch
                        if row.get("economic_key","").strip()}
        native_keys = {foreign_booked.source_key(x) for x in native_ids|ids|economic_ids}
        need(not native_keys.intersection(foreign_booked.source_key(x) for x in foreign_ids),
             "foreign original aliases an existing source or posting identity")
    if envelope["version"]==3:
        # Reject aggregate expansion before evaluating any individual tariff.
        # Count both declared account-wide/bill groups and meter-wide groups.
        meter_rows=0
        meter_bands=0
        for name,group in groups.items():
            if "raw_meter" not in sections[name]: continue
            meter_rows+=len(group)
            profile=sections[name]["raw_meter"]["profile"]
            tiers=profile.get("tiers")
            split=profile.get("tier_scope")
            need(isinstance(tiers,list) and split in ("meter","billing_account"),"invalid tariff form")
            count=(len({r.get("meter","").strip() for _,r in group if r.get("kind","").strip()=="usage"})
                   if split=="meter" else 1)
            meter_bands+=count*len(tiers)
        need(meter_rows<=MAX_METER_ROWS,"aggregate meter record limit")
        need(meter_bands<=MAX_V3_METER_BANDS,"aggregate meter band evidence limit")
    objs={}; entries=[]; pools=[]; rules=[]; drivers=[]; audits={}; result_base=None; pending=[]
    rollups=envelope.get("object_rollups")
    owners={}
    allow_allocation=envelope["version"]>=2
    with localcontext() as ctx:
        ctx.prec=60
        for name,group in sorted(groups.items()):
            need(group,f"declared input {name} has no original rows")
            cfg=sections[name];mode=find_mode(cfg)
            req,_=mode_fields(mode,cfg)
            used = req | {k for _,m in group for k,v in m.items() if v.strip()}
            out=io.StringIO()
            w=csv.DictWriter(out,fieldnames=sorted(used),extrasaction="ignore")
            w.writeheader();w.writerows(v for _,v in group)
            inner={k:v for k,v in schema.items() if k in {"dataset_id","plugin_id"}}
            inner.update(period=scope["period"],currency=scope["currency"],**cfg)
            result=adapter.from_text(out.getvalue(),inner,source_uri=source_uri,schema_uri=schema_uri)
            audits_block=result.recognition
            actual=(audits_block.get("context") if mode=="raw_recognition" else
                    audits_block["inventory_valuation"]["context"] if mode=="raw_inventory" else
                    audits_block["meter_billing"]["scope"])
            need(all(actual.get(k)==v for k,v in scope.items()),f"book, period or currency mismatch for {name}")
            need(allow_allocation or not(result.pools or result.rules or result.drivers),
                 "v1 supports direct entries and objects, not independently allocated subledgers")
            need(not (result.benchmarks or
                      result.forecast or result.standard_costs or result.currency_rates or result.unit_conversions or
                      result.reported_totals or result.reported_unit_costs or result.coverage or result.preflight_quality),
                 "unsupported non-ledger subledger parameters")
            row_map={n:pos for n,(pos,_) in enumerate(group,2)}
            target_ids={x.object_id for x in result.objects}
            branch_allocated=bool(result.pools or result.rules or result.drivers)
            for obj in result.objects:
                actual_obj=replace(obj,source_refs=real_refs(obj.source_refs,row_map,name,mode,source_uri,schema_uri))
                prior=objs.get(obj.object_id)
                need(prior is None or (not allow_allocation and prior==actual_obj),
                     "composed allocations require disjoint object IDs across branches")
                objs[obj.object_id]=actual_obj
                if rollups is not None: owners[obj.object_id]=name
            if branch_allocated:
                branch_pool_ids={x.pool_id for x in result.pools}
                branch_driver_ids={x.driver_id for x in result.drivers}
                need(branch_pool_ids=={x.pool_id for x in result.rules} and len(result.rules)==len(branch_pool_ids),
                     "exactly one explicit rule required for each branch pool")
                for p in result.pools:
                    need(p.owner_id in target_ids and not p.include_incoming,"branch pool owner or incoming pool invalid")
                    pools.append(replace(p,pool_id=f"{name}:{p.pool_id}",
                                   source_refs=real_refs(p.source_refs,row_map,name,mode,source_uri,schema_uri)))
                for d in result.drivers:
                    need(set(d.values)<=target_ids,"driver references object outside own branch")
                    drivers.append(replace(d,driver_id=f"{name}:{d.driver_id}",
                                     source_refs=real_refs(d.source_refs,row_map,name,mode,source_uri,schema_uri)))
                for r in result.rules:
                    need(set(r.target_ids)<=target_ids and r.stage==1 and not r.allow_partial
                         and (not r.driver_id or r.driver_id in branch_driver_ids),"rule must stay within branch and be single-stage")
                    rules.append(replace(r,rule_id=f"{name}:{r.rule_id}",pool_id=f"{name}:{r.pool_id}",
                                         driver_id=f"{name}:{r.driver_id}" if r.driver_id else ""))

            for e in result.entries:
                refs=real_refs(e.source_refs,row_map,name,mode,source_uri,schema_uri)
                entries.append(replace(e,entry_id=f"{name}:{e.entry_id}",source_refs=refs))
            block=real_refs(audits_block,row_map,name,mode,source_uri,schema_uri)
            audits[name]=block
            pending.extend({**item,"input_name":name} for item in block["unrecognized_nonzero_sources"])
            if result_base is None: result_base=result
        total=sum((e.amount for e in entries + pools),Decimal(0))
        blocks_total=sum((Decimal(str(v["ledger_recognized_total"])) for v in audits.values()),Decimal(0))
        need(total==blocks_total,"input and combined ledgers do not tie")
    plan=(compile_plan(rollups,owners,{p.owner_id for p in pools},schema.get("dataset_id","csv-unregistered"),schema_uri)
          if rollups is not None else None)
    budgets=(compile_budgets(envelope["object_budgets"],scope,rollups,
                             schema.get("dataset_id","csv-unregistered"),schema_uri)
             if "object_budgets" in envelope else None)
    checklist=(category_checklist.compile_plan(envelope["category_checklist"],scope,rollups,
                 schema.get("dataset_id","csv-unregistered"),schema_uri)
               if "category_checklist" in envelope else None)
    return replace(result_base,objects=tuple(objs.values()),entries=tuple(entries),object_rollup_plan=plan,
                   object_budget_plan=budgets,category_checklist_plan=checklist,
                   pools=tuple(pools),rules=tuple(rules),drivers=tuple(drivers),recognition={
        "operation":"raw_composite","context":dict(scope),"inputs":audits,"can_claim_complete":False,
        **({"composite_version":3} if envelope["version"]==3 else {}),
        "ledger_recognized_total":str(total),"source_recognition_status":
            "partial" if any(x["source_recognition_status"]=="partial" for x in audits.values()) else "accepted_declared_source_set",
        "unrecognized_nonzero_sources":pending,"memo_sources":[],"derived_views":{},
        "posted_pool_totals":{k:v["ledger_recognized_total"] for k,v in audits.items()},
    })
