"""Bounded linear source fold (v2). Never aggregate schedules or balances."""
from collections import defaultdict
from decimal import Decimal, localcontext
import hashlib, json
from .recognition_eval import load, Status

MAX_INPUT_EVENTS = 10000
FOLD_FROM = 32
BUCKET = "__linear_fold__/"

def fold(option, context, sources, tags, row_metadata, need):
    need(len(sources)<=MAX_INPUT_EVENTS,"v2 source event limit is 10000")
    need(all(not x["id"].startswith(BUCKET) for x in sources),"reserved internal source identifier")
    repeated={}
    for t in option["rules"]:
        if isinstance(t,dict) and t.get("op") in {"recognize","monthly_straight_line"} and isinstance(t.get("source_tag"),str):
            repeated.setdefault(t["source_tag"],[]).append(t)
    eligible={k:rules[0] for k,rules in repeated.items()
              if len(rules)==1 and rules[0]["op"]=="recognize" and k not in option["excluded_tags"]}
    buckets=defaultdict(list)
    for src in sources:
        tag=tags[src["id"]]
        if tag in eligible:
            m=option["posting_map"].get(eligible[tag]["id"],{})
            need(isinstance(m,dict),"invalid posting_map")
            target=row_metadata[src["id"]].get("target_object_id","") if "object_id_from" in m else ""
            buckets[tag,target].append(src)
    large={k:v for k,v in buckets.items() if len(v)>FOLD_FROM}
    if not large:
        return sources,{}

    # Use the original reference's strict leaf decoder without widening its DAG
    # limits. No availability/scope or source identity is inferred from a sum.
    parsed={}
    for start in range(0,len(sources),256):
        sub=load(dict(name="source preflight",context=context,sources=sources[start:start+256],
             rules=[dict(id="preflight_unused",op="sum",inputs=["preflight_absent"])],
             outputs=["preflight_unused"],excluded_sources={}))
        for src in sub.sources:
            need((src.book,src.period,src.currency)==(sub.context.book,sub.context.period,sub.context.currency),
                 f"cross-book, period or currency source {src.id}")
            need(src.status!=Status.MISSING,f"missing source {src.id}")
            parsed[src.id]=src
    # A repeated economic key among distinct posting sources cannot disappear
    # when their records are replaced by a single technical accumulator.
    used_economic={}
    for src in sources:
        sid=src["id"]
        if tags[sid] in repeated:
            key=src["economic_key"]
            need(key not in used_economic, f"duplicate economic source {key}")
            used_economic[key]=sid

    grouped={}; omitted=set()
    for (tag,target), members in sorted(large.items()):
        rule=eligible[tag]
        members.sort(key=lambda x:x["id"])
        for src in members:
            p=parsed[src["id"]]
            need(src["kind"]==rule.get("source_kind"),f"source {p.id} has wrong kind for tag")
            need(p.status in (Status.DATA,Status.ZERO) and p.amount is not None,f"source {p.id} is not a recognized amount")
            need(src["kind"] in ("expense","accrual","credit"),"only direct monetary expense/accrual/credit can fold")
        token=hashlib.sha256(json.dumps([tag,target],ensure_ascii=False).encode()).hexdigest()[:32]
        identity=BUCKET+token
        with localcontext() as c:
            c.prec=60
            total=sum(parsed[x["id"]].amount for x in members)
        grouped[identity]=members
        omitted.update(x["id"] for x in members)
        tags[identity]=tag;row_metadata[identity]={"target_object_id":target}
        first=members[0]
        src={k:first[k] for k in ("book","period","currency","kind")}
        src.update(id=identity,economic_key=identity,status="data",amount=format(total,".2f"),
                   evidence="exact monthly sum of separately validated original source events")
        grouped[identity]=(src,members)
    return [x for x in sources if x["id"] not in omitted]+[v[0] for v in grouped.values()],grouped

def expand_result(result,groups):
    if not groups:
        return
    raw={k:[x["id"] for x in v[1]] for k,v in groups.items()}
    econ={k:[x["economic_key"] for x in v[1]] for k,v in groups.items()}
    def ids(values,m):
        return sorted({x for v in values for x in m.get(v,[v])})
    for p in result["posted_costs"]:
        p["source_ids"]=ids(p["source_ids"],raw)
        p["economic_components"]=ids(p["economic_components"],econ)
    for d in result["derived_views"].values():
        d["source_ids"]=ids(d["source_ids"],raw)
    for tr in result["trace"].values():
        if tr.get("source") in groups:
            token=tr.pop("source");tr["source_ids"]=raw[token]
            tr["method"]="exact_linear_source_fold"
        if "source_ids" in tr:tr["source_ids"]=ids(tr["source_ids"],raw)
        if "economic_components" in tr:tr["economic_components"]=ids(tr["economic_components"],econ)
    for token,(_,members) in groups.items():
        result["source_coverage"].pop(token)
        for x in members:
            result["source_coverage"][x["id"]]={"status":x["status"],"evidence":x["evidence"],"treatment":"consumed"}
    result["linear_fold_group_count"]=len(groups)
