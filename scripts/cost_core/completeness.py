"""Optional, data-only cost coverage contract; no industry assumptions."""
from collections import defaultdict
from decimal import Decimal
import re
from .models import SourceRef

ZERO = Decimal(0)

def gate(condition, message):
    if not condition:
        raise ValueError("coverage: " + message)

def obj(value, label):
    gate(isinstance(value, dict), label + " must be an object")
    return value

def text(value, label):
    gate(isinstance(value, str) and 0 < len(value.strip()) <= 180, label + " must be nonempty text")
    return value.strip()

def fields(value, keys, label, optional=()):
    v = obj(value, label)
    gate(set(keys) <= set(v) <= set(keys) | set(optional), label + " fields invalid")
    return v

def money(value):
    from .csv_adapter import _number
    return _number(value,"control amount")

def specs(s):
    fields(s, ("version","categories","scopes","policies","required_sources","register","controls"),
           "plugin", ("observations","analysis","comparisons","period"))
    gate(s["version"]=="1", "unsupported version")
    cats=obj(s["categories"], "categories")
    gate(0<len(cats)<=128, "requires 1..128 categories")
    for k,v in cats.items():
        text(k,"category code")
        fields(v,("label","kind"),"category",("parent","source","allocation"))
        text(v["label"],"category label")
        gate(v["kind"] in ("group","posting","analysis"),"unknown category kind")
        if v["kind"]=="posting":
            text(v.get("source"),"posting source")
            gate(v.get("allocation") in ("entry","pool","either"),"unknown allocation policy")
        if v["kind"]=="analysis": gate("parent" in v,"analytical category needs posting parent")
        if "parent" in v:
            p=cats.get(text(v["parent"],"parent code"))
            gate(isinstance(p,dict) and p.get("kind")==("posting" if v["kind"]=="analysis" else "group"),"invalid category parent")
    for k in cats:
        path=set()
        while k is not None:
            gate(k not in path, "cyclic categories")
            path.add(k); k=cats[k].get("parent")
    return cats

