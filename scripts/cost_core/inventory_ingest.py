"""Compile bounded raw inventory movement CSV into the ordinary costing ledger."""
from dataclasses import replace
from decimal import Decimal
from urllib.parse import urlencode
import csv
import io
import unicodedata
from .inventory_eval import IDENTITY, Invalid, PRODUCTION_POLICY, instant, run_document
from .models import SourceRef
from .inventory_transfer import TRANSFER_POLICY, check_history, run_grouped

REQUIRED=IDENTITY | {"id","source_id","at","sequence","kind","quantity","evidence"}
OPTIONAL={"receipt_value","original_issue_id","restock_status"}
TRANSFER_COLUMNS={"transfer_id"}
PRODUCTION_COLUMNS={"production_object_id","economic_key"}

def need(ok, msg):
    if not ok:
        raise ValueError("inventory: " + msg)

STREAM_DIMENSIONS=("warehouse","item","uom")
MAX_STREAMS=64
MAX_BATCH_EVENTS=5000
SORT_CHRONOLOGY="sort_by_utc_timestamp_then_sequence"

def validate_map(spec):
    need(isinstance(spec,dict) and set(spec)=={"cogs","shrink"}, "cogs and shrink require explicit mapping")
    want={"object_id","object_kind","cost_element","cost_type","behavior"}
    need(all(isinstance(x,dict) and want<=set(x)<=want|{"object_name"}
             and all(isinstance(v,str) and v.strip() for v in x.values()) for x in spec.values()),
         "invalid posting target")


def validate_production_map(spec):
    need(isinstance(spec,dict) and len(spec)<=128,"invalid production posting map")
    required={"object_kind","cost_element","cost_type","behavior"}
    need(all(isinstance(batch,str) and 0<len(batch)<=256 and batch==batch.strip()
             and isinstance(item,dict) and required<=set(item)<=required|{"object_name"}
             and all(isinstance(value,str) and value.strip() for value in item.values())
             for batch,item in spec.items()),"production map requires batch and explicit cost dimensions")

def stream_key(data):
    return tuple(data.get(x) for x in STREAM_DIMENSIONS)

def validate_inventory(schema):
    op=schema["raw_inventory"]
    need(isinstance(op,dict), "invalid schema fields")
    multi="streams" in op
    expect={"operation","policy","context"}|({"streams"} if multi else {"opening","posting_map"})
    if "transfer_policy" in op:
        need(op["transfer_policy"]==TRANSFER_POLICY and multi and op.get("chronology")==SORT_CHRONOLOGY,
             "transfers require the explicit policy, multiple streams and deterministic chronology")
        expect.update(("transfer_policy","transfer_history"))
        try:
            check_history(op.get("transfer_history"))
        except Invalid as exc:
            raise ValueError("inventory: " + str(exc)) from exc
    if "chronology" in op:
        need(op["chronology"]==SORT_CHRONOLOGY, "unsupported movement chronology")
        expect.add("chronology")
    production="production_policy" in op
    if production:
        need(op["production_policy"]==PRODUCTION_POLICY,"unsupported production inventory policy")
        expect.add("production_policy")
        if not multi: expect.add("production_posting_map")
    need(set(op)==expect, "invalid schema fields")
    need(not ({"columns","raw_recognition","completeness","trusted_source_book","trusted_authority_id",
               "verified_source_book","source_book_manifest"} & set(schema)), "mutually exclusive or protected fields")
    need(schema.get("scenario","actual")=="actual","actual movement only")
    need(isinstance(op["context"],dict) and op["context"].get("currency")=="CNY",
         "this valuation version uses two-decimal CNY only")
    if not multi:
        validate_map(op["posting_map"])
        if production:
            validate_production_map(op["production_posting_map"])
            need(op["production_posting_map"],"production posting map must declare at least one batch")
        return
    need(set(op["context"])=={"book","period","currency","cutoff_at"}, "invalid shared context")
    min_streams=2 if "transfer_policy" in op else 1
    need(isinstance(op["streams"],list) and min_streams<=len(op["streams"])<=MAX_STREAMS,
         "invalid or oversized inventory streams")
    keys=set(); claimed_ids=set(); declared_production=False
    for stream in op["streams"]:
        required={"scope","opening","posting_map"}
        need(isinstance(stream,dict) and required<=set(stream)<=required|({"production_posting_map"} if production else set()),
             "invalid stream declaration")
        scope=stream["scope"]
        need(isinstance(scope,dict) and set(scope)==set(STREAM_DIMENSIONS)
             and all(isinstance(x,str) and x.strip() for x in scope.values()), "invalid stream scope")
        key=stream_key(scope)
        need(key not in keys,"duplicate inventory scope")
        keys.add(key)
        validate_map(stream["posting_map"])
        if "production_posting_map" in stream:
            validate_production_map(stream["production_posting_map"])
            declared_production = declared_production or bool(stream["production_posting_map"])
        ids={s["object_id"] for s in stream["posting_map"].values()}
        # Production mappings can feed the same batch from separate raw item
        # streams. Existing sale and shrink scopes still have disjoint owners.
        need(not(ids & claimed_ids),"different inventory scopes cannot share a sale/shrink posting object")
        claimed_ids.update(ids)
    need(not production or declared_production,"production posting map must declare at least one batch")

