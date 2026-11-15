"""Load operator-reviewed declarative rules. No expression execution."""
import json, re
from pathlib import Path
ROOT = Path(__file__).resolve().parents[2] / "references" / "cost_profiles"
_ID = re.compile(r"[a-z][a-z0-9_-]{0,47}")

def load_profile(code):
    p = ROOT / (code + ".json")
    if not p.is_file():
        return None
    if p.stat().st_size > 64_000:
        raise ValueError("Profile exceeds size limit")
    d = json.loads(p.read_text(encoding="utf-8"))
    if not isinstance(d, dict) or not {"version","industry_code","fields","modules","allocation"} <= set(d) or set(d)-{"version","industry_code","fields","modules","allocation","pools","metrics"}:
        raise ValueError("Invalid profile structure")
    if d["version"] != "1.0.0" or d["industry_code"] != code:
        raise ValueError("Incompatible profile version or industry")
    fields = d["fields"]
    if not isinstance(fields,list) or len(fields)>32:
        raise ValueError("Invalid profile fields")
    ids = set()
    for f in fields:
        if (not isinstance(f,dict) or set(f)!={"id","label","type","required"}
            or not isinstance(f["id"],str) or not _ID.fullmatch(f["id"])
            or f["id"] in ids or f["type"] not in ("decimal","text")
            or not isinstance(f["label"],str) or not f["label"].strip()
            or not isinstance(f["required"],bool)):
            raise ValueError("Invalid or duplicate profile field")
        ids.add(f["id"])
    mods = d["modules"]
    if not isinstance(mods,dict) or not 1<=len(mods)<=64:
        raise ValueError("Invalid cost modules")
    for k,v in mods.items():
        if (not _ID.fullmatch(k) or not isinstance(v,dict) or
            set(v)!={"label","behavior","cost_type"} or
            v["behavior"] not in ("fixed","variable") or
            v["cost_type"] not in ("direct","indirect") or
            not isinstance(v["label"],str) or not v["label"].strip()):
            raise ValueError("Invalid cost module")
    a=d["allocation"]
    if a is not None and (not isinstance(a,dict) or set(a)!={"driver","module"}
                          or a["driver"] not in ids or a["module"] not in mods
                          or next(f for f in fields if f["id"]==a["driver"])["type"]!="decimal"):
        raise ValueError("Invalid allocation rule")
    pools = d.get("pools", {})
    metrics = d.get("metrics", {})
    if not isinstance(pools,dict) or len(pools)>16 or not isinstance(metrics,dict) or len(metrics)>16:
        raise ValueError("Invalid pool or metric limit")
    for key, pool in pools.items():
        if (not _ID.fullmatch(key) or not isinstance(pool,dict) or set(pool)!={"driver","module"}
            or not any(f["id"]==pool["driver"] and f["type"]=="decimal" and f["required"] for f in fields)
            or pool["module"] not in mods):
            raise ValueError("Invalid configured pool")
    for key, metric in metrics.items():
        if (not _ID.fullmatch(key) or not isinstance(metric,dict) or set(metric)!={"field","op"}
            or not any(f["id"]==metric["field"] and f["type"]=="decimal" for f in fields)
            or metric["op"] not in ("sum","mean")):
            raise ValueError("Invalid configured metric")
    return d
