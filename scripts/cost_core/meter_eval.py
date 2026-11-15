"""Bounded independent meter tariff recognition; no external dependencies or arbitrary code."""
from collections import defaultdict
from decimal import Decimal as D, ROUND_HALF_UP, localcontext
import re

class Invalid(ValueError):
    pass

SCOPES = {"book","period","currency","billing_account"}
ROW_BASE = SCOPES | {"id","kind","evidence"}
MAX_ROWS = 12000
MAX_TIERS = 64


def require(ok, error):
    if not ok:
        raise Invalid(error)


def exact(obj, fields):
    require(type(obj) is dict and set(obj)==fields, "invalid object fields")


def val(x):
    require(type(x) is str and 0<len(x)<=128 and x==x.strip() and bool(x), "invalid text")
    return x


def month(s):
    require(type(s) is str and re.fullmatch(r"[0-9]{4}-(?:0[1-9]|1[0-2])",s) and not s.startswith("0000"),
            "invalid period")
    y,m=map(int,s.split("-"))
    return y*12+m


def dec(x, scale):
    require(type(x) is str and bool(re.fullmatch(r"-?[0-9]{1,14}(?:\.[0-9]{1,8})?",x)), "invalid decimal")
    require("." not in x or len(x.split(".")[1])<=scale, "exceeds declared precision")
    return D(x)


def recognize(profile, scope, rows):
    """One explicitly scoped bill. Half-up once after exact variable+fixed+credits."""
    with localcontext() as ctx:
        ctx.prec=50
        return _recognize(profile,scope,rows)


def _recognize(profile, scope, rows):
    exact(profile,{"version","currency","unit","tier_scope","usage_scale","currency_scale","tiers","basis_months"})
    require(type(profile["version"]) is int and profile["version"]==1,"unsupported profile version")
    val(profile["unit"])
    require(re.fullmatch("[A-Z]{3}",val(profile["currency"])),"invalid profile currency")
    require(profile["tier_scope"] in ("meter","billing_account"),"unknown tier scope")
    for name,maxv in (("usage_scale",6),("currency_scale",4),("basis_months",1200)):
        require(type(profile[name]) is int and 0<=profile[name]<=maxv,f"invalid {name}")
    us,cs,mcount=(profile[k] for k in ("usage_scale","currency_scale","basis_months"))
    quantum=D(1).scaleb(-cs)
    exact(scope,SCOPES)
    require(all(val(v) for v in scope.values()),"invalid scope")
    require(scope["currency"]==profile["currency"],"mixed or wrong currency")
    month_idx=month(scope["period"])
    require(type(profile["tiers"]) is list and 0<len(profile["tiers"])<=MAX_TIERS,"invalid tier count")
    tiers=[]
    lower=D(0)
    for i,t in enumerate(profile["tiers"]):
        exact(t,{"until","rate"})
        upper=dec(t["until"],us) if t["until"] is not None else None
        require((i==len(profile["tiers"])-1)==(upper is None),"only final tier can be uncapped")
        require(upper is None or upper>lower,"tier endpoints must increase")
        rate=dec(t["rate"],8)
        require(rate>=0,"negative usage rate")
        tiers.append((lower,upper,rate))
        if upper is not None: lower=upper
    require(type(rows) is list and 0<len(rows)<=MAX_ROWS,"invalid record count")
    groups=defaultdict(list)
    ids=set()
    fixed=[]
    credits=[]
    for r in rows:
        require(type(r) is dict,"invalid record")
        kind=r.get("kind")
        if kind=="usage":
            exact(r,ROW_BASE|{"qty","unit","meter"})
        elif kind=="basis":
            exact(r,ROW_BASE|{"amount","origin_period"})
        elif kind=="vendor_credit":
            exact(r,ROW_BASE|{"amount"})
        else:
            raise Invalid("unknown event kind")
        src=val(r["id"]); val(r["evidence"])
        require(src not in ids,"duplicate evidence source ID")
        ids.add(src)
        require(all(scope[key]==r[key] for key in SCOPES),"mixed book, currency, account or out-of-period record")
        if kind=="usage":
            require(r["unit"]==profile["unit"],"mixed or unsupported measurement unit")
            meter=val(r["meter"])
            q=dec(r["qty"],us)
            require(q>=0,"negative usage quantity")
            if q==0: require(bool(r["evidence"]),"zero usage needs evidence")
            group=meter if profile["tier_scope"]=="meter" else scope["billing_account"]
            groups[group].append((src,q,meter,r["evidence"]))
        else:
            amount=dec(r["amount"],cs)
            if kind=="basis":
                require(amount>=0,"negative basis")
                require(mcount>0,"unexpected fixed fee basis")
                idx=month_idx-month(r["origin_period"])
                require(0<=idx<mcount,"inactive fixed basis schedule")
                per_unit,rem=divmod(int(amount/quantum),mcount)
                charge=(per_unit+(rem if idx==mcount-1 else 0))*quantum
                fixed.append((src,charge,r["evidence"],amount,r["origin_period"],idx+1,mcount))
            else:
                require(amount<=0,"vendor credit must be nonpositive")
                credits.append((src,amount,r["evidence"]))
    require(bool(groups),"missing metered usage, not zero")
    require(len(fixed)==(1 if mcount else 0),"fixed basis missing or ambiguous")
    details={}
    for group, events in sorted(groups.items()):
        qty=sum((e[1] for e in events),D(0))
        bands=[]
        for a,b,rate in tiers:
            units=max(D(0),(qty if b is None else min(qty,b))-a)
            bands.append({"from":a,"until":b,"usage":units,"rate":rate,"cost":units*rate})
        details[group]={"quantity":qty,"meters":sorted({e[2] for e in events}),
                        "events":[{"source_id":s,"quantity":q,"meter":m,"evidence":p} for s,q,m,p in sorted(events)],
                        "bands":bands,"raw":sum((b["cost"] for b in bands),D(0))}
    billing=sum((d["raw"] for d in details.values()),D(0))
    fee=sum((f[1] for f in fixed),D(0))
    credit=sum((x[1] for x in credits),D(0))
    net=billing+fee+credit
    return {"scope":scope.copy(),"unit":profile["unit"],"tier_scope":profile["tier_scope"],"groups":details,
            "raw_variable":billing,"fixed":fee,"raw_net":net,"credit":credit,
            "reported_net":net.quantize(quantum,rounding=ROUND_HALF_UP),
            "basis_events":[dict(source_id=s,charge=c,evidence=e,basis=a,origin_period=o,month_index=i,months=n)
                             for s,c,e,a,o,i,n in fixed],
            "credit_events":[dict(source_id=s,amount=a,evidence=e) for s,a,e in sorted(credits)],
            "rounding":"raw decimal sum, HALF_UP currency precision once at final reported monthly net; fixed schedules allocate residual in final month"}