def resolve_groups(op, items):
    if "streams" not in op:
        doc={k:v for k,v in op.items() if k not in {"posting_map","production_posting_map","chronology"}}
        yield 0,op["posting_map"],{**doc,"transactions":items}
        return
    groups={stream_key(s["scope"]):[] for s in op["streams"]}
    for move in items:
        key=stream_key(move)
        need(key in groups,"movement without declared warehouse/item/unit scope")
        groups[key].append(move)
    claimed_events=set(); claimed_sources=set()
    for j,stream in enumerate(op["streams"]):
        context={**op["context"],**stream["scope"]}
        opening=stream["opening"]
        need(isinstance(opening,dict),"missing stream opening")
        moves=groups[stream_key(context)]
        for seen_field,live_field,claimed in (("seen_event_ids","id",claimed_events),
                                              ("seen_source_ids","source_id",claimed_sources)):
            history=opening.get(seen_field)
            need(isinstance(history,list) and len(history)<=2000 and all(isinstance(v,str) for v in history),
                 "invalid opening identity history")
            declared=set(history)|{x[live_field] for x in moves}
            need(not (claimed & declared), "event or source ID reused across inventory scopes")
            claimed.update(declared)
        yield j,stream["posting_map"],{
            "operation":op["operation"],"policy":op["policy"],"context":context,
            "opening":opening,"transactions":moves,
            **({"production_policy":op["production_policy"]} if "production_policy" in op else {})}

