"""Read-only completeness checklist evaluated on disjoint final components."""
from collections import defaultdict
from dataclasses import asdict,dataclass
from decimal import Decimal,localcontext
import hashlib
import json
import re

from .models import SourceRef
from .object_budgets import membership_sha256
from .object_rollups import safe_text

FIELD="category_checklist"


@dataclass(frozen=True,slots=True)
class CheckItem:
    ident:str
    name:str
    state:str
    evidence:str|None
    matches:tuple[tuple[str,str],...]
    policy_ref:SourceRef
    policy_sha256:str


@dataclass(frozen=True,slots=True)
class ChecklistGroup:
    group_id:str
    membership_sha256:str
    items:tuple[CheckItem,...]


@dataclass(frozen=True,slots=True)
class CategoryChecklistPlan:
    groups:tuple[ChecklistGroup,...]


def need(ok,why):
    if not ok:
        raise ValueError("category checklist: "+why)


def policy_sha256(row):
    """Exactly bind reviewed row contents; JSON formatting/key order are not policy."""
    canonical=json.dumps(row,ensure_ascii=False,sort_keys=True,separators=(",",":"),allow_nan=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _groups(config,scope,rollups,dataset_id,schema_uri):
    need(type(config) is dict and set(config)=={"version","groups"} and
         type(config["version"]) is int and config["version"]==1,"bad version or fields")
    need(type(rollups) is list and bool(rollups),"explicit final object rollups required")
    by_id={g["id"]:g for g in rollups}
    groups=config["groups"]
    need(type(groups) is dict and 0<len(groups)<=48 and groups.keys()<=by_id.keys(),
         "unknown or too many canonical groups")
    n_items=n_selectors=0
    for gid,rows in sorted(groups.items()):
        need(type(rows) is list and 0<len(rows)<=64,"invalid category count")
        branches={m["input_name"] for m in by_id[gid]["members"]}
        used,matched=set(),set()
        items=[]
        for index,row in enumerate(rows):
            fields={"id","name","state","matches"}
            need(type(row) is dict and fields<=row.keys()<=fields|{"evidence"},"invalid category fields")
            ci,label,state,selectors=(row[k] for k in ("id","name","state","matches"))
            need(type(ci) is str and re.fullmatch(r"[A-Za-z0-9_-]{1,40}",ci) and
                 ci.casefold() not in used and safe_text(label,128),"invalid or aliased category identity")
            used.add(ci.casefold())
            need(type(state) is str and state in {"required","declared_zero","not_applicable"},
                 "invalid category state")
            ev=row.get("evidence")
            need(("evidence" not in row if state=="required" else safe_text(ev,240)),
                 "misplaced or missing assertion evidence")
            need(type(selectors) is list and 0<len(selectors)<=24,"invalid selector count")
            pairs=[]
            for sel in selectors:
                need(type(sel) is dict and set(sel)=={"input_name","element_id"},"invalid selector shape")
                branch,element=sel["input_name"],sel["element_id"]
                need(safe_text(branch,40) and branch in branches and safe_text(element,128) and
                     (branch,element) not in matched,"unknown branch or overlapping selector")
                matched.add((branch,element))
                pairs.append((branch,element))
                n_selectors+=1
                need(n_selectors<=1024,"global category or selector limit")
            ref=SourceRef(dataset_id=dataset_id,source_uri=schema_uri,field=FIELD,
                    selector=f"raw_composite.category_checklist.groups.{gid}[{index}]",kind="configuration")
            items.append(CheckItem(ci,label,state,ev,tuple(pairs),ref,policy_sha256(row)))
            n_items+=1
            need(n_items<=256,"global category or selector limit")
        yield ChecklistGroup(gid,membership_sha256(scope,gid,by_id[gid]["members"]),tuple(items))


def compile_plan(config,scope,rollups,dataset_id,schema_uri):
    return CategoryChecklistPlan(tuple(_groups(config,scope,rollups,dataset_id,schema_uri)))


def validate(config,scope,rollups):
    for _ in _groups(config,scope,rollups,"validation",""):
        pass


def summarize(plan,rollup,objects,scope):
    """Scan final individual components rather than net cost_structure entries."""
    groups={x["id"]:x for x in rollup["groups"]}
    by_obj={x["object_id"]:x for x in objects}
    summaries=[]
    for cfg in plan.groups:
        current=groups[cfg.group_id]
        members=current["members"]
        if membership_sha256(scope,cfg.group_id,members)!=cfg.membership_sha256:
            raise ArithmeticError("category checklist finalized membership changed")
        selectors={s:item.ident for item in cfg.items for s in item.matches}
        found,remaining=defaultdict(list),defaultdict(list)
        for m in members:
            for part in by_obj[m["object_id"]]["cost_components"]:
                key=m["input_name"],part["element_id"]
                ident=selectors.get(key)
                (remaining[key] if ident is None else found[ident]).append(part)
        with localcontext() as ctx:
            ctx.prec=64
            def total(parts):
                return sum((Decimal(p["amount"]) for p in parts),Decimal(0))
            categorized=sum((total(parts) for parts in found.values()),Decimal(0))
            uncategorized=sum((total(p) for p in remaining.values()),Decimal(0))
            if categorized+uncategorized!=Decimal(current["total_cost"]):
                raise ArithmeticError("category checklist does not reconcile to final group cost")
            rows=[]
            def pointers(refs):
                by_source={tuple(sorted(r.items())):r for r in refs}
                return [by_source[key] for key in sorted(by_source)]
            for item in cfg.items:
                parts=found.get(item.ident,())
                if parts:
                    state="observed" if item.state=="required" else "conflict"
                    amount=format(total(parts),"f")
                elif item.state=="required":
                    state,amount="missing",None
                else:
                    state,amount=item.state,("0.00" if item.state=="declared_zero" else None)
                refs=[asdict(item.policy_ref),*(r for p in parts for r in p["source_refs"])]
                rows.append(dict(id=item.ident,name=item.name,state=state,declared_state=item.state,
                                 evidence=item.evidence,amount=amount,component_count=len(parts),
                                 policy_sha256=item.policy_sha256,
                                 source_refs=pointers(refs)))
            other=[dict(input_name=branch,element_id=element,amount=format(total(parts),"f"),
                        component_count=len(parts),
                        source_refs=pointers(r for part in parts for r in part["source_refs"]))
                   for (branch,element),parts in sorted(remaining.items())]
        summaries.append(dict(group_id=cfg.group_id,group_total=current["total_cost"],
            membership_sha256=cfg.membership_sha256,categories=rows,
            categorized_observed_amount=format(categorized,"f"),uncategorized_amount=format(uncategorized,"f"),
            uncategorized_component_count=sum(x["component_count"] for x in other),
            uncategorized_components=other,
            state=("needs_review" if remaining or any(x["state"] in {"missing","conflict"} for x in rows)
                   else "satisfied_for_declared_categories_only")))
    return dict(version=1,basis="readonly_final_components_declared_categories",
        can_claim_complete=False,groups=summaries,
        unreviewed_group_ids=sorted(set(groups)-{g.group_id for g in plan.groups}),
        ungrouped_ledger_amount=rollup["ungrouped_total"])
