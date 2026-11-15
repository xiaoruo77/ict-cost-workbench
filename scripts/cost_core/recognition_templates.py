"""Finite tag-based recognition templates; expand to the strict evaluator schema."""
import hashlib

_POST = {"recognize","monthly_straight_line"}
_BASIS = {
    "recognize":{"id","op","source_tag","source_kind","posting_pool"},
    "monthly_straight_line":{"id","op","source_tag","source_kind","posting_pool","start","months"},
    "sum":{"id","op","inputs"},
    "inventory_bridge":{"id","op","flow","opening_tag","closing_tag"},
}

def expand_template(option, sources, tags, row_metadata, need):
    bytag={}
    for item in sources:
        tag=tags[item["id"]]
        need(isinstance(tag,str) and 0<len(tag)<=80,"missing/invalid source tag")
        bytag.setdefault(tag,[]).append(item)
    rules=[]
    mappings={}
    excluded={}
    seen=set()
    need(isinstance(option["rules"],list),"template rules must be an array")
    need(len(option["rules"])<=512,"too many template rules")
    static=option["posting_map"]
    need(isinstance(option["excluded_tags"],dict),"excluded_tags must be an object")

    def group(tag):
        need(isinstance(tag,str) and tag in bytag,f"no records matching tag {tag!r}")
        return sorted(bytag[tag],key=lambda x:x["id"])

    def single(tag,kind):
        selected=group(tag)
        need(len(selected)==1 and selected[0]["kind"]==kind,f"tag {tag!r} must select one {kind} balance")
        return selected[0]["id"]

    static_post=set()
    for t in option["rules"]:
        need(isinstance(t,dict) and isinstance(t.get("op"),str) and t["op"] in _BASIS,"unknown template op")
        kind=t["op"]
        need(set(t)==_BASIS[kind],f"bad {kind} template keys")
        tid=t["id"]
        need(isinstance(tid,str) and 0<len(tid)<=80 and tid not in seen,"invalid/duplicate template id")
        seen.add(tid)
        if kind in _POST:
            static_post.add(tid)
            m=static.get(tid)
            need(isinstance(m,dict),f"missing posting map for template {tid}")
            members=[]
            for source in group(t["source_tag"]):
                sid=source["id"]
                need(source["kind"]==t["source_kind"],f"source {sid} has wrong kind for tag")
                suffix=hashlib.sha256(sid.encode()).hexdigest()[:24]
                eid=f"expanded/{tid}/{suffix}"
                row={k:v for k,v in t.items() if k not in ("source_tag","source_kind","id")}
                # Variable scheduling starts are tied to explicit origin months.
                if "start" in row and row["start"]=="source_origin":
                    need("origin_period" in source,f"source {sid} lacks its original period")
                    row["start"]=source["origin_period"]
                rules.append(dict(row,id=eid,source=sid));members.append(eid)
                item=dict(m)
                if "object_id_from" in item:
                    need(item.pop("object_id_from")=="target_object_id","only target_object_id can map a target")
                    need("object_id" not in item,"fixed and dynamic target both supplied")
                    item["object_id"]=row_metadata[sid].get("target_object_id","")
                mappings[eid]=item
            # Technical nodes are bounded independently from user-declared
            # sums; each join retains original contributors and scope.
            depth=0
            while len(members)>32:
                sub=[]
                for n in range(0,len(members),32):
                    jid=f"expanded-sum/{tid}/{depth}/{n//32}"
                    rules.append(dict(id=jid,op="sum",inputs=members[n:n+32]))
                    sub.append(jid)
                members=sub
                depth+=1
            rules.append(dict(id=tid,op="sum",inputs=members))
        elif kind=="inventory_bridge":
            rules.append(dict(id=tid,op=kind,flow=t["flow"],
                opening=single(t["opening_tag"],"opening_balance"),closing=single(t["closing_tag"],"closing_balance")))
        else:
            rules.append(t)
    need(set(static)==static_post,"template posting_map may only describe posting tags")
    for tag,reason in option["excluded_tags"].items():
        need(isinstance(tag,str) and isinstance(reason,str) and bool(reason.strip()),"malformed exclusion reason")
        # Optional disclosed cash/N/A categories need not occur every month.
        for item in bytag.get(tag,[]):
            excluded[item["id"]]=reason
    return rules,mappings,excluded