def compile_inventory(adapter,headers,rows,schema,source_uri,schema_uri):
    from .inventory_eval import MAX_EVENTS, MAX_BYTES
    op=schema["raw_inventory"]
    enabled="transfer_policy" in op
    production="production_policy" in op
    columns=OPTIONAL|(TRANSFER_COLUMNS if enabled else set())|(PRODUCTION_COLUMNS if production else set())
    need(REQUIRED<=set(headers)<=REQUIRED|columns, "invalid movement headers")
    multi="streams" in op
    need(len(rows)<=(MAX_BATCH_EVENTS if multi else MAX_EVENTS) and sum(len(v.encode("utf-8")) for r in rows if None not in r
                                       for v in r.values() if v is not None)<=MAX_BYTES, "movement CSV limit exceeded")
    items=[]
    line_by_source={}
    for index,row in enumerate(rows,2):
        need(None not in row and not any(v is None for v in row.values()),f"ragged movement row {index}")
        move={k:v.strip() for k,v in row.items() if k in REQUIRED or v.strip()}
        seq=move["sequence"]
        need(seq.isascii() and seq.isdigit() and len(seq)<=7, f"invalid movement sequence at row {index}")
        move["sequence"]=int(seq)
        line_by_source[move["source_id"]]=index
        items.append(move)
    period,currency=op["context"].get("period"),op["context"].get("currency")
    need(schema.get("period",period)==period and schema.get("currency",currency)==currency,"outer scope mismatch")
    dataset=schema.get("dataset_id","csv-unregistered")
    def ref(sid,field="quantity"):
        return SourceRef(dataset_id=dataset,source_uri=source_uri,field=field,
                         selector=urlencode({"row":line_by_source[sid],"source_id":sid}))
    ledger=io.StringIO()
    writer=csv.DictWriter(ledger,fieldnames=["object_id","object_kind","object_name","cost_element","cost_type",
                                             "behavior","period","currency","amount","record_type"])
    writer.writeheader()
    valuations=[]; entry_refs={}; dimensions={}; row_number=2
    economic_keys=set()
    metrics=("available_value","net_cogs","shrink","closing_value","reconciliation_difference")
    if production: metrics += ("net_production_consumption",)
    amounts=dict.fromkeys(metrics,Decimal(0))
    reorder=op.get("chronology")==SORT_CHRONOLOGY
    reordered=0
    documents=[]
    def policy_ref(selector):
        return SourceRef(dataset_id=dataset,source_uri=schema_uri,field="raw_inventory",selector=selector,kind="configuration")

    def declare_object(spec):
        identity={k:spec[k] for k in ("object_id","object_kind")}
        if "object_name" in spec:
            identity["object_name"]=spec["object_name"]
        normalized={**identity,"object_name":identity.get("object_name",identity["object_id"])}
        prior=dimensions.setdefault(identity["object_id"],normalized)
        need(prior==normalized,"inconsistent object declaration in posting mapping")

    for pos,posting_map,document in resolve_groups(op,items):
        if reorder:
            original=document["transactions"]
            try:
                ordered=sorted(original,key=lambda x:(instant(x["at"]),x["sequence"]))
            except Invalid as exc:
                raise ValueError("inventory: " + str(exc)) from exc
            reordered+=sum(old is not new for old,new in zip(original,ordered))
            document={**document,"transactions":ordered}
        documents.append((pos,posting_map,document))
    if enabled:
        try:
            transfers, proofs, history=run_grouped(documents,op["transfer_history"])
        except Invalid as exc:
            raise ValueError("inventory: " + str(exc)) from exc
        pair_links={}
        for proof in proofs:
            a,b=(proof[k] for k in ("dispatch_source_id","receipt_source_id"))
            proof["source_refs"]=(ref(a,"transfer_id"),ref(b,"transfer_id"))
            pair_links[a]=pair_links[b]=proof["source_refs"]
    for pos,posting_map,document in documents:
        result=transfers[pos] if enabled else run_document(document)
        need(result["status"]=="valid",result.get("error","invalid inventory"))
        production_map=(op["streams"][pos] if multi else op).get("production_posting_map",{})
        if production_map:
            selector=(f"raw_inventory.streams[{pos}].production_posting_map" if multi else "raw_inventory.production_posting_map")
            production_map_ref=policy_ref(selector)
            for object_id, spec in production_map.items():
                declare_object({**spec,"object_id":object_id})
        for move in result["stock_movements"]:
            sid=move["source_id"]
            move["source_refs"]=pair_links[sid] if enabled and sid in pair_links else (ref(sid),)
        selector=f"raw_inventory.streams[{pos}].opening" if multi else "raw_inventory.opening"
        result["opening_source_ref"]=SourceRef(dataset_id=dataset,source_uri=schema_uri,
                                                field="raw_inventory",selector=selector)
        for posting in result["cost_postings"]:
            category=posting["category"]
            if category=="production_consumption":
                object_id=posting["production_object_id"]
                need(object_id in production_map,f"undeclared production batch {object_id} at source {posting['source_id']}")
                key=unicodedata.normalize("NFKC",unicodedata.normalize("NFKC",posting["economic_key"]).casefold()).strip()
                need(key not in economic_keys,f"duplicate production economic identity at source {posting['source_id']}")
                economic_keys.add(key)
                spec={**production_map[object_id],"object_id":object_id}
                posting["cost_element"]=spec["cost_element"]
            else:
                spec=posting_map[category]
            writer.writerow(dict(spec,period=period,currency=currency,
                                 amount=posting["amount"],record_type="entry"))
            posting_refs=[ref(posting["source_id"])]
            if posting.get("original_source_id") in line_by_source:
                posting_refs.append(ref(posting["original_source_id"]))
            if category=="production_consumption":
                posting_refs.extend((ref(posting["source_id"],"production_object_id"),production_map_ref))
                posting["source_refs"]=tuple(posting_refs)
            entry_refs[f"csv-row-{row_number}"]=tuple(posting_refs)
            row_number+=1
        for x in posting_map.values():
            if production:
                declare_object(x)
            else:
                identity={k:x[k] for k in ("object_id","object_kind")}
                prior=dimensions.setdefault(x["object_id"],identity)
                need(prior==identity,"inconsistent object declaration in posting mapping")
        for k in metrics:
            amounts[k]+=Decimal(result["outputs"][k])
        valuations.append(result)
    posted_objects={x["object_id"] for x in csv.DictReader(io.StringIO(ledger.getvalue()))}
    for identity in dimensions.values():
        if identity["object_id"] not in posted_objects:
            writer.writerow(dict(identity,period=period,currency=currency,record_type="object"))
    result=valuations[0] if not multi else {
        "operation":op["operation"],"context":op["context"],"scope":"declared_separate_inventory_streams",
        "can_claim_complete":False,"outputs":{k:format(v,"f") for k,v in amounts.items()},
        "streams":valuations,
    }
    if reorder:
        result["chronology"]={
            "order":SORT_CHRONOLOGY,
            "events_at_changed_stream_position":reordered,
            "policy_ref":SourceRef(dataset_id=dataset,source_uri=schema_uri,
                                   field="raw_inventory",selector="raw_inventory.chronology"),
        }
    if enabled:
        inc=sum(Decimal(v["outputs"]["transfer_in"]) for v in valuations)
        out=sum(Decimal(v["outputs"]["transfer_out"]) for v in valuations)
        moved=sum(Decimal(x["amount"]) for x in proofs)
        need(inc==out==moved and amounts["available_value"]==
             amounts["net_cogs"]+amounts["shrink"]+amounts["closing_value"]+amounts.get("net_production_consumption",Decimal(0)),
             "consolidated transfer asset and expense do not tie")
        policy_ref=lambda k: SourceRef(dataset_id=dataset,source_uri=schema_uri,field="raw_inventory",
                                       selector="raw_inventory."+k)
        result["transfers"]={"policy":TRANSFER_POLICY,"closed_value":format(moved,".2f"),
                             "closed_pairs":proofs,"next_transfer_history":history,
                             "policy_ref":policy_ref("transfer_policy"),"history_ref":policy_ref("transfer_history")}
    normal={k:v for k,v in schema.items() if k!="raw_inventory"}
    normal.update(period=period,currency=currency)
    compiled=adapter.from_text(ledger.getvalue(),normal,source_uri=source_uri,schema_uri=schema_uri)
    entries=tuple(replace(x,source_refs=entry_refs[x.entry_id]) for x in compiled.entries)
    consumption=Decimal(result["outputs"].get("net_production_consumption","0.00"))
    total=Decimal(result["outputs"]["net_cogs"])+Decimal(result["outputs"]["shrink"])+consumption
    need(sum(x.amount for x in entries)==total,"valuation and ordinary ledger do not tie")
    if production:
        result["production_policy"]={"id":op["production_policy"],"policy_ref":
                                    SourceRef(dataset_id=dataset,source_uri=schema_uri,field="raw_inventory",
                                    selector="raw_inventory.production_policy",kind="configuration")}
    # Supply the existing snapshot contract, keeping the movement evidence and state separately typed.
    return replace(compiled,entries=entries,recognition={
        "operation":result["operation"],"inventory_valuation":result,"can_claim_complete":False,
        "ledger_recognized_total":str(total),"source_recognition_status":"accepted_declared_source_set",
        "unrecognized_nonzero_sources":[],"memo_sources":[],"derived_views":{},
        "posted_pool_totals":{"cogs":result["outputs"]["net_cogs"],"shrink":result["outputs"]["shrink"],
                              **({"production_consumption":result["outputs"]["net_production_consumption"]}
                                 if production else {})},
    })
