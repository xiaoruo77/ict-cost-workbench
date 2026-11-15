"""Bounded compilation of v2 recognition DAGs into the existing evaluator.

External source/rule limits remain unchanged in recognition_eval. All money
operations are executed there; this module partitions posting atoms and
reassociates validated node values while retaining original lineage.
"""
from collections import Counter
from dataclasses import replace
from decimal import Decimal, Context, ROUND_HALF_UP, localcontext
import json
from . import recognition_eval as strict

MAX_EVENTS = 10000
MAX_EXPANDED_RULES = 22000
MAX_EXPANDED_REFERENCES = 50000

def document_bytes(doc):
    return len(json.dumps(doc,ensure_ascii=False,separators=(",",":")).encode("utf-8"))

def requires_batching(raw):
    return (len(raw["sources"]) > strict.MAX_SOURCES or len(raw["rules"]) > strict.MAX_RULES
            or len(raw["outputs"]) > strict.MAX_OUTPUTS or document_bytes(raw) > strict.MAX_DOCUMENT_BYTES)

class Bounded:
    def __init__(self, raw):
        self.raw=raw
        self.audit={"document_count":0,"evaluation_document_count":0,"validation_document_count":0,
                    "max_sources":0,"max_rules":0,"max_outputs":0,"max_document_bytes":0,
                    "source_limit":strict.MAX_SOURCES,"rule_limit":strict.MAX_RULES,
                    "output_limit":strict.MAX_OUTPUTS,"document_byte_limit":strict.MAX_DOCUMENT_BYTES}
        self.names=set()
    def base(self,sources,rules,outputs,exclusions=None):
        return {"name":self.raw["name"],"context":self.raw["context"],"sources":sources,
                "rules":rules,"outputs":outputs,"excluded_sources":exclusions or {}}
    def fits(self,doc):
        refs=sum(len(r.get("inputs",[]))+(1 if r.get("flow") else 0) for r in doc["rules"])
        return (len(doc["sources"])<=strict.MAX_SOURCES and len(doc["rules"])<=strict.MAX_RULES
                and len(doc["outputs"])<=strict.MAX_OUTPUTS and refs<=strict.MAX_REFERENCES
                and document_bytes(doc)<=strict.MAX_DOCUMENT_BYTES)
    def note(self,doc,phase):
        strict.check(self.fits(doc),"Recognition atom exceeds strict document limits")
        self.audit["document_count"]+=1
        self.audit[phase+"_document_count"]+=1
        for key,n in (("sources",len(doc["sources"])),("rules",len(doc["rules"])),
                      ("outputs",len(doc["outputs"])),("document_bytes",document_bytes(doc))):
            self.audit["max_"+key]=max(self.audit["max_"+key],n)
    def chunks(self,items,build):
        batch=[]
        for item in items:
            proposed=batch+[item]
            if self.fits(build(proposed)):
                batch=proposed
                continue
            strict.check(bool(batch),"Recognition atom exceeds strict document limits")
            yield batch
            batch=[item]
            strict.check(self.fits(build(batch)),"Recognition atom exceeds strict document limits")
        if batch:
            yield batch
    def decode(self,doc):
        self.note(doc,"validation")
        return strict.load(doc)
    def evaluate(self,doc,carried=None):
        self.note(doc,"evaluation")
        if carried:
            # Internal carry values have already been computed by strict
            # evaluation. They are not newly admitted public money sources:
            # source money's 18-digit decoder must not cap a legal subtotal.
            decoding={**doc,"sources":[{**s,"amount":"0.00"} if s["id"] in carried else s
                                      for s in doc["sources"]]}
            spec=strict.load(decoding)
            spec=replace(spec,sources=tuple(replace(s,amount=carried[s.id]) if s.id in carried else s
                                           for s in spec.sources))
            return strict.evaluate(spec)
        result=strict.run_document(doc)
        strict.check(result["status"]=="valid",result.get("error","invalid source records"))
        return result
    def technical_id(self,kind,number):
        base="__bounded_recognition__/"+kind+"/"+str(number)
        ident=base
        while ident in self.names:
            ident+="_"
        self.names.add(ident)
        return ident
    def run(self):
        raw=self.raw
        strict.keys(raw,{"name","context","sources","rules","outputs","excluded_sources"})
        source_rows=strict.seq(raw["sources"]);rule_rows=strict.seq(raw["rules"])
        outputs=tuple(strict.text(x) for x in strict.seq(raw["outputs"]))
        strict.check(0<len(outputs)<=strict.MAX_OUTPUTS,"Too many outputs" if outputs else "No output declared")
        strict.check(len(set(outputs))==len(outputs),"Duplicate output")
        strict.check(len(source_rows)<=MAX_EVENTS,"v2 source event limit is 10000")
        strict.check(len(rule_rows)<=MAX_EXPANDED_RULES,"Expanded recognition rule limit exceeded")
        strict.check(sum(len(r.get("inputs",[]))+(1 if r.get("flow") else 0)
                         for r in rule_rows if isinstance(r,dict))<=MAX_EXPANDED_REFERENCES,
                     "Expanded recognition reference limit exceeded")
        exclusions=raw["excluded_sources"]
        strict.check(isinstance(exclusions,dict),"Expected exclusion map")
        for sid,reason in exclusions.items():strict.text(sid);strict.text(reason)
        sources={}
        build_sources=lambda batch:self.base(batch,[],["decode-only"])
        for batch in self.chunks(source_rows,build_sources):
            spec=self.decode(build_sources(batch))
            self.context=spec.context
            for src in spec.sources:
                strict.check(src.id not in sources,"Duplicate source ID")
                strict.check((src.book,src.period,src.currency)==
                             (spec.context.book,spec.context.period,spec.context.currency),
                             "Stale or cross-book/period/currency source: "+src.id)
                strict.check(src.status!=strict.Status.MISSING,"Missing source: "+src.id)
                sources[src.id]=src
        # Decode context even for an empty source population.
        if not sources:
            self.context=self.decode(self.base([],[],["decode-only"])).context
        strict.check(exclusions.keys()<=sources.keys(),"Undefined excluded source")
        rules={}
        build_rules=lambda batch:self.base([],batch,["decode-only"])
        for batch in self.chunks(rule_rows,build_rules):
            for rule in self.decode(build_rules(batch)).rules:
                strict.check(rule.id not in rules,"Duplicate rule ID")
                rules[rule.id]=rule
        strict.check(not(sources.keys() & rules.keys()),"Source and rule IDs must be disjoint")
        self.names=set(sources)|set(rules)
        active=set();depths={};order=[]
        def visit(rid):
            strict.check(rid in rules,"Undefined rule/output: "+str(rid))
            strict.check(rid not in active,"Cycle detected at "+rid)
            if rid in depths:return
            strict.check(len(active)<strict.MAX_DEPTH,"Rule dependency depth limit exceeded")
            active.add(rid);rule=rules[rid]
            deps=rule.inputs if rule.op==strict.Op.SUM else ((rule.flow,) if rule.op==strict.Op.BRIDGE else ())
            for dep in deps:visit(dep)
            depth=1+max((depths[d] for d in deps),default=0)
            strict.check(depth<=strict.MAX_DEPTH,"Rule dependency depth limit exceeded")
            active.remove(rid);depths[rid]=depth;order.append(rid)
        for rid in rules:visit(rid)
        for rid in outputs:visit(rid)
        original_sources={s["id"]:s for s in source_rows}
        original_rules={r["id"]:r for r in rule_rows}
        leaves=[r for r in rule_rows if r["op"] in ("recognize","monthly_straight_line")]
        def leaf_doc(batch):
            ids=list(dict.fromkeys(r["source"] for r in batch))
            strict.check(all(sid in original_sources for sid in ids),"Undefined source")
            return self.base([original_sources[s] for s in ids],batch,[batch[0]["id"]],
                             {sid:exclusions[sid] for sid in ids if sid in exclusions})
        values={};trace={};postings=[];posted_keys=set();used=set();pools={}
        for batch in self.chunks(leaves,leaf_doc):
            result=self.evaluate(leaf_doc(batch))
            for posting in result["posted_costs"]:
                components=frozenset(posting["economic_components"])
                strict.check(not(posted_keys & components),
                             "Duplicate posting economic component: "+posting["recognition_rule"])
                posted_keys.update(components);postings.append(posting)
                rid=posting["recognition_rule"];amount=Decimal(posting["amount"])
                values[rid]=(amount,components,frozenset(posting["source_ids"]))
                trace[rid]=result["trace"][rid];used.update(posting["source_ids"])
                key=posting["posting_pool"];pools[key]=pools.get(key,Decimal(0))+amount
        def combine(parts,rid):
            counts=Counter(c for part in parts for c in part[1])
            strict.check(not any(n>1 for n in counts.values()),"Double recognition at "+rid)
            return frozenset(counts),frozenset(s for part in parts for s in part[2])
        for rid in order:
            rule=rules[rid]
            if rule.op in (strict.Op.RECOGNIZE,strict.Op.MONTHLY):continue
            deps=rule.inputs if rule.op==strict.Op.SUM else (rule.flow,)
            parts=[values[dep] for dep in deps]
            if rule.op==strict.Op.BRIDGE:
                for sid in (rule.opening,rule.closing):
                    strict.check(sid in sources,"Undefined source: "+str(sid))
                    src=sources[sid]
                    parts.append((src.amount,frozenset({src.economic_key}),frozenset({sid})))
            components,ids=combine(parts,rid)
            carried={};technical_sources=[];technical_rules=[];mapped=[]
            for number,dep in enumerate(deps):
                amount=values[dep][0];sid=self.technical_id("value",len(self.names))
                leaf=self.technical_id("rule",len(self.names));mapped.append(leaf)
                carried[sid]=amount
                technical_sources.append({"id":sid,**raw["context"],"kind":"credit" if amount<0 else "expense",
                    "status":"data","amount":format(amount,".2f"),"economic_key":sid,
                    "evidence":"validated bounded recognition carry"})
                technical_rules.append({"id":leaf,"op":"recognize","source":sid,"posting_pool":"technical"})
            if rule.op==strict.Op.SUM:
                technical_rules.append({"id":rid,"op":"sum","inputs":mapped})
                detail={"op":"sum","inputs":list(rule.inputs)}
                actual_exclusions={}
            else:
                for sid in (rule.opening,rule.closing):technical_sources.append(original_sources[sid])
                technical_rules.append({"id":rid,"op":"inventory_bridge","flow":mapped[0],
                                        "opening":rule.opening,"closing":rule.closing})
                detail={"op":"inventory_bridge","flow":rule.flow,"opening":rule.opening,"closing":rule.closing,
                        "formula":"opening + current_flow - closing"}
                actual_exclusions={sid:exclusions[sid] for sid in (rule.opening,rule.closing) if sid in exclusions}
            result=self.evaluate(self.base(technical_sources,technical_rules,[rid],actual_exclusions),carried)
            amount=Decimal(result["outputs"][rid]);values[rid]=(amount,components,ids);used.update(ids)
            detail.update(amount=format(amount,".2f"),source_ids=sorted(ids),economic_components=sorted(components))
            trace[rid]=detail
        strict.check(used | exclusions.keys()==sources.keys(),
                     "Unclassified source coverage: "+str(sorted(sources.keys()-used-exclusions.keys())))
        strict.check(bool(used),"No recognized sources")
        nonzero=[{"source_id":sid,"kind":sources[sid].kind.value,"amount":format(sources[sid].amount,".2f"),"reason":why}
                 for sid,why in exclusions.items() if sources[sid].kind!=strict.Kind.CASH
                 and sources[sid].amount not in (None,Decimal(0))]
        limited=any(sources[s].kind!=strict.Kind.CASH and sources[s].status!=strict.Status.NA for s in exclusions)
        return {"status":"valid","scope":"synthetic_reference_not_production_acceptance","can_claim_complete":False,
          "coverage_status":"partial_declared_exclusions" if limited else "declared_input_coverage_only",
          "unrecognized_nonzero_sources":nonzero,"model":raw["name"],"context":vars(self.context),
          "posted_costs":postings,"posted_pool_totals":{p:format(v,".2f") for p,v in pools.items()},
          "derived_views":{rid:{"amount":format(values[rid][0],".2f"),"treatment":"non_posting_overlapping_view",
                               "source_ids":sorted(values[rid][2])}
                           for rid in outputs if rules[rid].op in (strict.Op.SUM,strict.Op.BRIDGE)},
          "memo_sources":[{"source_id":sid,"kind":sources[sid].kind.value,
                          "amount":None if sources[sid].amount is None else format(sources[sid].amount,".2f"),
                          "reason":why,"treatment":"not_posted"} for sid,why in exclusions.items()],
          "balance_inputs":[{"source_id":s.id,"kind":s.kind.value,"amount":format(s.amount,".2f"),
                             "treatment":"bridge_input_not_posted"} for s in sources.values()
                            if s.kind in (strict.Kind.OPENING,strict.Kind.CLOSING) and s.id in used],
          "outputs":{rid:format(values[rid][0].quantize(Decimal("0.01"),rounding=ROUND_HALF_UP),".2f") for rid in outputs},
          "trace":{rid:trace[rid] for rid in order},"excluded_sources":dict(exclusions),
          "source_coverage":{s.id:{"status":s.status.value,"evidence":s.evidence,
                                 "treatment":"excluded" if s.id in exclusions else "consumed"} for s in sources.values()},
          "bounded_evaluation":self.audit}

def run_bounded_document(raw):
    try:
        with localcontext(Context(prec=60,rounding=ROUND_HALF_UP)):
            return Bounded(raw).run()
    except strict.Invalid as exc:
        return {"status":"invalid","error":str(exc),"outputs":{}}