def evaluate(s, postings, rules, fields_present, currency, dataset, schema_uri, period):
    cats=specs(s)
    scopes=obj(s["scopes"],"scopes"); policies=obj(s["policies"],"policies")
    sources=obj(s["required_sources"],"required sources")
    reg=obj(s["register"],"source register")
    obs=obj(s.get("observations",{}),"observations")
    views=obj(s.get("analysis",{}),"analysis")
    posting={k for k,v in cats.items() if v["kind"]=="posting"}
    gate(0<len(scopes)<=256,"invalid scope count")
    gate(set(obs)|set(views)<=set(scopes),"observation scope is undeclared")
    for source,v in reg.items():
        text(source,"source system")
        fields(v,("state","evidence"),"source review")
        gate(v["state"] in ("reviewed","missing"),"source review state must be reviewed or missing")
        text(v["evidence"],"source register evidence")
    gate(set(sources)==set(policies),"source requirements must cover every scope type")
    for kind,p in policies.items():
        p=obj(p,"applicability")
        gate(set(p)==posting,"every posting category needs explicit applicability")
        for status in p.values():
            gate(status=="required" or (isinstance(status,str) and status.startswith("na:") and status[3:].strip()),"use required or na:<reason>")
        ss=sources[kind]
        gate(isinstance(ss,list) and all(isinstance(v,str) and v in reg for v in ss),"unknown required source")
        gate({cats[c]["source"] for c,status in p.items() if status=="required"}<=set(ss),"required cost source omitted from scope register policy")
    for k,v in scopes.items():
        text(k,"scope id"); gate(isinstance(v,str) and v in policies,"scope type has no policy")
    required=("source_system","document_id","line_id")
    gate(set(required)<=set(fields_present),"missing source identity columns")
    gate(isinstance(s["controls"],list),"controls must be an array")
    for cat in posting: gate(cats[cat]["source"] in reg,"posting source not in register")
    controls={}
    for c in s["controls"]:
        fields(c,(*required,"amount"),"control")
        k=tuple(text(c[x],x) for x in required)
        gate(k not in controls,"duplicate parent posting control")
        gate(k[0] in reg,"unknown parent posting control source")
        controls[k]=money(c["amount"])
    actual=defaultdict(lambda: ZERO); sums=defaultdict(lambda: ZERO)
    entries=defaultdict(list); seen=set(); splits=defaultdict(set)
    issues=[]
    def add(code,scope="",category="",**details):
        issues.append(dict(code=code,object_id=scope or None,category=category or None,**details))
    def evidence(field,selector):
        return SourceRef(dataset_id=dataset,source_uri=schema_uri,field=field,selector=selector,kind="configuration")
    if not re.fullmatch(r"[0-9]{4}-(0[1-9]|1[0-2])",period):
        raise ValueError("coverage: period must be YYYY-MM")
    if s.get("period") is None:
        add("coverage_control_period_unverified",expected=period)
    elif s["period"] != period:
        add("coverage_control_period_mismatch",expected=period,declared=s["period"])
    active_pools={r.pool_id for r in rules}
    for line,row,mode,item in postings:
        owner=item.owner_id if mode=="pool" else item.object_id
        cat=item.element_id; val=item.amount
        gate(item.currency==currency,"coverage requires normalized ledger currency")
        identity=tuple(text(row.get(f),f"row {line} {f}") for f in required)
        split=row.get("split_id","").strip()
        if split: text(split,f"row {line} split_id")
        key=identity+(split,)
        gate(key not in seen,f"duplicate source posting at CSV row {line}")
        seen.add(key); splits[identity].add(split)
        gate(len(splits[identity])==1 or "" not in splits[identity],"cannot book parent with its splits")
        sums[identity]+=val
        if owner not in scopes:
            add("coverage_unmapped_scope", owner, cat, row=line);continue
        if cat not in posting:
            add("coverage_unmapped_category",owner,cat,row=line);continue
        actual[(owner,cat)]+=val; entries[(owner,cat)].append(item)
        spec=cats[cat]
        if identity[0]!=spec["source"]:
            add("coverage_wrong_source",owner,cat,row=line,expected=spec["source"])
        if mode=="pool" and item.pool_id not in active_pools:
            add("coverage_pool_has_no_allocation",owner,cat,row=line,pool_id=item.pool_id)
        if spec["allocation"] not in (mode,"either"):
            add("coverage_wrong_allocation",owner,cat,row=line,expected=spec["allocation"])
    for k in sorted(set(sums)|set(controls)):
        if k not in controls:
            add("coverage_uncontrolled_posting",posting=list(k),booked=str(sums[k]))
        elif k not in sums:
            add("coverage_expected_posting_missing",posting=list(k),expected=str(controls[k]))
        elif sums[k]!=controls[k]:
            add("coverage_control_difference",posting=list(k),expected=str(controls[k]),booked=str(sums[k]))
    covered=[]; analysis=[]; totals=defaultdict(lambda:ZERO)
    for scope, kind in scopes.items():
        for sid in sources[kind]:
            if reg[sid]["state"]!="reviewed":add("coverage_source_not_reviewed",scope,source=sid)
        assertions=obj(obs.get(scope,{}),"verification assertions")
        gate(set(assertions)<=posting,"observation category is not a posting category")
        for cat in sorted(posting):
            cells=entries[(scope,cat)]; seen_value=bool(cells); expected=policies[kind][cat]
            o=assertions.get(cat)
            refs=[r for item in cells for r in item.source_refs]
            reason=""
            if expected!="required":
                gate(o is None,"verified zero is only for applicable categories")
                state,reason="not_applicable",expected[3:]
                if seen_value:
                    add("coverage_not_applicable_booked",scope,cat);state="conflict"
            elif o is not None:
                fields(o,("state","evidence"),"zero assertion")
                gate(o["state"]=="zero","use zero or declare NA in policy")
                reason=text(o["evidence"],"zero evidence")
                state="verified_zero"
                if seen_value:
                    add("coverage_zero_conflicts_with_posting",scope,cat);state="conflict"
                if reg[cats[cat]["source"]]["state"]!="reviewed":
                    add("coverage_zero_source_not_reviewed",scope,cat);state="conflict"
            elif seen_value: state="observed"
            else:
                state="missing"; add("coverage_required_missing",scope,cat)
            if state=="verified_zero":
                refs.append(evidence("observations",f"completeness.observations.{scope}.{cat}"))
            if state=="not_applicable":
                refs.append(evidence("policies",f"completeness.policies.{kind}.{cat}"))
            amount=actual[(scope,cat)] if seen_value else ZERO if state=="verified_zero" else None
            covered.append(dict(scope=scope,scope_type=kind,category=cat,label=cats[cat]["label"],state=state,
                                amount=amount,reason=reason,source_refs=tuple(dict.fromkeys(refs))))
        subtotal=defaultdict(lambda:ZERO)
        for cat,a in obj(views.get(scope,{}),"analytical disclosures").items():
            gate(cat in cats and cats[cat]["kind"]=="analysis","not an analytical category")
            fields(a,("amount","evidence"),"analytical disclosure")
            value=money(a["amount"]); parent=cats[cat]["parent"]
            label=text(a["evidence"],"analytical evidence")
            gate(value>=0,"analytical breakdown must be nonnegative")
            subtotal[parent]+=value
            state="observed"
            if subtotal[parent]>actual[(scope,parent)]:
                add("coverage_analytical_exceeds_parent",scope,cat)
                state="unverified"
            refs=(evidence("analysis",f"completeness.analysis.{scope}.{cat}"),)
            analysis.append(dict(scope=scope,category=cat,label=cats[cat]["label"],parent=parent,
                                 amount=value,posting=False,state=state,evidence=label,source_refs=refs))
    for (_,cat), amount in actual.items():
        totals[cat]+=amount
        parent=cats[cat].get("parent")
        while parent is not None:
            totals[parent]+=amount
            parent=cats[parent].get("parent")
    comparisons=[]
    for cat,v in obj(s.get("comparisons",{}),"budget comparisons").items():
        gate(cat in posting,"budget comparison requires posting category")
        fields(v,("budget","evidence"),"budget comparison",
               ("actual_quantity","actual_rate","budget_quantity","budget_rate"))
        label=text(v["evidence"],"budget evidence")
        observed=totals[cat]; budget=money(v["budget"])
        refs=(evidence("comparisons",f"completeness.comparisons.{cat}"),)
        entry=dict(category=cat,label=cats[cat]["label"],actual=observed,budget=budget,
                   variance=observed-budget,evidence=label,source_refs=refs)
        factors=("actual_quantity","actual_rate","budget_quantity","budget_rate")
        if any(f in v for f in factors):
            gate(all(f in v for f in factors),"variance bridge requires all quantity/rate fields")
            aq,ar,bq,br=(money(v[f]) for f in factors)
            gate(min(aq,ar,bq,br)>=0,"variance factor must be nonnegative")
            if aq*ar!=observed or bq*br!=budget:
                add("coverage_budget_factors_not_reconciled",category=cat)
            else:
                entry.update(actual_quantity=aq,actual_rate=ar,budget_quantity=bq,budget_rate=br,
                             usage_effect=(aq-bq)*br,rate_effect=aq*(ar-br))
        comparisons.append(entry)
    recognized=sum(actual.values(),ZERO)
    ledger=sum(sums.values(),ZERO)
    return dict(version="1",status="incomplete" if issues else "complete_for_declared_scope",
                evaluated_scope="full_reported_ledger",scopes=scopes,cells=covered,
                category_totals={k:dict(label=cats[k]["label"],kind=cats[k]["kind"],amount=v) for k,v in totals.items()},
                analytical=analysis,comparisons=comparisons,ledger_total=ledger,mapped_total=recognized,unmapped_total=ledger-recognized,
                controls_total=sum(controls.values(),ZERO),issues=issues,
                source_register={k:{**v,"source_refs":(evidence("register",f"completeness.register.{k}"),)} for k,v in reg.items()})
